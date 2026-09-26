"""Inference-free boundary records. Coordinates always refer to source pixels."""

from dataclasses import asdict, dataclass, field
from typing import Any

from PIL import Image

Polygon = tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class AdapterMetadata:
    name: str
    version: str
    weights_hash: str | None
    runtime: str
    dependencies: dict[str, str] = field(default_factory=dict)
    preprocessing: dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Frame:
    timestamp_s: float
    sample_key: str
    source_width: int
    source_height: int
    image: Image.Image
    chunk_id: int | None = None

    def __post_init__(self):
        import math

        if not math.isfinite(self.timestamp_s) or self.timestamp_s < 0:
            raise ValueError("Frame timestamp must be finite and nonnegative")
        if any(
            isinstance(v, bool) or not isinstance(v, int) or v <= 0
            for v in (self.source_width, self.source_height)
        ):
            raise ValueError("Source dimensions must be positive integers")
        if not self.sample_key:
            raise ValueError("Frame sample identity is required")

    def __iter__(self):
        # Transitional timestamp/image unpacking for legacy scanner and calibration.
        return iter((self.timestamp_s, self.image))

    def __getitem__(self, index):
        return (self.timestamp_s, self.image)[index]

    @property
    def working_to_source(self):
        return (self.source_width / self.image.width, self.source_height / self.image.height)


@dataclass(frozen=True)
class Detection:
    identity: str
    polygon: Polygon
    confidence: float


@dataclass(frozen=True)
class PreparedCrop:
    detection: Detection
    image: Image.Image
    # Homogeneous 3x3 mapping from padded crop pixels to source pixels.
    crop_to_source: tuple[tuple[float, ...], ...]
