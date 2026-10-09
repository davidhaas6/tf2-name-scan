"""Focused acquisition diagnostics; use benchmark_scan.py for actual scan timings.

Resolve the cached format once, then drain production-shaped FFmpeg sections
without OCR and compare native yt-dlp transfers with/without bounded HTTP ranges.
Signed URLs and authentication headers are never written to retained results;
only numerical Range/Content-Range headers are recorded.
Requires psutil (included in the installed Paddle environment).
"""

import argparse
import copy
import hashlib
import json
import math
import re
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil
from yt_dlp import YoutubeDL
from yt_dlp.downloader.http import HttpFD
from yt_dlp.networking import Request

from tf2scan.config import load_config
from tf2scan.detected_ingestion import _chunks
from tf2scan.download import resolve_media
from tf2scan.scan_benchmark import DEFAULT_VIDEO, digest, git_state, media_metadata, read_cache
from tf2scan.settings import merge_settings
from tf2scan.ytdlp_options import with_node


def drain(path, info, sampling, *, remote=False, copy_only=False):
    """Mirror sample_frames' command/filter/sections, with diagnostic logging.

    SHA-256 covers every eligible RGB frame, not just the assigned sample grid.
    Copy-only bypasses decoding/filtering and has no frame-equivalence claim.
    """
    result = {"sections": [], "status": "failed"}
    pixels, grid = hashlib.sha256(), hashlib.sha256()
    frames = 0
    complete = True
    started = time.perf_counter()
    last_timestamp = 0
    for _, start, end in _chunks(info["duration"], sampling["chunk_seconds"]):
        fps = sampling["fps"]
        source_start = max(
            0, math.floor((max(start, last_timestamp) - sampling["overlap_s"]) * fps) / fps
        )
        height = min(info["height"], sampling["max_height"])
        width = max(1, round(info["width"] * height / info["height"]))
        command = ["ffmpeg", "-nostdin", "-v", "debug", "-benchmark", "-noautorotate"]
        if remote:
            command += [
                "-rw_timeout",
                "30000000",
                "-headers",
                "".join(f"{key}: {value}\r\n" for key, value in info["http_headers"].items()),
            ]
        command += [
            "-ss",
            str(source_start),
            "-t",
            str(end - source_start),
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
        ]
        if copy_only:
            command += ["-c:v", "copy", "-f", "null", "-"]
        else:
            command += [
                "-vf",
                f"setpts=PTS-STARTPTS,fps={fps}:start_time=0,scale={width}:{height}",
                "-pix_fmt",
                "rgb24",
                "-f",
                "rawvideo",
                "pipe:1",
            ]
        section = {"start_s": source_start, "end_s": end, "frames": 0}
        stop = threading.Event()
        memory = [0]
        section_started = time.perf_counter()
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
            observed = psutil.Process(process.pid)

            def monitor(stop=stop, observed=observed, memory=memory):
                while not stop.is_set():
                    try:
                        m = observed.memory_info()
                        memory[0] = max(memory[0], getattr(m, "peak_wset", m.rss), m.rss)
                    except psutil.NoSuchProcess:
                        return
                    stop.wait(0.05)

            thread = threading.Thread(target=monitor, daemon=True)
            thread.start()
            try:
                if not copy_only:
                    size = width * height * 3
                    index = 0
                    while True:
                        data = process.stdout.read(size)
                        if not data:
                            break
                        if len(data) != size:
                            raise RuntimeError("Truncated RGB frame")
                        if index == 0:
                            section["first_frame_s"] = round(
                                time.perf_counter() - section_started, 3
                            )
                        timestamp = source_start + index / fps
                        index += 1
                        section["frames"] += 1
                        if start - 1e-8 <= timestamp < end - 1e-8:
                            complete &= abs(timestamp - frames / fps) <= 1e-8
                            pixels.update(data)
                            grid.update(f"{timestamp:.9f}\n".encode())
                            frames += 1
                            last_timestamp = timestamp
                code = process.wait(timeout=30)
                if copy_only:
                    last_timestamp = (math.ceil(end * fps - 1e-9) - 1) / fps
            finally:
                if process.poll() is None:
                    process.terminate()
                process.stdout.close()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                stop.set()
                thread.join()
            section["wall_s"] = round(time.perf_counter() - section_started, 3)
            section["peak_memory_bytes"] = memory[0]
            section["exit_code"] = code
            errors.seek(0)
            detail = errors.read().decode(errors="replace")
            # Retain only safe numerical diagnostics, never complete debug logs.
            section["http_ranges"] = re.findall(r"Range: bytes=([0-9]+-[0-9]*)", detail)
            section["io_statistics"] = re.findall(
                r"Statistics: ([0-9]+) bytes read, ([0-9]+) seeks", detail
            )
            bench = re.search(r"bench: utime=([\d.]+)s stime=([\d.]+)s rtime=([\d.]+)s", detail)
            if bench:
                section["ffmpeg_cpu_s"] = round(float(bench[1]) + float(bench[2]), 3)
                section["ffmpeg_wall_s"] = float(bench[3])
            if code:
                raise RuntimeError(f"FFmpeg exited {code}; upstream details omitted")
            result["sections"].append(section)
    result.update(status="completed", wall_s=round(time.perf_counter() - started, 3))
    if not copy_only:
        expected = math.ceil(info["duration"] * sampling["fps"] - 1e-8)
        result["sample_grid"] = {
            "frames": frames,
            "expected_frames": expected,
            "complete": bool(complete and frames == expected),
            "sha256": grid.hexdigest(),
        }
        result["rgb_sha256"] = pixels.hexdigest()
    return result


