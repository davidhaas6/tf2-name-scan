"""Optional wall-clock accounting for a single detected scan."""

import json
import os
import platform
import time
from collections import defaultdict
from contextlib import contextmanager


class ScanProfile:
    def __init__(self):
        self.started = time.perf_counter()
        self.seconds = defaultdict(float)
        self.counts = defaultdict(int)
        self.persistence_batches = []
        self.frames = 0
        self.crops = 0
        self.source_resolution = None
        self.working_resolution = None
        self.detector_resolution = None

    @contextmanager
    def measure(self, stage):
        started = time.perf_counter()
        try:
            yield
        finally:
            self.seconds[stage] += time.perf_counter() - started

    def result(self, video, settings, *, model_setup_s=0):
        wall = time.perf_counter() - self.started
        stages = {
            key: round(self.seconds[key], 3)
            for key in (
                "decoding",
                "detection",
                "crop_preparation",
                "recognition",
                "persistence_evidence",
                "startup_cleanup",
            )
        }
        stages["other"] = round(max(0, wall - sum(self.seconds[key] for key in stages)), 3)
        transaction = self.seconds["persistence_transaction"]
        image_save = self.seconds["evidence_image_save"]
        compaction = self.seconds["evidence_compaction"]
        reference_lookup = self.seconds["evidence_reference_lookup"]
        file_walk = self.seconds["evidence_file_walk"]
        detail = {
            "transaction_total": round(transaction, 3),
            "image_save": round(image_save, 3),
            "transaction_other": round(max(0, transaction - image_save), 3),
            "compaction_total": round(compaction, 3),
            "reference_lookup": round(reference_lookup, 3),
            "file_walk": round(file_walk, 3),
            "compaction_other": round(max(0, compaction - reference_lookup - file_walk), 3),
        }
        bottleneck = max(stages, key=stages.get)
        duration = video["duration_s"]
        return {
            "video_id": video["id"],
            "duration_s": duration,
            "wall_s": round(wall, 3),
            "realtime_factor": round(wall / duration, 3) if duration else None,
            "model_setup_s": round(model_setup_s, 3),
            "frames": self.frames,
            "crops": self.crops,
            "source_resolution": self.source_resolution,
            "working_resolution": self.working_resolution,
            "detector_resolution": self.detector_resolution,
            "hardware": {
                "cpu": platform.processor(),
                "logical_cpus": os.cpu_count(),
                "platform": platform.platform(),
            },
            "stages_s": stages,
            "largest_stage": bottleneck,
            "settings": settings,
            "persistence_detail_s": detail,
            "persistence_counts": dict(self.counts),
            "persistence_batches": self.persistence_batches,
        }


def write_profile(path, result):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
