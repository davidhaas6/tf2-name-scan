"""Durability and ownership checks for bounded detector scans."""

import shutil
import subprocess
from io import BytesIO

import pytest
from test_detection import Recognizer, box, frame

from tf2scan.config import Config
from tf2scan.detection import FakeDetector
from tf2scan.frames import bounded_request_supported, frame_iterator, sample_frames
from tf2scan.ingestion import ingest
from tf2scan.storage import Store


def _config(tmp_path):
    return Config(
        tmp_path / "config.yaml",
        {
            "sampling": {"chunk_seconds": 2},
            "acquisition": {"attempts": 1, "overlap_s": 1},
            "persistence": {"max_rows": 1},
        },
    )


@pytest.mark.parametrize("remote", [False, True])
def test_resume_committed_batches_and_overlap(tmp_path, monkeypatch, remote):
    config = _config(tmp_path)
    detector = FakeDetector({str(i): [box()] for i in range(4)})
    recognizer = Recognizer()
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"fixture")
    if remote:
        monkeypatch.setattr(
            "tf2scan.detected_ingestion.resolve_stream",
            lambda _: (
                "https://cdn.example/video",
                4,
                {"width": 320, "height": 180},
                "cdn.example",
                {},
            ),
        )
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
        store.upsert_video(
            {
                "id": "v",
                "title": "video",
                "source_url": "https://example/video",
                "duration_s": 4,
                "local_path": None if remote else str(local),
            }
        )
        video = store.video("v")
        with pytest.raises(RuntimeError, match="disconnected"):
            ingest(store, config, video, recognizer, detector=detector)
        assert [
            r["status"] for r in store.rows("SELECT status FROM scan_chunks ORDER BY sequence_no")
        ] == ["completed", "failed"]
        assert store.rows("SELECT count(*) AS n FROM sampled_frames")[0]["n"] == 3
        evidence = store.rows("SELECT evidence_path FROM text_clusters")[0]["evidence_path"]
        assert (store.root / evidence).is_file()
        assert ingest(store, config, video, recognizer, detector=detector)
        assert starts == [0, 1, 1]
        assert [
            r["sample_key"]
            for r in store.rows("SELECT sample_key FROM sampled_frames ORDER BY timestamp_s")
        ] == ["0", "1", "2", "3"]
        cluster = store.rows("SELECT * FROM text_clusters")[0]
        assert cluster["support_count"] == 4
        assert cluster["close_reason"] == "end_of_scan"
        assert (store.root / evidence).is_file()
        assert len(store.selected_scan_ids()) == 1
        assert not store.rows("PRAGMA foreign_key_check")


@pytest.mark.parametrize("remote", [False, True])
def test_interrupt_resumes_near_committed_frame_in_same_chunk(tmp_path, monkeypatch, remote):
    config = Config(
        tmp_path / "config.yaml",
        {
            "sampling": {"chunk_seconds": 10},
            "acquisition": {"attempts": 1, "overlap_s": 2},
            "persistence": {"max_rows": 1},
        },
    )
    detector = FakeDetector({str(i): [box()] for i in range(10)})
    recognizer = Recognizer()
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"fixture")
    if remote:
        monkeypatch.setattr(
            "tf2scan.detected_ingestion.resolve_stream",
            lambda _: (
                "https://cdn.example/video",
                10,
                {"width": 320, "height": 180},
                "cdn.example",
                {},
            ),
        )
    starts = []

    def samples(path, fps, start, **kwargs):
        starts.append(start)
        if len(starts) == 1:

            def interrupted():
                for second in range(6):
                    yield frame(str(second))
                raise KeyboardInterrupt

            return interrupted()
        return [frame(str(i)) for i in range(int(start), int(kwargs["end"]))]

    monkeypatch.setattr("tf2scan.detected_ingestion.sample_frames", samples)
    with Store(config.root) as store:
        store.upsert_video(
            {
                "id": "v",
                "title": "video",
                "source_url": "https://example/video",
                "duration_s": 10,
                "local_path": None if remote else str(local),
            }
        )
        video = store.video("v")
        with pytest.raises(KeyboardInterrupt):
            ingest(store, config, video, recognizer, detector=detector)
        assert (
            store.rows("SELECT last_processed_timestamp_s FROM scan_chunks")[0][
                "last_processed_timestamp_s"
            ]
            == 5
        )
        assert ingest(store, config, video, recognizer, detector=detector)
        assert starts == [0, 3]
        assert [
            row["timestamp_s"]
            for row in store.rows("SELECT timestamp_s FROM sampled_frames ORDER BY timestamp_s")
        ] == list(range(10))
        assert [
            row["requested_start_s"]
            for row in store.rows(
                "SELECT requested_start_s FROM chunk_attempts ORDER BY attempt_no"
            )
        ] == [0, 3]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg not installed")