def transfer(info, folder, *, chunked, expected):
    selected = copy.deepcopy(info)
    selected["downloader_options"] = {"http_chunk_size": 10 << 20 if chunked else 0}
    requests = []
    destination = folder / "transfer.mp4"
    # Use the same native downloader/client/headers/URL in both treatments.
    with YoutubeDL(with_node(quiet=True, noprogress=True, retries=0)) as ydl:
        original = ydl.urlopen

        def observed(request):
            range_header = request.headers.get("Range")
            started = time.perf_counter()
            response = original(request)
            requests.append(
                {
                    "range": range_header,
                    "open_s": round(time.perf_counter() - started, 3),
                    "content_length": response.headers.get("Content-Length"),
                    "content_range": response.headers.get("Content-Range"),
                }
            )
            return response

        ydl.urlopen = observed
        started = time.perf_counter()
        success = HttpFD(ydl, ydl.params).download(str(destination), selected)[0]
        elapsed = time.perf_counter() - started
    if not success:
        raise RuntimeError("Native transfer failed")
    result = {
        "status": "completed",
        "wall_s": round(elapsed, 3),
        "bytes": destination.stat().st_size,
        "sha256": digest(destination),
        "http_requests": requests,
    }
    result["matches_cache"] = result["sha256"] == expected["sha256"]
    destination.unlink()
    return result


