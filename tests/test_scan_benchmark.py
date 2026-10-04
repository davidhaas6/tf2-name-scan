"""Offline checks of benchmark isolation, routing, caching and timing boundaries."""

import argparse
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_detection import Recognizer, box, frame

from tf2scan import scan_benchmark as bench
from tf2scan.detection import FakeDetector
from tf2scan.download import VIDEO_FORMAT, resolve_media
from tf2scan.storage import Store


def media():
    return {
        "format_id": "247",
        "ext": "webm",
        "width": 320,
        "height": 180,
        "fps": 30,
        "vcodec": "vp9",
        "acodec": "none",
        "duration": 2,
        "url": "https://cdn.example/fresh",
        "http_headers": {"X-Test": "header"},
    }


def install_download(monkeypatch, *, fail=False):
    options_seen = []

    class Downloader:
        def __init__(self, options):
            self.options = options
            options_seen.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def prepare_filename(self, info):
            return self.options["outtmpl"].replace("%(ext)s", info["ext"])

        def extract_info(self, url, download):
            info = media()
            if download:
                Path(self.prepare_filename(info)).write_bytes(b"downloaded-video")
                if fail:
                    raise RuntimeError("download disconnected")
            return info

    monkeypatch.setattr("yt_dlp.YoutubeDL", Downloader)
    monkeypatch.setattr(
        bench,
        "probe",
        lambda _: (
            {"width": 320, "height": 180, "codec_name": "vp9", "avg_frame_rate": "30/1"},
            2,
        ),
    )
    return options_seen


def test_cache_reuse_corruption_and_atomic_publication(tmp_path, monkeypatch):
    options = install_download(monkeypatch)
    timing = {}
    cache, reused = bench.ensure_cache(tmp_path, "clip", "https://example/video", timing)
    assert not reused
    assert "cache_download_s" in timing
    assert cache["media"]["format_id"] == "247"
    assert "url" not in cache["media"] and "http_headers" not in cache["media"]
    assert options[0]["format"] == VIDEO_FORMAT
    assert options[1]["format"] == "247"
    assert bench.ensure_cache(tmp_path, "clip", "https://example/video", {}) == (cache, True)
    assert len(options) == 2  # Reuse does not resolve or download anything.
    (tmp_path / cache["filename"]).write_bytes(b"corrupted-video!")
    assert bench.read_cache(tmp_path, "clip") is None
    install_download(monkeypatch, fail=True)
    with pytest.raises(RuntimeError, match="disconnected"):
        bench.ensure_cache(tmp_path, "clip", "https://example/video", {})
    assert json.loads((tmp_path / "clip.json").read_text()) == cache
    assert not list(tmp_path.glob("download-*"))


def test_failed_first_download_has_no_published_cache(tmp_path, monkeypatch):
    install_download(monkeypatch, fail=True)
    timing = {}
    with pytest.raises(RuntimeError):
        bench.ensure_cache(tmp_path, "clip", "https://example/video", timing)
    assert list(tmp_path.iterdir()) == []
    assert timing["cache_download_s"] >= 0


def test_exact_remote_format_is_required(tmp_path, monkeypatch):
    install_download(monkeypatch)
    with pytest.raises(ValueError, match="format 999 is unavailable"):
        resolve_media("https://example/video", "999")


def request(tmp_path, source):
    run_dir = tmp_path / source
    run_dir.mkdir()
    local = tmp_path / "cached.webm"
    local.write_bytes(b"video")
    data = {
        "output_dir": str(tmp_path / "production"),
        "pipeline": "detection",
        "sampling": {"region": "top_right"},
        "acquisition": {"attempts": 1},
    }
    payload = {
        "config_path": str(tmp_path / "config.yaml"),
        "config_data": data,
        "source": source,
        "video_id": "clip",
        "source_url": "https://example/video",
        "local_path": str(local) if source == "local" else None,
        "selected_media": bench.media_metadata(media()),
    }
    path = run_dir / "request.json"
    bench.write_profile(path, payload)
    return path


