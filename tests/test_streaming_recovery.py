"""Durability and ownership checks for bounded detector scans."""

import shutil
import subprocess
from io import BytesIO

import pytest
from test_detection import Recognizer, box, frame

from tf2scan.config import Config
from tf2scan.detection import FakeDetector
from tf2scan.frames import frame_iterator, sample_frames
from tf2scan.ingestion import ingest
from tf2scan.storage import Store


def _config(tmp_path):
    return Config(tmp_path / "config.yaml", {
        "sampling": {"chunk_seconds": 2},
        "acquisition": {"attempts": 1, "overlap_s": 1},
        "persistence": {"max_rows": 1},
    })


@pytest.mark.parametrize("remote", [False, True])
def test_resume_committed_batches_and_overlap(tmp_path, monkeypatch, remote):
    config = _config(tmp_path)
    detector = FakeDetector({str(i): [box()] for i in range(4)})
    recognizer = Recognizer()
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"fixture")
    if remote:
        monkeypatch.setattr("tf2scan.detected_ingestion.resolve_stream",
                            lambda _: ("https://cdn.example/video", 4, {"width": 320, "height": 180},
                                       "cdn.example", {}))
    failed = [False]
    starts = []

    def samples(path, fps, start, **kwargs):
        starts.append(start)
        if start == 1 and not failed[0]:
            failed[0] = True

            def broken():
                yield frame("1")
                yield frame("2")
                raise RuntimeError("source disconnected")

            return broken()
        return [frame(str(i)) for i in range(int(start), int(kwargs["end"]))]

    monkeypatch.setattr("tf2scan.detected_ingestion.sample_frames", samples)
    with Store(config.root) as store:
        store.upsert_video({"id": "v", "title": "video", "source_url": "https://example/video",
                            "duration_s": 4, "local_path": None if remote else str(local)})
        video = store.video("v")
        with pytest.raises(RuntimeError, match="disconnected"):
            ingest(store, config, video, recognizer, detector=detector)
        assert [r["status"] for r in store.rows(
            "SELECT status FROM scan_chunks ORDER BY sequence_no")] == ["completed", "failed"]
        assert store.rows("SELECT count(*) AS n FROM sampled_frames")[0]["n"] == 3
        evidence = store.rows("SELECT evidence_path FROM text_clusters")[0]["evidence_path"]
        assert (store.root / evidence).is_file()
        assert ingest(store, config, video, recognizer, detector=detector)
        assert starts == [0, 1, 1]
        assert [r["sample_key"] for r in store.rows(
            "SELECT sample_key FROM sampled_frames ORDER BY timestamp_s")] == ["0", "1", "2", "3"]
        cluster = store.rows("SELECT * FROM text_clusters")[0]
        assert cluster["support_count"] == 4
        assert cluster["close_reason"] == "end_of_scan"
        assert (store.root / evidence).is_file()
        assert len(store.selected_scan_ids()) == 1
        assert not store.rows("PRAGMA foreign_key_check")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg not installed")
def test_real_local_section_timestamps_and_early_close(tmp_path):
    path = tmp_path / "section.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=size=96x64:rate=10", "-t", "4", "-pix_fmt", "yuv420p",
                    str(path)], check=True)
    records = list(sample_frames(path, fps=1, start=1, end=3))
    assert [record.timestamp_s for record in records] == [1, 2]
    with frame_iterator(sample_frames(path, fps=1, start=1, end=3)) as frames:
        assert next(frames).timestamp_s == 1


@pytest.mark.parametrize("payload,message", [(b"too short", "truncated frame"),
                                              (b"", "upstream failed")])
def test_decoder_failure_closes_process(monkeypatch, payload, message):
    class Process:
        def __init__(self):
            self.stdout = BytesIO(payload)
            self.terminated = False

        def poll(self):
            return -15 if self.terminated else None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return -15 if self.terminated else 1

    process = Process()
    monkeypatch.setattr("tf2scan.frames.subprocess.Popen", lambda *a, **k: process)
    with pytest.raises(RuntimeError, match=message):
        list(sample_frames("https://cdn.example/video", end=1,
                           dimensions={"width": 2, "height": 2}))
    assert process.stdout.closed
    if payload:
        assert process.terminated


def test_failed_reprocess_keeps_completed_evidence(tmp_path, monkeypatch):
    config = _config(tmp_path)
    detector = FakeDetector({"0": [box()]})
    recognizer = Recognizer()
    with Store(config.root) as store:
        store.upsert_video({"id": "v", "title": "video", "source_url": "url",
                            "duration_s": 1})
        video = store.video("v")
        assert ingest(store, config, video, recognizer, detector=detector,
                      frame_source=[frame()])
        original = store.selected_scan_ids()[0]
        evidence = store.rows("SELECT evidence_path FROM text_clusters")[0]["evidence_path"]

        def broken():
            raise RuntimeError("decoder failed")
            yield

        with pytest.raises(RuntimeError, match="decoder failed"):
            ingest(store, config, video, recognizer, detector=detector,
                   frame_source=broken(), reprocess=True)
        assert store.selected_scan_ids() == [original]
        assert (store.root / evidence).is_file()
