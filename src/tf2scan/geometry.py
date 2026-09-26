"""Versioned source quadrilaterals; replay keys are deliberately not overlap tests."""

import math
import struct

VERSION = "quad-f32le-v1"


def validate_polygon(points):
    if len(points) != 4 or any(len(p) != 2 for p in points):
        raise ValueError("Expected four x/y pairs")
    points = tuple(tuple(float(v) for v in p) for p in points)
    if any(not math.isfinite(v) for p in points for v in p):
        raise ValueError("Polygon coordinates must be finite")
    crosses = []
    for i in range(4):
        a, b, c = points[i], points[(i + 1) % 4], points[(i + 2) % 4]
        crosses.append((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]))
    if any(c <= 1e-6 for c in crosses):
        raise ValueError("Expected convex, nondegenerate clockwise image-coordinate winding")
    return points


def encode_polygon(points):
    points = validate_polygon(points)
    blob = struct.pack("<8f", *(v for p in points for v in p))
    decode_polygon(blob)  # Reject overflow or degeneration after float32 conversion.
    return blob


def decode_polygon(blob, version=VERSION):
    if version != VERSION or len(blob) != 32:
        raise ValueError("Unsupported geometry codec or blob length")
    values = struct.unpack("<8f", blob)
    return validate_polygon(tuple(zip(values[::2], values[1::2])))


def geometry_key(points):
    # Deterministic 1/16 source-pixel lattice; geometric replay reconciliation is R07.
    return ",".join(str(round(v * 16)) for p in validate_polygon(points) for v in p)
