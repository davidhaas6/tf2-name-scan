"""All useful OCR rows enter the corpus before any username query runs."""

import hashlib
import json
import logging
import time
import uuid
from contextlib import closing
from pathlib import Path

from .clustering import Cluster, RowTracker
from .frames import sample_frames
from .hud import boxes, prepare, useful
from .matching import NORMALIZATION_VERSION, compact, normalize

log = logging.getLogger(__name__)


def ingest(store, config, video, recognizer, fps=None, reprocess=False, frame_source=None):
    if video["status"] == "scanned" and not reprocess:
        return False
    profile_name, profile = config.profile(video["channel_id"], video["hud_profile"])
    settings = dict(config.data["scan"])
    settings["fps"] = fps or settings["fps"]
    provenance = json.dumps(
        {
            "profile": profile,
            "settings": settings,
            "model": recognizer.model_version,
            "normalization": NORMALIZATION_VERSION,
        },
        sort_keys=True,
    )
    signature = hashlib.sha256(provenance.encode()).hexdigest()
    # A unique generation keeps existing evidence valid until the replacement commits.
    folder = Path("report/assets") / (
        hashlib.sha256(video["id"].encode()).hexdigest()[:16] + "-" + uuid.uuid4().hex
    )
    absolute = store.root / folder
    absolute.mkdir(parents=True)
    tracker = RowTracker(settings["cluster_gap_s"], settings.get("cluster_similarity", 0.82))
    old_paths = store.rows(
        "SELECT evidence_path,frame_path FROM row_clusters WHERE video_id=?", (video["id"],)
    )
    started = time.perf_counter()
    count = 0
    batch = []

    def flush(db):
        nonlocal count
        if not batch:
            return
        results = recognizer.recognize([prepare(item[3], settings) for item in batch])
        if len(results) != len(batch):
            raise RuntimeError("Recognizer returned the wrong number of rows")
        for (timestamp, row, frame, crop), result in zip(batch, results):
            text = result.text
            if not normalize(text):
                continue
            count += 1
            confidence = result.confidence if result.confidence is not None else -1.0
            cluster = tracker.find(timestamp, row, text)
            if cluster is None:
                cursor = db.execute(
                    """INSERT INTO row_clusters
                    (video_id,start_s,end_s,row_index,canonical_text,normalized_text,compact_text,
                     best_confidence,support_count,hud_profile,hud_config_json,model_version,
                     normalization_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        video["id"],
                        timestamp,
                        timestamp,
                        row,
                        text,
                        normalize(text),
                        compact(text),
                        result.confidence,
                        1,
                        profile_name,
                        json.dumps(profile, sort_keys=True),
                        recognizer.model_version,
                        NORMALIZATION_VERSION,
                    ),
                )
                cluster = Cluster(cursor.lastrowid, timestamp, row, text, -2.0)
                tracker.active.append(cluster)
            crop_path = (folder / f"{cluster.id}-row.png").as_posix()
            frame_path = (folder / f"{cluster.id}-frame.jpg").as_posix()
            if confidence > cluster.confidence:
                crop.save(store.root / crop_path)
                frame.save(store.root / frame_path, quality=85)
                cluster.text, cluster.confidence = text, confidence
                db.execute(
                    """UPDATE row_clusters SET canonical_text=?,normalized_text=?,
                    compact_text=?,best_confidence=?,evidence_path=?,frame_path=?,
                    evidence_timestamp_s=?,evidence_row_index=? WHERE id=?""",
                    (
                        text,
                        normalize(text),
                        compact(text),
                        result.confidence,
                        crop_path,
                        frame_path,
                        timestamp,
                        row,
                        cluster.id,
                    ),
                )
            cluster.last_time, cluster.row = timestamp, row
            cluster.timestamps.add(timestamp)
            db.execute(
                "UPDATE row_clusters SET end_s=?,support_count=? WHERE id=?",
                (timestamp, len(cluster.timestamps), cluster.id),
            )
            db.execute(
                """INSERT INTO observations
                (video_id,row_cluster_id,timestamp_s,row_index,raw_text,normalized_text,
                 compact_text,ocr_confidence,model_version,normalization_version,hud_profile,crop_path)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    video["id"],
                    cluster.id,
                    timestamp,
                    row,
                    text,
                    normalize(text),
                    compact(text),
                    result.confidence,
                    recognizer.model_version,
                    NORMALIZATION_VERSION,
                    profile_name,
                    crop_path,
                ),
            )
        batch.clear()

    try:
        with store.transaction() as db:
            db.execute("DELETE FROM row_clusters WHERE video_id=?", (video["id"],))
            source = (
                frame_source
                if frame_source is not None
                else sample_frames(video["local_path"], settings["fps"])
            )
            sampled = 0
            with closing(iter(source)) as frames:
                for timestamp, frame in frames:
                    sampled += 1
                    for row, box in enumerate(boxes(frame.size, profile)[1]):
                        crop = frame.crop(box)
                        if useful(
                            crop,
                            settings.get("min_contrast", 4),
                            settings.get("min_edge_density", 0.01),
                        ):
                            batch.append((timestamp, row, frame, crop))
                            if len(batch) >= settings["batch_size"]:
                                flush(db)
                flush(db)
            if not sampled:
                raise RuntimeError("No video frames decoded")
            db.execute(
                """UPDATE videos SET status='scanned',error=NULL,
                scanned_at=CURRENT_TIMESTAMP,scan_signature=?,scan_seconds=?,
                scan_config_json=?,hud_profile=? WHERE id=?""",
                (signature, time.perf_counter() - started, provenance, profile_name, video["id"]),
            )
    except BaseException:
        for path in absolute.iterdir():
            path.unlink()
        absolute.rmdir()
        raise
    for paths in old_paths:
        for value in paths.values():
            if value:
                path = (store.root / value).resolve()
                if path.is_relative_to(store.root.resolve()):
                    path.unlink(missing_ok=True)
    log.info("%s: retained %d observations", video["id"], count)
    return True
