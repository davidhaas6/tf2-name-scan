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
