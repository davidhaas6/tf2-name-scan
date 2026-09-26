"""Deterministic, target-independent proposal and Unicode text retention."""

import math
import unicodedata

from .crops import prepare_crop


def area(points):
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:] + points[:1]))) / 2


def overlap(a, b):
    """Convex polygon IoU and intersection / smaller area (not bounding boxes)."""
    output = list(a)
    for p, q in zip(b, b[1:] + b[:1]):
        source, output = output, []
        if not source:
            break

        def side(v, p=p, q=q):
            return (q[0] - p[0]) * (v[1] - p[1]) - (q[1] - p[1]) * (v[0] - p[0])

        previous = source[-1]
        for current in source:
            s, t = side(previous), side(current)
            if (s >= 0) != (t >= 0):
                ratio = s / (s - t)
                output.append(tuple(x + ratio * (y - x) for x, y in zip(previous, current)))
            if t >= 0:
                output.append(current)
            previous = current
    intersection = area(output)
    aa, ab = area(a), area(b)
    return intersection / (aa + ab - intersection), intersection / min(aa, ab)


def proposal_key(crop):
    d = crop.detection
    return (-d.confidence, d.polygon, d.identity)


def select_crops(frame, detections, settings):
    crops = []
    for detection in detections:
        try:
            crop = prepare_crop(
                frame,
                detection,
                min_height=settings["crops"]["min_height"],
                padding=settings["crops"]["padding"],
            )
            if detection.confidence >= settings["detector"]["confidence"]:
                crops.append(crop)
        except ValueError:
            continue
    kept, duplicates = [], 0
    for crop in sorted(crops, key=proposal_key):
        for other in kept:
            iou, contained = overlap(crop.detection.polygon, other.detection.polygon)
            if (
                iou >= settings["crops"]["duplicate_iou"]
                or contained >= settings["crops"]["containment"]
            ):
                duplicates += 1
                break
        else:
            kept.append(crop)
    cap = settings["crops"]["max_per_frame"]
    return kept[:cap], duplicates, max(0, len(kept) - cap)


def useful_text(result, floor):
    if result.confidence is not None and (
        not math.isfinite(result.confidence) or not floor <= result.confidence <= 1
    ):
        return False
    return sum(unicodedata.category(c)[0] in "LN" for c in result.text) >= 2
