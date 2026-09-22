"""Validated configuration; paths are relative to the YAML file."""

import math
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from .settings import effective_scan, merge_settings, query_settings

DEFAULT_PROFILE = {
    "roi": {"x": 0.52, "y": 0.02, "width": 0.47, "height": 0.30},
    "row_count": 6,
    "row_height": 0.145,
    "row_step": 0.155,
    "padding": {"left": 8, "right": 8, "top": 2, "bottom": 2},
}


@dataclass
class Config:
    path: Path
    data: dict

    @property
    def root(self):
        return self.resolve(self.data.get("output_dir", "output"))

    def resolve(self, value):
        return (self.path.parent / Path(value).expanduser()).resolve()

    def effective_scan(self, overrides=None, adapters=None):
        return effective_scan(self, overrides, adapters)

    def profile(self, channel=None, override=None):
        if self.data.get("pipeline", "detection") != "legacy_hud":
            raise ValueError("HUD profiles require pipeline: legacy_hud")
        name = override or self.data.get("channels", {}).get(channel, {}).get(
            "profile", self.data.get("default_profile", "default_tf2_720p")
        )
        return name, self.data["profiles"][name]

    @property
    def aliases(self):
        query = self.data.get("query", {})
        return list(dict.fromkeys([query.get("target_name", "")] + query.get("aliases", [])))


def positive(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be a finite positive number")


def load_config(path="config.yaml"):
    path = Path(path).resolve()
    with path.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    if not isinstance(data, dict):
        raise TypeError("Configuration must be a mapping")
    pipeline = data.setdefault("pipeline", "detection")
    if pipeline not in ("detection", "legacy_hud"):
        raise ValueError("pipeline must be detection or legacy_hud")
    if pipeline != "legacy_hud" and any(key in data for key in ("profiles", "default_profile", "ocr")):
        raise ValueError("Old HUD/OpenOCR configuration requires pipeline: legacy_hud")
    merge_settings(data)
    query_settings(data)
    if pipeline == "legacy_hud":
        data.setdefault("profiles", {"default_tf2_720p": DEFAULT_PROFILE})
    for name, profile in data.get("profiles", {}).items():
        roi = profile["roi"]
        for axis, extent in (("x", "width"), ("y", "height")):
            if not (0 <= roi[axis] < 1 and 0 < roi[extent] <= 1 - roi[axis] + 1e-9):
                raise ValueError(f"{name}: ROI must fit inside the frame")
        count = profile["row_count"]
        if not isinstance(count, int) or count < 1:
            raise ValueError(f"{name}: row_count must be a positive integer")
        for field in ("row_height", "row_step"):
            positive(profile[field], field)
        if (count - 1) * profile["row_step"] + profile["row_height"] > 1 + 1e-9:
            raise ValueError(f"{name}: rows extend beyond ROI")
        if any(not isinstance(v, int) or v < 0 for v in profile.get("padding", {}).values()):
            raise ValueError("Padding must be nonnegative integer pixels")
    scan = data.setdefault("scan", {})
    for field, default in (("fps", 1), ("batch_size", 32), ("upscale", 2), ("cluster_gap_s", 8)):
        scan.setdefault(field, default)
        positive(scan[field], field)
    if not isinstance(scan["batch_size"], int):
        raise TypeError("batch_size must be an integer")
    if scan.get("interpolation", "bicubic") not in ("nearest", "bilinear", "bicubic", "lanczos"):
        raise ValueError("Unknown interpolation method")
    if scan.get("preprocessing", "rgb") not in ("rgb", "grayscale", "contrast"):
        raise ValueError("Unknown preprocessing method")
    for key in ("cluster_similarity", "min_edge_density"):
        value = scan.get(key, 0.82 if key == "cluster_similarity" else 0.01)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise ValueError(f"{key} must be between 0 and 1")
    if data.get("ocr", {}).get("use_gpu", "auto") not in ("auto", "true", "false"):
        raise ValueError('ocr.use_gpu must be auto, "true", or "false" (quote YAML booleans)')
    filters = data.get("filters", {})
    for key in ("date_from", "date_to"):
        if filters.get(key):
            date.fromisoformat(str(filters[key]))
    for key in ("title_include", "title_exclude"):
        if filters.get(key):
            re.compile(filters[key])
    config = Config(path, data)
    if pipeline == "legacy_hud":
        config.profile()
        for channel in data.get("channels", {}):
            config.profile(channel)
    return config