@pytest.mark.parametrize("source", ["local", "stream"])
def test_worker_routes_inputs_and_excludes_setup_and_validation(tmp_path, monkeypatch, source):
    path = request(tmp_path, source)
    clock = [0.0]
    monkeypatch.setattr("time.perf_counter", lambda: clock[0])
    detector = FakeDetector({str(i): [box()] for i in range(2)})
    recognizer = Recognizer()
    closed = []
    detector.close = lambda: closed.append("detector")
    recognizer.close = lambda: closed.append("recognizer")

    def models(config):
        assert config.root == path.parent / "corpus"
        clock[0] += 10
        return detector, recognizer

    monkeypatch.setattr("tf2scan.paddle_backend.create_models", models)
    resolved = []

    def resolve(url, format_id):
        resolved.append((url, format_id))
        clock[0] += 2
        return media()

    monkeypatch.setattr(bench, "resolve_media", resolve)
    sampled = []

    def samples(input_path, fps, start, **kwargs):
        sampled.append((input_path, kwargs))
        clock[0] += 3
        return [frame(str(i)) for i in range(2)]

    monkeypatch.setattr("tf2scan.detected_ingestion.sample_frames", samples)
    original_grid = bench.sample_grid

    def slow_validation(*args):
        clock[0] += 100
        return original_grid(*args)

    monkeypatch.setattr(bench, "sample_grid", slow_validation)
    assert bench.scan_worker(path) == 0
    result = json.loads((path.parent / "result.json").read_text())
    profile = json.loads((path.parent / "profile.json").read_text())
    assert result["status"] == "completed"
    assert result["sample_grid"]["complete"]
    assert profile["model_setup_s"] == 10
    assert profile["wall_s"] == (5 if source == "stream" else 3)
    assert profile["realtime_factor"] == (2.5 if source == "stream" else 1.5)
    assert closed == ["recognizer", "detector"]
    assert not (tmp_path / "production").exists()
    if source == "stream":
        assert resolved == [("https://example/video", "247")]
        assert sampled[0][0] == "https://cdn.example/fresh"
        assert sampled[0][1]["http_headers"] == {"X-Test": "header"}
    else:
        assert resolved == []
        assert sampled[0][0] == str(tmp_path / "cached.webm")


@pytest.mark.parametrize("setup_failure", [False, True])
def test_worker_preserves_failure_and_partial_profile(tmp_path, monkeypatch, setup_failure):
    path = request(tmp_path, "stream")
    detector = FakeDetector({})
    recognizer = Recognizer()
    detector.close = recognizer.close = lambda: None

    def models(_):
        if setup_failure:
            raise RuntimeError("model unavailable")
        return detector, recognizer

    monkeypatch.setattr("tf2scan.paddle_backend.create_models", models)

    def resolve(*args):
        raise RuntimeError("format unavailable")

    monkeypatch.setattr(bench, "resolve_media", resolve)
    assert bench.scan_worker(path) == 1
    result = json.loads((path.parent / "result.json").read_text())
    assert result["status"] == "failed"
    assert "unavailable" in result["error"]
    assert (path.parent / "profile.json").exists() is not setup_failure


