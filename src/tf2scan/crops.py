"""HUD-independent perspective rectification with explicit source mappings."""

import math
from dataclasses import replace

import numpy as np
from PIL import Image

from .contracts import PreparedCrop
from .geometry import validate_polygon


def canonicalize(polygon):
    points = np.asarray(polygon, dtype=float)
    if points.shape != (4, 2) or not np.isfinite(points).all():
        raise ValueError("Expected four finite x/y pairs")
    center = points.mean(axis=0)
    angles = np.arctan2(points[:, 1] - center[1], points[:, 0] - center[0])
    points = points[np.argsort(angles)]
    # Deterministic top-left start, including equal-sum diamond corners.
    start = min(range(4), key=lambda i: (sum(points[i]), points[i, 1], points[i, 0]))
    points = np.roll(points, -start, axis=0)
    return validate_polygon(points)


def dimensions(polygon):
    p = np.asarray(polygon)
    width = max(np.linalg.norm(p[1] - p[0]), np.linalg.norm(p[2] - p[3]))
    height = min(np.linalg.norm(p[3] - p[0]), np.linalg.norm(p[2] - p[1]))
    return float(width), float(height)


def homography(destination, source):
    """Matrix mapping four destination corners to four source corners."""
    matrix, rhs = [], []
    for (x, y), (u, v) in zip(destination, source):
        matrix.extend(((x, y, 1, 0, 0, 0, -u * x, -u * y), (0, 0, 0, x, y, 1, -v * x, -v * y)))
        rhs.extend((u, v))
    return np.append(np.linalg.solve(matrix, rhs), 1).reshape(3, 3)


def prepare_crop(frame, detection, *, min_height=6, padding=2):
    """Rectify at working-image density; retain source geometry and padded mapping.

    Padding samples surrounding source pixels. At image boundaries Pillow supplies
    white pixels; the detection is never clipped or silently shifted inward.
    """
    if isinstance(padding, bool) or not isinstance(padding, int) or padding < 0:
        raise ValueError("padding must be nonnegative integer pixels")
    if not math.isfinite(min_height) or min_height <= 0:
        raise ValueError("min_height must be finite and positive")
    polygon = canonicalize(detection.polygon)
    if not math.isfinite(detection.confidence) or not 0 <= detection.confidence <= 1:
        raise ValueError("Invalid detector confidence")
    if any(x < 0 or y < 0 or x > frame.source_width or y > frame.source_height for x, y in polygon):
        raise ValueError("Detection falls outside the source frame")
    _, source_height = dimensions(polygon)
    if source_height < min_height:
        raise ValueError("Text is below the minimum source height")
    sx, sy = frame.working_to_source
    working = tuple((x / sx, y / sy) for x, y in polygon)
    w, h = dimensions(working)
    width, height = max(1, math.ceil(w)), max(1, math.ceil(h))
    corners = (
        (padding, padding),
        (padding + width, padding),
        (padding + width, padding + height),
        (padding, padding + height),
    )
    mapping = homography(corners, polygon)
    to_working = np.diag((1 / sx, 1 / sy, 1)) @ mapping
    to_working /= to_working[2, 2]
    image = frame.image.convert("RGB").transform(
        (width + 2 * padding, height + 2 * padding),
        Image.Transform.PERSPECTIVE,
        tuple(to_working.flat)[:8],
        Image.Resampling.BICUBIC,
        fillcolor="white",
    )
    return PreparedCrop(
        replace(detection, polygon=polygon),
        image,
        tuple(tuple(float(v) for v in row) for row in mapping),
    )


def geometry_metadata(polygon, source_width, source_height):
    points = np.asarray(polygon)
    cx, cy = points.mean(axis=0)
    row = min(2, int(cy / source_height * 3))
    col = min(2, int(cx / source_width * 3))
    region = ("top", "middle", "bottom")[row] + "_" + ("left", "center", "right")[col]
    width, height = np.ptp(points, axis=0)
    return region, float(width / source_width), float(height / source_height)
