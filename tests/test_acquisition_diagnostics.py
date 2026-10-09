"""Ensure diagnostic coverage/pixel checks agree with the production decoder."""

import hashlib
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

from tf2scan.frames import sample_frames


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg not installed")
def test_drain_matches_production_across_overlap_and_flags_short_source(tmp_path):
    pytest.importorskip("psutil")
    path = Path(__file__).resolve().parents[1] / "eval" / "attribute_acquisition.py"
    spec = importlib.util.spec_from_file_location("acquisition_diagnostics", path)
    diagnostics = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diagnostics)
    video = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x48:rate=10",
            "-t",
            "4.2",
            "-c:v",
            "libx264",
            str(video),
        ],
        check=True,
    )
    info = {"width": 64, "height": 48, "duration": 4}
    sampling = {"fps": 1, "max_height": 720, "chunk_seconds": 3, "overlap_s": 1}
    actual = diagnostics.drain(video, info, sampling)
    pixels, grid = hashlib.sha256(), hashlib.sha256()
    # Production uses max(chunk start 3, saved timestamp 2) minus overlap 1.
    for start, end, eligible_start in ((0, 3, 0), (2, 4, 3)):
        for frame in sample_frames(video, fps=1, start=start, end=end):
            if eligible_start <= frame.timestamp_s < end:
                pixels.update(frame.image.tobytes())
                grid.update(f"{frame.timestamp_s:.9f}\n".encode())
    assert actual["sample_grid"]["complete"]
    assert actual["sample_grid"]["frames"] == 4
    assert actual["sample_grid"]["sha256"] == grid.hexdigest()
    assert actual["rgb_sha256"] == pixels.hexdigest()
    assert [section["start_s"] for section in actual["sections"]] == [0, 2]
    assert sum(section["frames"] for section in actual["sections"]) == 5
    short = diagnostics.drain(video, {**info, "duration": 8}, {**sampling, "chunk_seconds": 10})
    assert not short["sample_grid"]["complete"]
    assert short["sample_grid"]["frames"] < short["sample_grid"]["expected_frames"]