def test_invocations_keep_history_fresh_corpora_and_failed_commands(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text("pipeline: detection\noutput_dir: production\n")
    args = argparse.Namespace(config=str(config), video="clip", label="baseline")
    cached = {"media": bench.media_metadata(media()), "filename": "cached.webm"}
    monkeypatch.setattr(bench, "read_cache", lambda *_: cached)
    monkeypatch.setattr(bench, "git_state", lambda: {"commit": "abc", "dirty": True})
    launches = []

    def run(command, **kwargs):
        launches.append(command)
        assert command[:3] == [bench.sys.executable, "-m", "tf2scan.scan_benchmark"]
        payload = json.loads(Path(command[-1]).read_text())
        assert payload["source"] == "stream" and payload["local_path"] is None
        assert payload["selected_media"]["format_id"] == "247"
        if len(launches) == 2:
            return SimpleNamespace(returncode=7)
        bench.write_profile(
            Path(command[-1]).parent / "result.json",
            {
                "status": "completed",
                "sample_grid": {"complete": True},
            },
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bench.subprocess, "run", run)
    first = bench.run_benchmark(args, "stream")
    second = bench.run_benchmark(args, "stream")
    assert first["status"] == "completed" and second["status"] == "failed"
    assert "exited 7" in second["error"]
    assert first["run_id"] != second["run_id"]
    history = [json.loads(line) for line in Path(first["history_path"]).read_text().splitlines()]
    assert history == [first, second]
    assert len(list((tmp_path / "output/benchmarks/runs").iterdir())) == 2
    assert not (tmp_path / "production").exists()


def test_compare_flags_media_settings_and_incomplete_grids():
    run = {
        "status": "completed",
        "run_id": "one",
        "source": "local",
        "media": bench.media_metadata(media()),
        "effective_scan": {"settings": "same"},
        "sample_grid": {"complete": True, "sha256": "grid", "frames": 2},
        "timing": {"wall_s": 3, "realtime_factor": 1.5, "stages_s": {"decoding": 2}},
        "cache_download_s": 1000,
    }
    other = copy.deepcopy(run)
    other.update(source="stream", run_id="two", cache_download_s=0)
    other["timing"] = {"wall_s": 5, "realtime_factor": 2.5, "stages_s": {"decoding": 4}}
    result = bench.compare_runs(run, other)
    assert result["equivalent"] and result["wall_difference_s"] == 2
    assert result["stage_differences_s"] == {"decoding": 2}
    other["effective_scan"] = {"settings": "changed"}
    other["media"]["format_id"] = "999"
    other["sample_grid"]["complete"] = False
    result = bench.compare_runs(run, other)
    assert not result["equivalent"]
    assert "Mismatched media" in result["warnings"]
    assert "Mismatched settings/models" in result["warnings"]
    assert "stream sample grid is incomplete or unavailable" in result["warnings"]


def test_sample_grid_detects_missing_samples_and_same_count_wrong_timestamps(tmp_path):
    with Store(tmp_path) as store:
        assert not bench.sample_grid(store, 2, 1)["complete"]
        # A small independent table exercises the timestamp checker without creating OCR rows.
        store.db.execute("ALTER TABLE sampled_frames RENAME TO original_frames")
        store.db.execute("CREATE TABLE sampled_frames(timestamp_s REAL)")
        store.db.executemany("INSERT INTO sampled_frames VALUES (?)", [(0,), (1,)])
        assert bench.sample_grid(store, 2, 1)["complete"]
        store.db.execute("UPDATE sampled_frames SET timestamp_s=0.5 WHERE timestamp_s=1")
        assert not bench.sample_grid(store, 2, 1)["complete"]


def test_preparation_failure_is_recorded_without_launching_models(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bench, "git_state", lambda: {"commit": None, "dirty": None})
    args = argparse.Namespace(config="missing.yaml", video="clip", label=None)
    record = bench.run_benchmark(args, "local")
    assert record["status"] == "failed" and "FileNotFoundError" in record["error"]
    assert record["profile_path"] is None
    assert json.loads(Path(record["history_path"]).read_text()) == record


def test_command_and_scan_timing_exclude_cache_download_from_rtf(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("pipeline: detection\n")
    clock = [0.0]
    monkeypatch.setattr("time.perf_counter", lambda: clock[0])
    monkeypatch.setattr(bench, "git_state", dict)

    def cache(folder, video_id, url, timing):
        clock[0] += 1000
        timing["cache_download_s"] = 1000
        return {"media": bench.media_metadata(media()), "filename": "clip.webm"}, False

    def run(command, **kwargs):
        clock[0] += 50
        bench.write_profile(
            Path(command[-1]).parent / "result.json",
            {
                "status": "completed",
                "timing": {
                    "wall_s": 3,
                    "realtime_factor": 1.5,
                    "model_setup_s": 10,
                    "stages_s": {"decoding": 3},
                },
            },
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bench, "ensure_cache", cache)
    monkeypatch.setattr(bench.subprocess, "run", run)
    record = bench.run_benchmark(
        argparse.Namespace(config="config.yaml", video="clip", label=None), "local"
    )
    assert record["command_s"] == 1050
    assert record["worker_command_s"] == 50
    assert record["cache_download_s"] == 1000
    assert record["timing"]["wall_s"] == 3 and record["timing"]["realtime_factor"] == 1.5


def test_compare_freezes_config_and_pins_cached_format(tmp_path, monkeypatch):
    calls = []
    config = {"config_path": str(tmp_path / "config.yaml"), "config_data": {"sampling": {"fps": 2}}}
    history = tmp_path / "history.jsonl"
    local_dir = tmp_path / "runs/local"
    local_dir.mkdir(parents=True)
    bench.write_profile(local_dir / "request.json", config)

    def run(args, source, **kwargs):
        calls.append((source, kwargs))
        return {
            "status": "completed",
            "source": source,
            "run_id": source,
            "media": bench.media_metadata(media()),
            "cache": {"media": bench.media_metadata(media())},
            "history_path": str(history),
            "effective_scan": config,
            "sample_grid": {"complete": True},
        }

    monkeypatch.setattr(bench, "run_benchmark", run)
    assert bench.main(["compare"]) == 0
    assert [call[0] for call in calls] == ["local", "stream"]
    assert calls[1][1]["selected_media"]["format_id"] == "247"
    assert calls[1][1]["config_snapshot"] == config
    assert (local_dir / "comparison.json").is_file()