def test_real_local_section_timestamps_and_early_close(tmp_path):
    path = tmp_path / "section.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=96x64:rate=10",
            "-t",
            "4",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )
    records = list(sample_frames(path, fps=1, start=1, end=3))
    assert [record.timestamp_s for record in records] == [1, 2]
    with frame_iterator(sample_frames(path, fps=1, start=1, end=3)) as frames:
        assert next(frames).timestamp_s == 1


@pytest.mark.parametrize(
    "payload,message", [(b"too short", "truncated frame"), (b"", "upstream failed")]
)
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
        list(
            sample_frames("https://cdn.example/video", end=1, dimensions={"width": 2, "height": 2})
        )
    assert process.stdout.closed
    if payload:
        assert process.terminated


@pytest.mark.parametrize("count", [0, 2, 4, 5])
def test_clean_decoder_exit_checks_requested_coverage(monkeypatch, count):
    class Process:
        def __init__(self):
            self.stdout = BytesIO(bytes(12 * count))

        def wait(self, timeout=None):
            return 0

    process = Process()
    monkeypatch.setattr("tf2scan.frames.subprocess.Popen", lambda *a, **k: process)
    source = sample_frames(
        "https://cdn.example/video", start=10, end=15, dimensions={"width": 2, "height": 2}
    )
    if count < 4:
        with pytest.raises(RuntimeError, match=f"ended early: decoded {count}"):
            list(source)
    else:
        assert len(list(source)) == count
    assert process.stdout.closed


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg not installed")
def test_short_local_source_fails_scan_and_resumes(tmp_path):
    path = tmp_path / "short.mp4"

    def create_clip(duration):
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=96x64:r=10",
                "-t",
                str(duration),
                "-pix_fmt",
                "yuv420p",
                str(path),
            ],
            check=True,
        )

    create_clip(2)
    config = Config(
        tmp_path / "config.yaml",
        {
            "sampling": {"chunk_seconds": 5},
            "acquisition": {"attempts": 1, "overlap_s": 1},
            "persistence": {"max_rows": 1},
        },
    )
    with Store(config.root) as store:
        store.upsert_video(
            {
                "id": "v",
                "title": "video",
                "source_url": path.as_uri(),
                "local_path": str(path),
                "duration_s": 5,
            }
        )
        detector, recognizer = FakeDetector({}), Recognizer()
        video = store.video("v")
        with pytest.raises(RuntimeError, match="ended early"):
            ingest(store, config, video, recognizer, detector=detector)
        assert not store.selected_scan_ids()
        assert store.rows("SELECT status FROM scan_chunks")[0]["status"] == "failed"
        assert store.rows("SELECT count(*) n FROM sampled_frames")[0]["n"] == 2
        create_clip(5)
        assert ingest(store, config, video, recognizer, detector=detector)
        assert len(store.selected_scan_ids()) == 1
        assert [
            r["timestamp_s"]
            for r in store.rows("SELECT timestamp_s FROM sampled_frames ORDER BY timestamp_s")
        ] == list(range(5))
        assert not store.rows("PRAGMA foreign_key_check")


def test_failed_reprocess_keeps_completed_evidence(tmp_path, monkeypatch):
    config = _config(tmp_path)
    detector = FakeDetector({"0": [box()]})
    recognizer = Recognizer()
    with Store(config.root) as store:
        store.upsert_video({"id": "v", "title": "video", "source_url": "url", "duration_s": 1})
        video = store.video("v")
        assert ingest(store, config, video, recognizer, detector=detector, frame_source=[frame()])
        original = store.selected_scan_ids()[0]
        evidence = store.rows("SELECT evidence_path FROM text_clusters")[0]["evidence_path"]

        def broken():
            raise RuntimeError("decoder failed")
            yield

        with pytest.raises(RuntimeError, match="decoder failed"):
            ingest(
                store,
                config,
                video,
                recognizer,
                detector=detector,
                frame_source=broken(),
                reprocess=True,
            )
        assert store.selected_scan_ids() == [original]
        assert (store.root / evidence).is_file()


