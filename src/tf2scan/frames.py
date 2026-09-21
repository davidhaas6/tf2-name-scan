"""Bounded-memory FFmpeg sampling with checked process completion."""

import json
import subprocess
import tempfile

from PIL import Image


def probe(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        check=True,
    )
    data = json.loads(result.stdout)
    stream = next(s for s in data["streams"] if s["codec_type"] == "video")
    return stream, float(data.get("format", {}).get("duration", 0))


def sample_frames(path, fps=1, start=0, limit=None):
    if fps <= 0 or start < 0:
        raise ValueError("fps must be positive and start nonnegative")
    stream, _ = probe(path)
    width = max(2, round(stream["width"] * 720 / stream["height"] / 2) * 2)
    command = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-noautorotate",
        "-ss",
        str(start),
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-vf",
        f"setpts=PTS-STARTPTS,fps={fps}:start_time=0,scale={width}:720",
        "-pix_fmt",
        "rgb24",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    size = width * 720 * 3
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
                yield start + index / fps, Image.frombytes("RGB", (width, 720), bytes(data))
                index += 1
            if limit is None or index < limit:
                code = process.wait()
                completed = True
                if code:
                    errors.seek(0)
                    raise RuntimeError(errors.read().decode(errors="replace"))
        finally:
            if not completed and process.poll() is None:
                process.terminate()
            process.stdout.close()
            process.wait()
