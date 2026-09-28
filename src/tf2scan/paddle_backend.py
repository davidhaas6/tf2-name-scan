"""Lazy PaddleOCR 3.7 adapters; detector and recognizer share a Paddle device."""

import math
import os
from importlib.metadata import version
from pathlib import Path

import numpy as np
import yaml

from .contracts import AdapterMetadata
from .detection import to_source_detection
from .recognize import Recognition, file_hash
from .settings import merge_settings

MODEL_NAMES = {
    "PP-OCRv6-small-det": "PP-OCRv6_small_det",
    "PP-OCRv6-small-rec": "PP-OCRv6_small_rec",
}


def metadata(engine, name, options):
    directory = Path(engine.paddlex_predictor.model_dir)
    with (directory / "inference.yml").open(encoding="utf-8") as stream:
        actual_name = yaml.safe_load(stream)["Global"]["model_name"]
    if actual_name != name:
        raise ValueError(f"Requested model {name}, but loaded artifacts identify {actual_name}")
    dependencies = {p: version(p) for p in ("paddleocr", "paddlex", "paddlepaddle", "numpy")}
    return AdapterMetadata(
        name,
        dependencies["paddleocr"],
        file_hash(directory),
        "paddle",
        dependencies,
        {**options, "model_directory": str(directory)},
    )


class PaddleDetector:
    def close(self):
        self.engine.close()

    def __init__(self, engine, settings):
        self.engine, self.settings = engine, settings
        self.metadata = metadata(
            engine,
            MODEL_NAMES.get(settings["name"], settings["name"]),
            {**settings, "color": "BGR", "stride_rounding": 32},
        )

    def detect(self, frames):
        output = []
        # Native inputs can differ in size. Keep per-image resize policy exact;
        # inference objects are reused for the whole worker.
        for frame in frames:
            image = np.asarray(frame.image.convert("RGB"))[:, :, ::-1].copy()
            minimum = self.settings["resize"] == "min"
            results = list(
                self.engine.predict(
                    image,
                    batch_size=1,
                    limit_type="min" if minimum else "max",
                    limit_side_len=self.settings["limit_side_len"]
                    if minimum
                    else max(frame.image.size),
                    max_side_limit=max(4000, max(frame.image.size)),
                    box_thresh=self.settings["confidence"],
                )
            )
            if len(results) != 1:
                raise RuntimeError("Paddle detector returned the wrong number of frames")
            polygons, scores = results[0]["dt_polys"], results[0]["dt_scores"]
            if len(polygons) != len(scores):
                raise RuntimeError("Paddle detector polygon/score count mismatch")
            output.append(
                [
                    to_source_detection(i, p, float(s), frame.working_to_source,
                                        frame.source_offset)
                    for i, (p, s) in enumerate(zip(polygons, scores))
                ]
            )
        return output


class PaddleRecognizer:
    def close(self):
        self.engine.close()

    def __init__(self, engine, settings):
        self.engine, self.settings = engine, settings
        self.metadata = metadata(
            engine,
            MODEL_NAMES.get(settings["name"], settings["name"]),
            {**settings, "color": "BGR"},
        )
        self.model_version = self.metadata.version

    def recognize(self, crops):
        output = []
        size = self.settings["batch_size"]
        for start in range(0, len(crops), size):
            batch = crops[start : start + size]
            results = list(
                self.engine.predict(
                    [np.asarray(c.convert("RGB"))[:, :, ::-1].copy() for c in batch],
                    batch_size=size,
                )
            )
            if len(results) != len(batch):
                raise RuntimeError("Paddle recognizer returned the wrong number of crops")
            for result in results:
                score = float(result["rec_score"])
                if not math.isfinite(score) or not 0 <= score <= 1:
                    raise RuntimeError("Paddle recognizer returned an invalid confidence")
                output.append(Recognition(result["rec_text"], score))
        return output


def create_models(config):
    settings = merge_settings(config.data)
    if any(settings[s]["backend"] != "paddle" for s in ("detector", "recognizer")):
        raise ValueError("Local detection CLI requires Paddle detector and recognizer")
    if settings["detector"]["device"] != settings["recognizer"]["device"]:
        raise ValueError("Paddle detector and recognizer must use the same device")
    os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(config.resolve("models/paddlex")))
    try:
        from paddleocr import TextDetection, TextRecognition
    except ImportError as exc:
        raise RuntimeError("Install the Paddle runtime: uv sync --extra paddle") from exc

    def construct(cls, section):
        s = settings[section]
        return cls(
            model_name=MODEL_NAMES.get(s["name"], s["name"]),
            model_dir=str(config.resolve(s["weights"])) if s["weights"] else None,
            device=s["device"],
            enable_mkldnn=s["enable_mkldnn"],
        )

    detector = construct(TextDetection, "detector")
    recognizer = None
    try:
        recognizer = construct(TextRecognition, "recognizer")
        return PaddleDetector(detector, settings["detector"]), PaddleRecognizer(
            recognizer, settings["recognizer"]
        )
    except BaseException:
        if recognizer is not None:
            recognizer.close()
        detector.close()
        raise
