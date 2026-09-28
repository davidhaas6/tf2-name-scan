import argparse
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image

from tf2scan.cli import execute
from tf2scan.download import cleanup_downloads, download, resolve_stream
from tf2scan.indexing import index_sources
from tf2scan.recognize import OpenOCRRecognizer
from tf2scan.storage import Store


def test_openocr_pil_and_bgr_input_contract():
    adapter = OpenOCRRecognizer.__new__(OpenOCRRecognizer)
    adapter.batch_size = 2
    adapter.pil_input = True
    adapter.bgr = False
    captured = []

    def engine(img_numpy_list, batch_num):
        captured.extend(img_numpy_list)
        return [{"text": "PlayerOne", "score": 0.9} for _ in img_numpy_list]

    adapter.engine = engine
    image = Image.new("RGB", (10, 10), (255, 0, 0))
    assert adapter.recognize([image])[0].text == "PlayerOne"
    assert isinstance(captured.pop(), Image.Image)
    adapter.pil_input = False
    adapter.bgr = True
    adapter.recognize([image])
    assert np.array_equal(captured.pop()[0, 0], [0, 0, 255])


def test_archive_recovery_and_download_retention(tmp_path, monkeypatch):
    class YDL:
        def __init__(self, options):
            assert options["js_runtimes"] == {"node": {}}
            self.options = options
            self.archive = {"youtube test"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download=False):
            return {"id": "test", "ext": "mp4"}

        def prepare_filename(self, info):
            return str(tmp_path / "downloads/test.mp4")

        def _make_archive_id(self, info):
            return "youtube test"

        def process_info(self, info):
            assert "youtube test" not in self.archive
            Path(self.prepare_filename(info)).write_bytes(b"video")

    monkeypatch.setattr("yt_dlp.YoutubeDL", YDL)
    with Store(tmp_path) as store:
        store.upsert_video(
            {
                "id": "test",
                "source_url": "https://youtube.com/watch?v=test",
                "title": "test",
                "status": "scanned",
            }
        )
        path = download(store, store.video("test"))
        assert path.exists()
        assert store.video("test")["status"] == "scanned"
        local = tmp_path / "user-video.mp4"
        local.write_bytes(b"user owned")
        store.upsert_video(
            {
                "id": "local",
                "source_url": local.as_uri(),
                "title": "local",
                "status": "scanned",
                "local_path": str(local),
            }
        )
        cleanup_downloads(store)
        assert not path.exists()
        assert local.exists()


def test_index_and_stream_resolution_enable_node(tmp_path, monkeypatch):
    url = "https://www.youtube.com/watch?v=ZiZmodw-yRc"
    options_seen = []

    class YDL:
        def __init__(self, options):
            options_seen.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, source, download=False):
            assert source == url and not download
            return {
                "id": "ZiZmodw-yRc",
                "webpage_url": url,
                "title": "test",
                "duration": 15,
                "formats": [{"height": 720}],
                "url": "https://media.example.com/video",
                "width": 1280,
                "height": 720,
            }

    monkeypatch.setattr("yt_dlp.YoutubeDL", YDL)
    config = SimpleNamespace(data={"sources": [url], "pipeline": "detection"})
    with Store(tmp_path) as store:
        assert index_sources(store, config) == (1, 0)
        assert resolve_stream(store.video("ZiZmodw-yRc"))[0] == "https://media.example.com/video"
    assert all(options["js_runtimes"] == {"node": {}} for options in options_seen)


def test_cli_query_does_not_load_ocr(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("query: {target_name: PlayerOne}\n")

    def forbidden(*args, **kwargs):
        raise AssertionError("Query must not touch OCR or media")

    monkeypatch.setattr("tf2scan.cli.OpenOCRRecognizer", forbidden)
    monkeypatch.setattr("tf2scan.cli.download", forbidden)
    monkeypatch.setattr("tf2scan.cli.ingest", forbidden)
    assert (
        execute(argparse.Namespace(command="query", config=str(config), name=None, alias=[])) == 0
    )
    assert (tmp_path / "output/report/index.html").exists()


def test_failed_video_does_not_stop_queue(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("pipeline: legacy_hud")
    with Store(tmp_path / "output") as store:
        for video_id in ("bad", "good"):
            store.upsert_video(
                {"id": video_id, "source_url": "https://example.com/video", "title": video_id}
            )
    visited = []

    def fake_ingest(store, config, video, *args, **kwargs):
        visited.append(video["id"])
        if video["id"] == "bad":
            raise RuntimeError("decoder failed")
        store.update_video(video["id"], status="scanned")

    monkeypatch.setattr("tf2scan.cli.OpenOCRRecognizer", lambda _: object())
    monkeypatch.setattr("tf2scan.cli.download", lambda *args: Path("fake.mp4"))
    monkeypatch.setattr("tf2scan.cli.ingest", fake_ingest)
    args = argparse.Namespace(
        command="scan",
        config=str(config),
        fps=None,
        local=None,
        video=None,
        profile=None,
        reprocess=False,
    )
    assert execute(args) == 1
    assert visited == ["bad", "good"]
    with Store(tmp_path / "output") as store:
        assert store.video("bad")["status"] == "failed"
        assert store.video("good")["status"] == "scanned"
