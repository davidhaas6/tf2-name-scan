"""One fresh-corpus production scan per process, with retained benchmark history."""

import argparse
import copy
import hashlib
import json
import logging
import math
import re
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, load_config
from .download import resolve_media, stream_details
from .frames import probe
from .ingestion import ingest
from .performance import ScanProfile, write_profile
from .settings import merge_settings
from .storage import Store
from .ytdlp_options import with_node

DEFAULT_VIDEO = "GCN-CzALyq0"
MEDIA_FIELDS = ("format_id", "ext", "width", "height", "fps", "vcodec", "acodec", "duration")
log = logging.getLogger(__name__)


def media_metadata(info):
    # Signed URLs and HTTP headers are deliberately absent from retained metadata.
    return {key: info.get(key) for key in MEDIA_FIELDS}


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def local_metadata(path):
    stream, duration = probe(path)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Cached video must have a finite positive duration")
    if not all(isinstance(stream.get(k), int) and stream[k] > 0 for k in ("width", "height")):
        raise ValueError("Cached video has invalid dimensions")
    return {
        "width": stream["width"],
        "height": stream["height"],
        "codec": stream.get("codec_name"),
        "duration_s": duration,
        "frame_rate": stream.get("avg_frame_rate"),
    }


def read_cache(cache_dir, video_id):
    """Only published, intact, probeable video files qualify for reuse."""
    try:
        metadata = json.loads((cache_dir / f"{video_id}.json").read_text(encoding="utf-8"))
        path = (cache_dir / metadata["filename"]).resolve()
        if path.parent != cache_dir.resolve() or not path.is_file():
            return None
        if path.stat().st_size != metadata["bytes"] or digest(path) != metadata["sha256"]:
            return None
        if local_metadata(path) != metadata["probe"]:
            return None
        selected = metadata["media"]
        if not selected["format_id"] or selected["acodec"] != "none":
            return None
        stream_details({**selected, "url": "https://validation.invalid"}, {})
        if abs(selected["duration"] - metadata["probe"]["duration_s"]) > 1:
            return None
        return metadata
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, StopIteration):
        return None


def ensure_cache(cache_dir, video_id, source_url, timing):
    cached = read_cache(cache_dir, video_id)
    if cached:
        return cached, True
    from yt_dlp import YoutubeDL

    cache_dir.mkdir(parents=True, exist_ok=True)
    info = resolve_media(source_url)
    timing["media"] = media_metadata(info)
    stream_details(info, {})  # Validate bounded VOD before downloading.
    if info.get("acodec") != "none" or info.get("vcodec") in (None, "none"):
        raise ValueError("Benchmark cache requires a video-only format")
    # Neither a partial download nor a failed probe is ever published as a cache entry.
    with tempfile.TemporaryDirectory(prefix="download-", dir=cache_dir) as temporary:
        options = with_node(
            format=str(info["format_id"]),
            noplaylist=True,
            quiet=True,
            outtmpl=str(Path(temporary) / "video.%(ext)s"),
            retries=3,
            fragment_retries=3,
        )
        started = time.perf_counter()
        try:
            with YoutubeDL(options) as ydl:
                downloaded = ydl.extract_info(source_url, download=True)
                path = Path(ydl.prepare_filename(downloaded))
            if media_metadata(downloaded) != media_metadata(info):
                raise ValueError("Selected media changed during cache download")
            measured = local_metadata(path)
            if [measured["width"], measured["height"]] != [info["width"], info["height"]]:
                raise ValueError("Downloaded video dimensions differ from the selected format")
            if abs(measured["duration_s"] - info["duration"]) > 1:
                raise ValueError("Downloaded video duration differs from the selected VOD")
            filename = f"{video_id}-{uuid.uuid4().hex}.{info['ext']}"
            cached = {
                "filename": filename,
                "media": media_metadata(info),
                "probe": measured,
                "bytes": path.stat().st_size,
                "sha256": digest(path),
            }
            path.replace(cache_dir / filename)
            metadata_path = cache_dir / f"{video_id}.json"
            temporary_metadata = Path(temporary) / "metadata.json"
            write_profile(temporary_metadata, cached)
            temporary_metadata.replace(metadata_path)
        finally:
            timing["cache_download_s"] = round(time.perf_counter() - started, 3)
    return cached, False