@contextmanager
def bounded_proxy(info):
    """Experimental seekable stream: serve FFmpeg from bounded upstream requests.

    No media is retained and only a 64 KiB read buffer is used. This is an
    attribution treatment, not a production retry/cancellation implementation.
    The experiment permits brief overlap while FFmpeg opens a new seek response
    before closing its old response. Production would need explicit cancellation
    and enforcement of the configured upstream concurrency limit.
    """
    requests = []
    size = info["filesize"]
    with YoutubeDL(with_node(quiet=True, noprogress=True)) as ydl:

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                match = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", "bytes=0-"))
                if not match or int(match[1]) >= size:
                    self.send_error(416)
                    return
                start = int(match[1])
                end = min(int(match[2]) if match[2] else size - 1, size - 1)
                self.send_response(206)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                try:
                    while start <= end:
                        chunk_end = min(start + 10000000 - 1, end)
                        headers = {
                            **info["http_headers"],
                            "Accept-Encoding": "identity",
                            "Range": f"bytes={start}-{chunk_end}",
                        }
                        request = {"range": headers["Range"], "bytes_forwarded": 0, "bytes_read": 0}
                        requests.append(request)
                        opened = time.perf_counter()
                        with ydl.urlopen(Request(info["url"], headers=headers)) as response:
                            request["open_s"] = round(time.perf_counter() - opened, 3)
                            if (
                                response.headers.get("Content-Range")
                                != f"bytes {start}-{chunk_end}/{size}"
                            ):
                                raise RuntimeError("Upstream ignored the bounded byte range")
                            remaining = chunk_end - start + 1
                            while remaining:
                                data = response.read(min(65536, remaining))
                                if not data:
                                    raise RuntimeError("Upstream range ended early")
                                request["bytes_read"] += len(data)
                                self.wfile.write(data)
                                request["bytes_forwarded"] += len(data)
                                remaining -= len(data)
                        start = chunk_end + 1
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # FFmpeg closes a response when it seeks.

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/video", requests
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", default=DEFAULT_VIDEO)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--steps",
        nargs="+",
        choices=("local", "remote", "copy", "chunked", "unchunked", "proxy"),
        default=["local", "remote", "chunked", "unchunked"],
    )
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    root = Path("output/benchmarks").resolve()
    cache = read_cache(root / "cache", args.video)
    if not cache:
        parser.error("Prepare a validated cache with benchmark_scan.py benchmark --source local")
    config = load_config(args.config)
    settings = merge_settings(config.data)
    sampling = {**settings["sampling"], "overlap_s": settings["acquisition"]["overlap_s"]}
    folder = (
        root
        / "attribution"
        / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + args.video)
    )
    folder.mkdir(parents=True)
    result = {
        "video_id": args.video,
        "git": git_state(),
        "sampling": sampling,
        "cache_sha256": cache["sha256"],
        "results": [],
    }
    started = time.perf_counter()
    info = resolve_media(
        f"https://www.youtube.com/watch?v={args.video}", cache["media"]["format_id"]
    )
    result["resolution_s"] = round(time.perf_counter() - started, 3)
    result["media"] = media_metadata(info)
    if result["media"] != cache["media"]:
        raise ValueError("Resolved format differs from the validated cache")
    result["delivery"] = {
        key: info.get(key) for key in ("tbr", "protocol", "filesize", "downloader_options")
    }
    result["versions"] = {
        "ffmpeg": subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, check=True
        ).stdout.splitlines()[0]
    }
    for repetition in range(args.repeat):
        for step in args.steps:
            print(f"{args.video} repetition {repetition + 1}: {step}", flush=True)
            item = {"step": step, "repetition": repetition + 1}
            try:
                if step in ("local", "remote", "copy"):
                    path = root / "cache" / cache["filename"] if step == "local" else info["url"]
                    item.update(
                        drain(
                            path, info, sampling, remote=step != "local", copy_only=step == "copy"
                        )
                    )
                elif step == "proxy":
                    with bounded_proxy(info) as (path, requests):
                        item.update(drain(path, info, sampling, remote=True))
                        item["upstream_requests"] = requests
                else:
                    item.update(transfer(info, folder, chunked=step == "chunked", expected=cache))
            except Exception as exc:  # noqa: BLE001 - retain safe failure data for every treatment
                # Exception messages may contain signed upstream URLs.
                item.update(status="failed", error_type=type(exc).__name__)
            if not item.get("sample_grid", {}).get("complete", True):
                item["warnings"] = ["Incomplete sample grid; not equivalent work"]
            if item.get("matches_cache") is False:
                item["warnings"] = ["Downloaded bytes differ from the validated cache"]
            result["results"].append(item)
            (folder / "result.json").write_text(
                json.dumps(result, indent=2) + "\n", encoding="utf-8"
            )
            print(json.dumps(item), flush=True)
    print(f"Retained results: {folder / 'result.json'}")
    return int(
        any(
            item["status"] != "completed"
            or not item.get("sample_grid", {}).get("complete", True)
            or item.get("matches_cache") is False
            for item in result["results"]
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
