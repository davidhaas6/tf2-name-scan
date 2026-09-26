"""Alias retrieval is deliberately independent of ingestion."""

import math
import re
import unicodedata
from itertools import pairwise

from rapidfuzz.distance import Levenshtein

NORMALIZATION_VERSION = "nfkc-casefold-separators-v1"
MATCHER_VERSION = "window-levenshtein-v2"


def normalize(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def compact(text):
    return re.sub(r"[\s_\-|.·:]+", "", normalize(text))


def alias_score(text, alias, short_name_length=4):
    """Compare near-length substrings, never the complete row alone."""
    best = 0.0
    short = len(compact(alias)) <= short_name_length
    for normalizer in (normalize, compact):
        haystack, needle = normalizer(text), normalizer(alias)
        if not needle:
            continue
        if needle in haystack:
            return 1.0
        if short:
            continue
        radius = max(1, math.ceil(len(needle) * 0.2))
        for length in range(max(1, len(needle) - radius), len(needle) + radius + 1):
            for start in range(len(haystack) - length + 1):
                best = max(
                    best,
                    Levenshtein.normalized_similarity(needle, haystack[start : start + length]),
                )
    return best


def promoted(scores, strong=0.95, weak=0.82, gap=3):
    """Require distinct supporting timestamps for consensus."""
    if any(score >= strong for _, score in scores):
        return True
    times = sorted({t for t, score in scores if score >= weak})
    return any(b - a <= gap for a, b in pairwise(times))