def sample_grid(store, duration, fps):
    expected = math.ceil(duration * fps - 1e-8)
    value = hashlib.sha256()
    count = 0
    complete = True
    for row in store.db.execute("SELECT timestamp_s FROM sampled_frames ORDER BY timestamp_s"):
        timestamp = row[0]
        complete &= abs(timestamp - count / fps) <= 1e-8
        value.update(f"{timestamp:.9f}\n".encode())
        count += 1
    return {
        "frames": count,
        "expected_frames": expected,
        "complete": bool(complete and count == expected),
        "sha256": value.hexdigest(),
    }


def scan_worker(request_path):
    """Executed only in a new child process; the production scanner owns all OCR."""
    from .paddle_backend import create_models

    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    run_dir = Path(request_path).parent
    data = copy.deepcopy(request["config_data"])
    data["output_dir"] = str(run_dir / "corpus")
    config = Config(Path(request["config_path"]), data)
    settings = merge_settings(data)
    video = {
        "id": request["video_id"],
        "source_url": request["source_url"],
        "title": request["video_id"],
        "duration_s": None,
        "local_path": None,
    }
    selected = request.get("selected_media")
    result = {"status": "failed", "media": selected, "error": None, "profile_path": None}
    profile = None
    setup_s = 0

    def remote_source(current_video):
        info = resolve_media(
            current_video["source_url"], selected["format_id"] if selected else None
        )
        result["media"] = media_metadata(info)
        details = stream_details(info, current_video)
        video["duration_s"] = details[1]
        return details

    try:
        if config.root.exists():
            raise ValueError("Benchmark corpus already exists; each measured run must start fresh")
        with Store(config.root) as store, ExitStack() as models:
            if request["source"] == "local":
                video["local_path"] = request["local_path"]
                if not Path(video["local_path"]).is_file():
                    raise FileNotFoundError("Benchmark local cache file is missing")
                video["duration_s"] = selected["duration"]
            store.upsert_video(video)
            video = store.video(video["id"])
            started = time.perf_counter()
            try:
                detector, recognizer = create_models(config)
            finally:
                setup_s = time.perf_counter() - started
                result["model_setup_s"] = round(setup_s, 3)
            models.callback(detector.close)
            models.callback(recognizer.close)
            snapshot, _ = config.effective_scan(
                adapters={"detector": detector.metadata, "recognizer": recognizer.metadata}
            )
            result["effective_scan"] = json.loads(snapshot)
            profile = ScanProfile()
            try:
                completed = ingest(
                    store,
                    config,
                    video,
                    recognizer,
                    reprocess=True,
                    detector=detector,
                    profile=profile,
                    stream_resolver=remote_source,
                )
                result["status"] = "completed" if completed else "failed"
            finally:
                # Freeze scan time before grid validation, serialization or model teardown.
                measured = profile.result(video, settings, model_setup_s=setup_s)
                result["profile_path"] = str(run_dir / "profile.json")
                write_profile(run_dir / "profile.json", measured)
                result["timing"] = {
                    key: measured[key]
                    for key in ("wall_s", "realtime_factor", "model_setup_s", "stages_s")
                }
                if video["duration_s"]:
                    store.update_video(video["id"], duration_s=video["duration_s"])
                    result["sample_grid"] = sample_grid(
                        store, video["duration_s"], settings["sampling"]["fps"]
                    )
    except BaseException as exc:
        result["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        log.exception("Benchmark scan failed")
    finally:
        write_profile(run_dir / "result.json", result)
    return 0 if result["status"] == "completed" else 1


def git_state():
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return {"commit": commit, "dirty": bool(status)}
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None}


