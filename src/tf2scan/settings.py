"""Effective scan identity and provisional, evaluation-tunable defaults."""

import copy
import hashlib
import json
import math

from .matching import NORMALIZATION_VERSION

DEFAULTS = {
    "sampling": {"fps": 1, "chunk_seconds": 600, "max_height": 720},
    "detector": {
        "backend": "paddle",
        "name": "PP-OCRv6-small-det",
        "version": "unknown",
        "weights": None,
        "device": "cpu",
        "batch_size": 1,
        "confidence": 0.3,
        "resize": "native",
        "limit_side_len": 960,
    },
    "recognizer": {
        "backend": "paddle",
        "name": "PP-OCRv6-small-rec",
        "version": "unknown",
        "weights": None,
        "device": "cpu",
        "batch_size": 32,
    },
    "crops": {
        "min_height": 6,
        "duplicate_iou": 0.85,
        "containment": 0.90,
        "max_per_frame": 64,
        "padding": 2,
        "confidence_floor": 0.1,
    },
    "clustering": {
        "gap_s": 3,
        "similarity": 0.82,
        "center_distance": 0.05,
        "motion_tolerance": 0.5,
    },
    "acquisition": {
        "attempts": 3,
        "backoff_s": 2,
        "max_backoff_s": 30,
        "streams_per_host": 1,
        "overlap_s": 3,
        "cache_ttl_s": 0,
    },
    "persistence": {
        "commit_interval_s": 20,
        "max_rows": 10000,
        "max_bytes": 16777216,
        "wal_checkpoint_pages": 1000,
        "wal_size_bytes": 67108864,
    },
    "evidence": {
        "policy": "representative",
        "full_frames": False,
        "max_candidates": 4,
        "compact": True,
    },
}
MATCHING = {"strong": 0.95, "weak": 0.82, "gap_s": 3, "short_name_length": 4}


def canonical_json(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def merge_settings(data, overrides=None):
    result = copy.deepcopy(DEFAULTS)
    for section, defaults in result.items():
        supplied = data.get(section, {})
        if not isinstance(supplied, dict) or set(supplied) - set(defaults):
            raise ValueError(f"Unknown or invalid {section} settings")
        defaults.update(supplied)
    if overrides:
        for section, values in overrides.items():
            if section not in result or set(values) - set(result[section]):
                raise ValueError(f"Unknown override: {section}")
            result[section].update(values)
    validate(result)
    return result


def validate(settings):
    nonnegative = {"padding", "cache_ttl_s", "overlap_s"}
    thresholds = {
        "confidence",
        "confidence_floor",
        "duplicate_iou",
        "containment",
        "similarity",
        "center_distance",
        "motion_tolerance",
    }
    for section, values in settings.items():
        for key, value in values.items():
            default = DEFAULTS[section][key]
            label = f"{section}.{key}"
            if isinstance(default, bool):
                if not isinstance(value, bool):
                    raise TypeError(f"{label} must be boolean")
            elif isinstance(default, (int, float)):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise TypeError(f"{label} must be numeric")
                if (
                    not math.isfinite(value)
                    or value < 0
                    or (value == 0 and key not in nonnegative | thresholds)
                ):
                    raise ValueError(f"{label} has invalid range")
                if (
                    isinstance(default, int)
                    and key
                    not in {
                        "fps",
                        "gap_s",
                        "min_height",
                        "chunk_seconds",
                        "backoff_s",
                        "max_backoff_s",
                        "overlap_s",
                        "cache_ttl_s",
                        "commit_interval_s",
                    }
                    and not isinstance(value, int)
                ):
                    raise ValueError(f"{label} must be an integer")
                if key in thresholds and value > 1:
                    raise ValueError(f"{label} must be between 0 and 1")
            elif default is None:
                if value is not None and (not isinstance(value, str) or not value):
                    raise ValueError(f"{label} must be a path or null")
            elif not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must be a nonempty string")
    if settings["detector"]["backend"] not in {"paddle", "fake"}:
        raise ValueError("Unsupported detector backend")
    if settings["recognizer"]["backend"] not in {"paddle", "openocr", "fake"}:
        raise ValueError("Unsupported recognizer backend")
    if settings["detector"]["resize"] not in {"native", "min"}:
        raise ValueError("detector.resize must be native or min")
    a = settings["acquisition"]
    if a["streams_per_host"] != 1 or a["max_backoff_s"] < a["backoff_s"]:
        raise ValueError("Invalid acquisition concurrency/backoff")
    if a["overlap_s"] >= settings["sampling"]["chunk_seconds"]:
        raise ValueError("overlap_s must be smaller than chunk_seconds")
    if not 10 <= settings["persistence"]["commit_interval_s"] <= 30:
        raise ValueError("commit_interval_s must be within 10–30 seconds")
    if settings["evidence"]["policy"] != "representative":
        raise ValueError("Only representative evidence policy is supported")


def query_settings(data):
    values = {**MATCHING, **data.get("matching", {})}
    if set(values) != set(MATCHING):
        raise ValueError("Unknown matching setting")
    for key, value in values.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError(f"Invalid matching.{key}")
    if not 0 <= values["weak"] <= values["strong"] <= 1 or values["gap_s"] <= 0:
        raise ValueError("Invalid matching thresholds/gap")
    if not isinstance(values["short_name_length"], int) or values["short_name_length"] < 1:
        raise ValueError("Invalid short_name_length")
    return values


def effective_scan(config, overrides=None, adapters=None):
    from .recognize import file_hash

    settings = merge_settings(config.data, overrides)
    for section in ("detector", "recognizer"):
        weights = settings[section]["weights"]
        settings[section]["weights_hash"] = file_hash(config.resolve(weights)) if weights else None
    snapshot = {
        "settings": settings,
        "pipeline": config.data.get("pipeline", "detection"),
        "versions": {
            "preprocessing": "rectified-v1",
            "retention": "polygon-unicode-v1",
            "geometry": "quad-f32le-v1",
            "normalization": NORMALIZATION_VERSION,
            "clustering": (
                "legacy-row-v1" if config.data.get("pipeline") == "legacy_hud" else "geometry-motion-v1"
            ),
        },
        "adapters": {key: value.to_dict() for key, value in (adapters or {}).items()},
    }
    if snapshot["pipeline"] == "legacy_hud":
        snapshot["legacy"] = {
            key: config.data.get(key)
            for key in ("profiles", "channels", "default_profile", "scan", "ocr")
        }
    encoded = canonical_json(snapshot)
    return encoded, hashlib.sha256(encoded.encode()).hexdigest()
