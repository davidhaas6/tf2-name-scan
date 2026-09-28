"""Full-frame detector boundary and deterministic test adapter.

Adapters may resize internally, but must return source-pixel quadrilaterals.
No Paddle import/model construction is needed to use these contracts.
"""

import math
from typing import Protocol

from PIL import Image

from .contracts import AdapterMetadata, Detection, Frame


class TextDetector(Protocol):
    metadata: AdapterMetadata

    def detect(self, frames: list[Frame]) -> list[list[Detection]]: ...


def detector_input(frame, resize="native", limit_side_len=960):
    """Return inference image and tensor-to-source scale; offset is on Frame."""
    image = frame.image
    if resize == "min":
        if not isinstance(limit_side_len, int) or limit_side_len <= 0:
            raise ValueError("Detector minimum side must be a positive integer")
        scale = limit_side_len / min(image.size)
        image = image.resize(
            tuple(max(1, round(v * scale)) for v in image.size), Image.Resampling.BICUBIC
        )
    elif resize != "native":
        raise ValueError("Unknown detector resize policy")
    sx, sy = frame.working_to_source
    return image, (sx * frame.image.width / image.width, sy * frame.image.height / image.height)


def to_source_detection(identity, polygon, confidence, tensor_to_source, source_offset=(0, 0)):
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("Detector confidence must be finite and within [0,1]")
    sx, sy = tensor_to_source
    ox, oy = source_offset
    return Detection(
        str(identity), tuple((ox + x * sx, oy + y * sy) for x, y in polygon), confidence
    )


class FakeDetector:
    """Scripted source-coordinate results indexed by canonical sample identity."""

    metadata = AdapterMetadata("fake-detector", "1", None, "python")

    def __init__(self, results):
        self.results = results
        self.calls = []

    def detect(self, frames):
        self.calls.append(tuple(frame.sample_key for frame in frames))
        return [list(self.results.get(frame.sample_key, ())) for frame in frames]


def detect_frames(detector, frames):
    results = detector.detect(frames)
    if len(results) != len(frames):
        raise RuntimeError("Detector returned the wrong number of frame results")
    for detections in results:
        identities = [d.identity for d in detections]
        if len(set(identities)) != len(identities):
            raise RuntimeError("Detector identities must be unique within a frame")
    return results
