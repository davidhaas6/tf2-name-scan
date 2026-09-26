"""Bounded-memory FFmpeg sampling with checked process completion."""

import json
import math
import subprocess
import tempfile
from contextlib import contextmanager

from PIL import Image

from .contracts import Frame


@contextmanager
def frame_iterator(source):
    """Close decoder generators on errors/cancellation; also accept plain iterables."""
    frames = iter(source)
    try:
        yield frames
    finally:
        close = getattr(frames, "close", None)
        if close is not None:
            close()


def probe(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        check=True,
    )
    data = json.loads(result.stdout)
    stream = next(s for s in data["streams"] if s["codec_type"] == "video")
    return stream, float(data.get("format", {}).get("duration", 0))


def sample_frames(path, fps=1, start=0, limit=None, *, max_height=720, chunk_id=None,
                  end=None, dimensions=None, http_headers=None):
    if not math.isfinite(fps) or not math.isfinite(start) or fps <= 0 or start < 0:
        raise ValueError("fps must be positive and start nonnegative")
    if isinstance(max_height, bool) or not isinstance(max_height, int) or max_height <= 0:
        raise ValueError("max_height must be a positive integer")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("limit must be a positive integer")
    if end is not None and (not math.isfinite(end) or end <= start):
        raise ValueError("end must exceed start")
    stream = dimensions if dimensions is not None else probe(path)[0]
    source_width, source_height = stream["width"], stream["height"]
    if source_width <= 0 or source_height <= 0:
        raise ValueError("Invalid video dimensions")
    height = min(source_height, max_height)
    width = max(1, round(source_width * height / source_height))
    command = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-noautorotate",
        *(["-rw_timeout", "30000000"] if str(path).startswith(("http:", "https:")) else []),
        *(["-headers", "".join(f"{key}: {value}\r\n" for key, value in
                              http_headers.items())] if http_headers else []),
        "-ss",
        str(start),
        *(["-t", str(end - start)] if end is not None else []),
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-vf",
        f"setpts=PTS-STARTPTS,fps={fps}:start_time=0,scale={width}:{height}",
        "-pix_fmt",
        "rgb24",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    size = width * height * 3
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
        completed = False
        try:
            index = 0
            while limit is None or index < limit:
                data = bytearray()
                while len(data) < size:
                    part = process.stdout.read(size - len(data))
                    if not part:
                        break
                    data.extend(part)
                if not data:
                    break
                if len(data) != size:
                    raise RuntimeError("FFmpeg produced a truncated frame")
                timestamp = start + index / fps
                if end is not None and timestamp >= end - 1e-9:
                    break
                yield Frame(
                    timestamp,
                    f"{timestamp:.9f}",
                    source_width,
                    source_height,
                    Image.frombytes("RGB", (width, height), bytes(data)),
                    chunk_id,
                )
                index += 1
            if limit is None or index < limit:
                try:
                    code = process.wait(timeout=5)
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError("FFmpeg did not finish after video EOF") from exc
                completed = True
                if code:
                    errors.seek(0)
                    detail = errors.read().decode(errors="replace").strip()
                    raise RuntimeError(detail or f"FFmpeg upstream failed (exit {code})")
        finally:
            if not completed and process.poll() is None:
                process.terminate()
            process.stdout.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
