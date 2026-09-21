"""Reproducible crop benchmark with source-grouped occurrence metrics."""

import json
import statistics
import time
import tracemalloc
from collections import defaultdict
from pathlib import Path

from PIL import Image
from rapidfuzz.distance import Levenshtein

from .hud import prepare
from .matching import alias_score, normalize, promoted
from .report import atomic_write


def benchmark(manifest, recognizer, settings, output):
    manifest = Path(manifest)
    records = [
        json.loads(line)
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not records:
        raise ValueError("Benchmark manifest is empty; add verified crops first")
    splits = {}
    for record in records:
        for field in ("image", "source_id", "visible_names"):
            if field not in record:
                raise ValueError(f"Manifest record missing {field}")
        split = record.get("split", "test")
        if splits.setdefault(record["source_id"], split) != split:
            raise ValueError(f"Source {record['source_id']} leaks across splits")
    aliases = sorted({name for record in records for name in record["visible_names"]})
    predictions, latencies = [], []
    tracemalloc.start()
    started = time.perf_counter()
    batch_size = settings["batch_size"]
    for start in range(0, len(records), batch_size):
        crops = []
        for record in records[start : start + batch_size]:
            with Image.open(manifest.parent / record["image"]) as image:
                crops.append(prepare(image.convert("RGB"), settings))
        batch_start = time.perf_counter()
        results = recognizer.recognize(crops)
        latencies.append(time.perf_counter() - batch_start)
        if len(results) != len(crops):
            raise RuntimeError("Recognizer returned the wrong number of benchmark results")
        predictions.extend(results)
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    pairs, texts, groups = [], [], defaultdict(list)
    occurrences = defaultdict(list)
    durations = {}
    for index, (record, prediction) in enumerate(zip(records, predictions)):
        if "text" in record:
            expected, actual = normalize(record["text"]), normalize(prediction.text)
            texts.append(
                (expected == actual, Levenshtein.distance(expected, actual), len(expected))
            )
        if "source_duration_s" in record:
            durations[record["source_id"]] = record["source_duration_s"]
        for alias in aliases:
            positive = alias in record["visible_names"]
            score = alias_score(prediction.text, alias)
            pair = {
                "source_id": record["source_id"],
                "image": record["image"],
                "alias": alias,
                "positive": positive,
                "score": score,
                "text": prediction.text,
            }
            pairs.append(pair)
            for key in ("resolution", "hud", "compression", "crowded"):
                groups[f"{key}={record.get(key, 'unknown')}"].append(pair)
            groups[f"alias_length={len(alias)}"].append(pair)
            occurrence = str(record.get("occurrence_id", record["image"]))
            occurrences[(record["source_id"], occurrence, alias)].append(
                (record.get("timestamp_s", index), score, positive)
            )

    def metrics(values, threshold):
        tp = sum(p["positive"] and p["score"] >= threshold for p in values)
        fp = sum(not p["positive"] and p["score"] >= threshold for p in values)
        positives = sum(p["positive"] for p in values)
        return {
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / positives if positives else None,
            "true_positives": tp,
            "false_positives": fp,
            "positives": positives,
        }

    occurrence_tp = occurrence_fp = occurrence_positive = 0
    positive_videos, found_videos = set(), set()
    for (source, _, alias), values in occurrences.items():
        positive = any(p for _, _, p in values)
        accepted = promoted([(t, score) for t, score, _ in values])
        occurrence_positive += positive
        occurrence_tp += positive and accepted
        occurrence_fp += not positive and accepted
        if positive:
            positive_videos.add((source, alias))
            if accepted:
                found_videos.add((source, alias))
    # Each alias is an independent query over the annotated source windows.
    hours = sum(durations.values()) / 3600 * len(aliases)
    complete_durations = len(durations) == len(splits)
    result = {
        "model_version": recognizer.model_version,
        "settings": settings,
        "crops": len(records),
        "wall_seconds": elapsed,
        "crops_per_second": len(records) / elapsed,
        "mean_batch_latency_s": statistics.mean(latencies),
        "python_peak_bytes": peak,
        "exact_match": sum(t[0] for t in texts) / len(texts) if texts else None,
        "character_error_rate": sum(t[1] for t in texts) / max(1, sum(t[2] for t in texts))
        if texts
        else None,
        "thresholds": {str(t): metrics(pairs, t) for t in (0.65, 0.82, 0.95, 1.0)},
        "groups": {key: metrics(value, 0.95) for key, value in groups.items()},
        "occurrence_recall": occurrence_tp / occurrence_positive if occurrence_positive else None,
        "video_level_recall": len(found_videos) / len(positive_videos) if positive_videos else None,
        "false_candidates_per_query_video_hour": occurrence_fp / hours
        if hours and complete_durations
        else None,
        "candidates_per_query_video_hour": (occurrence_tp + occurrence_fp) / hours
        if hours and complete_durations
        else None,
        "notes": [
            "Durations must describe exhaustively annotated windows, not whole sparsely labeled videos.",
            "Occurrence IDs group adjacent crops; absent IDs mean one occurrence per image.",
            "Python peak excludes native tensors and GPU memory; crop throughput excludes video decoding.",
        ],
    }
    atomic_write(output, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    atomic_write(
        Path(output).with_suffix(".scores.jsonl"),
        "".join(json.dumps(pair, ensure_ascii=False) + "\n" for pair in pairs),
    )
    return result