class _NoFrameProcess:
    """Minimal Popen stand-in that reports a clean exit after an empty drain."""

    def __init__(self):
        self.stdout = BytesIO(b"")

    def wait(self, timeout=None):
        return 0


def _capture_command(monkeypatch):
    captured = {}

    def fake_popen(command, *args, **kwargs):
        captured["command"] = command
        return _NoFrameProcess()

    monkeypatch.setattr("tf2scan.frames.subprocess.Popen", fake_popen)
    return captured


def _forbidden_probe():
    raise AssertionError("capability probe must not run for this input")


def test_bounded_http_command_has_request_flags(monkeypatch):
    monkeypatch.setattr("tf2scan.frames.bounded_request_supported", lambda: True)
    captured = _capture_command(monkeypatch)
    list(
        sample_frames(
            "https://cdn.example/video",
            end=1,
            dimensions={"width": 2, "height": 2},
            bounded_requests=True,
        )
    )
    command = captured["command"]
    assert command[command.index("-request_size") + 1] == "10485760"
    assert command[command.index("-multiple_requests") + 1] == "1"


def test_bounded_http_respects_custom_size_and_no_persistence(monkeypatch):
    monkeypatch.setattr("tf2scan.frames.bounded_request_supported", lambda: True)
    captured = _capture_command(monkeypatch)
    list(
        sample_frames(
            "https://cdn.example/video",
            end=1,
            dimensions={"width": 2, "height": 2},
            bounded_requests=True,
            request_size_bytes=3145728,
            persistent_connections=False,
        )
    )
    command = captured["command"]
    assert command[command.index("-request_size") + 1] == "3145728"
    assert "-multiple_requests" not in command


def test_unbounded_http_command_has_no_request_flags(monkeypatch):
    monkeypatch.setattr("tf2scan.frames.bounded_request_supported", _forbidden_probe)
    captured = _capture_command(monkeypatch)
    list(
        sample_frames(
            "https://cdn.example/video",
            end=1,
            dimensions={"width": 2, "height": 2},
            bounded_requests=False,
        )
    )
    command = captured["command"]
    assert "-request_size" not in command
    assert "-multiple_requests" not in command


def test_local_source_ignores_bounded_request_flags(monkeypatch, tmp_path):
    monkeypatch.setattr("tf2scan.frames.bounded_request_supported", _forbidden_probe)
    captured = _capture_command(monkeypatch)
    list(
        sample_frames(
            str(tmp_path / "clip.mp4"),
            end=1,
            dimensions={"width": 2, "height": 2},
            bounded_requests=True,
        )
    )
    command = captured["command"]
    assert "-request_size" not in command
    assert "-multiple_requests" not in command


def test_bounded_requires_capable_ffmpeg(monkeypatch):
    class Help:
        stdout = "  -multiple_requests  <boolean>  use persistent connections\n"
        stderr = ""

    monkeypatch.setattr("tf2scan.frames.subprocess.run", lambda *args, **kwargs: Help())
    bounded_request_supported.cache_clear()
    assert bounded_request_supported() is False
    with pytest.raises(RuntimeError, match="FFmpeg 9\\+.*request_size"):
        list(
            sample_frames(
                "https://cdn.example/video",
                end=1,
                dimensions={"width": 2, "height": 2},
                bounded_requests=True,
            )
        )
    bounded_request_supported.cache_clear()


def test_bounded_request_size_must_be_positive_integer(monkeypatch):
    monkeypatch.setattr("tf2scan.frames.subprocess.Popen", lambda *args, **kwargs: None)
    for value in (0, -1, 1.5, True, "10485760"):
        with pytest.raises(ValueError, match="request_size_bytes"):
            list(
                sample_frames(
                    "https://cdn.example/video",
                    end=1,
                    dimensions={"width": 2, "height": 2},
                    bounded_requests=True,
                    request_size_bytes=value,
                )
            )
