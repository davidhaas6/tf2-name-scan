"""Narrow adapter for the official OpenOCR PyTorch recognizer."""

import hashlib
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Protocol

import numpy as np
from PIL import Image

from .contracts import AdapterMetadata


@dataclass(frozen=True)
class Recognition:
    text: str
    confidence: float | None = None


class Recognizer(Protocol):
    model_version: str
    metadata: AdapterMetadata

    def recognize(self, crops: list[Image.Image]) -> list[Recognition]: ...


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class OpenOCRRecognizer:
    def __init__(self, config):
        settings = config.data.get("ocr", {})
        for key in ("repository", "config", "checkpoint"):
            if not settings.get(key):
                raise ValueError(f"Set ocr.{key}; see docs/usage.md for SVTRv2-S setup")
        repository = config.resolve(settings["repository"])
        model_config = config.resolve(settings["config"])
        checkpoint = config.resolve(settings["checkpoint"])
        if not (repository / "tools/infer_rec.py").is_file():
            raise ValueError(f"Not an OpenOCR checkout: {repository}")
        sys.path.insert(0, str(repository))
        try:
            from tools.engine.config import Config as OCRConfig
            from tools.infer_rec import OpenRecognizer
        except ImportError as exc:
            raise RuntimeError(
                "Install OpenOCR's PyTorch dependencies in this environment"
            ) from exc
        cfg = OCRConfig(str(model_config)).cfg
        if not checkpoint.is_file():
            raise FileNotFoundError(f"Missing OpenOCR checkpoint: {checkpoint}")
        cfg["Global"]["pretrained_model"] = str(checkpoint)
        cfg["Global"]["checkpoints"] = None
        cfg["Global"]["backend"] = "torch"

        # Upstream config paths are often relative to its repository.
        def resolve_dictionaries(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "character_dict_path" and item and not Path(item).is_absolute():
                        value[key] = str(repository / item)
                    else:
                        resolve_dictionaries(item)
            elif isinstance(value, list):
                for item in value:
                    resolve_dictionaries(item)

        resolve_dictionaries(cfg)
        try:
            revision = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            )
            version = revision.stdout.strip()
        except FileNotFoundError:
            version = ""
        version = version or file_hash(repository / "tools/infer_rec.py")
        self.model_version = (
            f"OpenOCR:{version}:SVTRv2-{settings.get('variant', 'S')}:"
            f"config={file_hash(model_config)}:weights={file_hash(checkpoint)}"
        )
        dependencies = {}
        for package in ("torch", "numpy", "Pillow"):
            try:
                dependencies[package] = package_version(package)
            except PackageNotFoundError:
                dependencies[package] = "unknown"
        self.metadata = AdapterMetadata(
            f"SVTRv2-{settings.get('variant', 'S')}",
            self.model_version,
            file_hash(checkpoint),
            "torch",
            dependencies,
            {
                "config_hash": file_hash(model_config),
                "transforms": cfg["Eval"]["dataset"]["transforms"],
            },
        )
        self.engine = OpenRecognizer(
            config=cfg, backend="torch", use_gpu=settings.get("use_gpu", "auto")
        )
        self.batch_size = config.data["scan"]["batch_size"]
        transforms = cfg["Eval"]["dataset"]["transforms"]
        self.pil_input = any("DecodeImagePIL" in op for op in transforms)
        decode = next(
            (
                op.get("DecodeImage", op.get("DecodeImagePIL"))
                for op in transforms
                if "DecodeImage" in op or "DecodeImagePIL" in op
            ),
            {},
        )
        self.bgr = decode.get("img_mode", "RGB") == "BGR"

    def recognize(self, crops):
        if not crops:
            return []
        # Upstream skips the decoder for its in-memory API, even though the
        # argument is called img_numpy_list. English MSR configs expect PIL.
        images = (
            [image.convert("RGB") for image in crops]
            if self.pil_input
            else [np.asarray(image.convert("RGB")) for image in crops]
        )
        if self.bgr and not self.pil_input:
            images = [image[:, :, ::-1].copy() for image in images]
        results = self.engine(img_numpy_list=images, batch_num=self.batch_size)
        if len(results) != len(crops):
            raise RuntimeError("OpenOCR returned the wrong number of results")
        return [Recognition(result["text"], float(result["score"])) for result in results]