def print_summary(record):
    timing = record.get("timing") or {}
    print(f"{record['run_id']} {record['source']}: {record['status']}")
    if "wall_s" in timing:
        print(
            f"  scan {timing['wall_s']:.3f}s; RTF {timing['realtime_factor']}; "
            f"model setup {timing['model_setup_s']:.3f}s; command {record['command_s']:.3f}s"
        )
        print("  stages: " + ", ".join(f"{k} {v:.3f}s" for k, v in timing["stages_s"].items()))
    print(f"  cache download {record['cache_download_s']:.3f}s; history: {record['history_path']}")
    for warning in record["warnings"]:
        print(f"  WARNING: {warning}")
    if record.get("error"):
        print(f"  {record['error']}")


def run_benchmark(args, source, *, selected_media=None, config_snapshot=None):
    started = time.perf_counter()
    timestamp = datetime.now(timezone.utc)
    run_id = timestamp.strftime("%Y%m%dT%H%M%S.%fZ") + "-" + source + "-" + uuid.uuid4().hex[:8]
    root = Path("output/benchmarks").resolve()
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True)
    record = {
        "schema_version": 1,
        "run_id": run_id,
        "timestamp": timestamp.isoformat(),
        "label": args.label,
        "video_id": args.video,
        "source": source,
        "git": git_state(),
        "status": "failed",
        "error": None,
        "warnings": [],
        "profile_path": None,
        "cache_download_s": 0.0,
        "cache_reused": None,
        "history_path": str(root / "history.jsonl"),
        "media": selected_media,
    }
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", args.video):
            raise ValueError("--video must be a YouTube video ID")
        config = (
            Config(
                Path(config_snapshot["config_path"]), copy.deepcopy(config_snapshot["config_data"])
            )
            if config_snapshot
            else load_config(args.config)
        )
        if config.data["pipeline"] != "detection":
            raise ValueError("Scan benchmarks require pipeline: detection (ScanProfile)")
        snapshot, _ = config.effective_scan()
        record["effective_scan"] = json.loads(snapshot)
        source_url = f"https://www.youtube.com/watch?v={args.video}"
        cache_dir = root / "cache"
        local_path = None
        if source == "local":
            cached, reused = ensure_cache(cache_dir, args.video, source_url, record)
            record["cache_reused"] = reused
            record["cache"] = cached
            selected_media = cached["media"]
            local_path = str(cache_dir / cached["filename"])
        elif selected_media is None:
            cached = read_cache(cache_dir, args.video)
            if cached:
                selected_media = cached["media"]
        record["media"] = selected_media
        request = {
            "config_path": str(config.path),
            "config_data": config.data,
            "source": source,
            "video_id": args.video,
            "source_url": source_url,
            "local_path": local_path,
            "selected_media": selected_media,
        }
        request_path = run_dir / "request.json"
        write_profile(request_path, request)
        worker_started = time.perf_counter()
        # A new interpreter creates new models; output is retained without polluting the summary.
        with (run_dir / "command.log").open("w", encoding="utf-8") as output:
            process = subprocess.run(
                [sys.executable, "-m", "tf2scan.scan_benchmark", "_run", str(request_path)],
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
            )
        record["worker_command_s"] = round(time.perf_counter() - worker_started, 3)
        record["exit_code"] = process.returncode
        if (run_dir / "result.json").is_file():
            record.update(json.loads((run_dir / "result.json").read_text(encoding="utf-8")))
        else:
            record["error"] = (
                f"Scan process exited {process.returncode} without result.json; see command.log"
            )
        if process.returncode:
            if record["status"] != "interrupted":
                record["status"] = "failed"
            record["error"] = (
                record.get("error") or f"Scan process exited {process.returncode}; see command.log"
            )
        if record.get("sample_grid") and not record["sample_grid"]["complete"]:
            record["warnings"].append(
                "Incomplete sample grid; scan RTF does not represent full coverage"
            )
        if selected_media and record.get("media") != selected_media:
            record["warnings"].append("Resolved media differs from the pinned cache metadata")
    except (Exception, KeyboardInterrupt) as exc:
        record["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"
        log.exception("Benchmark command failed")
    finally:
        record["command_s"] = round(time.perf_counter() - started, 3)
        write_profile(run_dir / "run.json", record)
        # One append per completed invocation, including preparation/model/scan failures.
        with (root / "history.jsonl").open("a", encoding="utf-8") as history:
            history.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        print_summary(record)
    return record


def compare_runs(local, stream):
    warnings = []
    for run in (local, stream):
        if run["status"] != "completed":
            warnings.append(f"{run['source']} run did not complete")
        if not run.get("sample_grid", {}).get("complete"):
            warnings.append(f"{run['source']} sample grid is incomplete or unavailable")
    for key, label in (
        ("effective_scan", "settings/models"),
        ("media", "media"),
        ("sample_grid", "sample grids"),
    ):
        if local.get(key) != stream.get(key):
            warnings.append(f"Mismatched {label}")
    result = {
        "local_run_id": local["run_id"],
        "stream_run_id": stream["run_id"],
        "equivalent": not warnings,
        "warnings": warnings,
    }
    print("Comparison (stream minus local; cache download excluded):")
    if warnings:
        for warning in warnings:
            print(f"  WARNING: {warning}")
        print("  Runs do not establish an equivalent-work speed comparison.")
    if local.get("timing") and stream.get("timing"):
        a, b = local["timing"], stream["timing"]
        result["wall_difference_s"] = round(b["wall_s"] - a["wall_s"], 3)
        result["rtf_difference"] = (
            round(b["realtime_factor"] - a["realtime_factor"], 3)
            if a["realtime_factor"] is not None and b["realtime_factor"] is not None
            else None
        )
        result["stage_differences_s"] = {
            key: round(b["stages_s"][key] - a["stages_s"][key], 3) for key in a["stages_s"]
        }
        print(
            f"  wall delta {result['wall_difference_s']:+.3f}s; RTF delta {result['rtf_difference']}"
        )
        print(
            "  stages: "
            + ", ".join(f"{k} {v:+.3f}s" for k, v in result["stage_differences_s"].items())
        )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("benchmark", "compare"):
        sub = commands.add_parser(name)
        sub.add_argument("--video", default=DEFAULT_VIDEO)
        sub.add_argument("--config", default="config.yaml")
        sub.add_argument("--label")
        if name == "benchmark":
            sub.add_argument("--source", choices=("stream", "local"), default="stream")
    worker = commands.add_parser("_run", help=argparse.SUPPRESS)
    worker.add_argument("request")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.command == "_run":
        return scan_worker(args.request)
    if args.command == "benchmark":
        result = run_benchmark(args, args.source)
        if result["status"] == "interrupted":
            return 130
        return int(result["status"] != "completed")
    local = run_benchmark(args, "local")
    if local["status"] == "interrupted":
        return 130
    # A failed download cannot establish which exact format to compare.
    if not local.get("cache"):
        print("Comparison unavailable: local preparation did not produce a valid cached video.")
        return 1
    local_dir = Path(local["history_path"]).parent / "runs" / local["run_id"]
    snapshot = json.loads((local_dir / "request.json").read_text(encoding="utf-8"))
    stream = run_benchmark(args, "stream", selected_media=local["media"], config_snapshot=snapshot)
    comparison = compare_runs(local, stream)
    write_profile(
        Path(local["history_path"]).parent / "runs" / local["run_id"] / "comparison.json",
        comparison,
    )
    return int(not comparison["equivalent"])


if __name__ == "__main__":
    raise SystemExit(main())
