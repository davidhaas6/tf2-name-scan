"""All useful OCR rows enter the corpus before any username query runs."""

import hashlib
import json
import logging
import time
import uuid
from contextlib import closing
from pathlib import Path

from .clustering import Cluster, RowTracker
from .contracts import AdapterMetadata
from .frames import sample_frames
from .hud import boxes, prepare, useful
from .lineage import insert
from .matching import NORMALIZATION_VERSION, compact, normalize

log = logging.getLogger(__name__)


def ingest(store, config, video, recognizer, fps=None, reprocess=False, frame_source=None):
    if (
        store.rows("SELECT id FROM selected_video_scans WHERE video_id=?", (video["id"],))
        and not reprocess
    ):
        return False
    profile_name, profile = config.profile(video["channel_id"], video["hud_profile"])
    settings = dict(config.data["scan"])
    settings["fps"] = fps or settings["fps"]
    provenance, signature = config.effective_scan(
        {"sampling": {"fps": settings["fps"]}},
        {
            "recognizer": getattr(
                recognizer,
                "metadata",
                AdapterMetadata("legacy-recognizer", recognizer.model_version, None, "unknown"),
            )
        },
    )
    # A unique generation keeps existing evidence valid until the replacement commits.
    folder = Path("report/assets") / (
        hashlib.sha256(video["id"].encode()).hexdigest()[:16] + "-" + uuid.uuid4().hex
    )
    absolute = store.root / folder
    absolute.mkdir(parents=True)
    tracker = RowTracker(settings["cluster_gap_s"], settings.get("cluster_similarity", 0.82))
    scan_id = store.start_scan(video["id"], provenance, signature, legacy=True)
    with store.transaction() as db:
        chunk_id = insert(
            db,
            "scan_chunks",
            video_scan_id=scan_id,
            sequence_no=0,
            chunk_start_s=0,
            download_mode="legacy-whole-video",
        )
    frame_ids = {}
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
                    """INSERT INTO text_clusters
                    (video_scan_id,video_id,start_s,end_s,row_index,canonical_text,normalized_text,compact_text,
                     best_confidence,support_count,hud_profile,hud_config_json,model_version,
                     normalization_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        scan_id,
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
            representative = confidence > cluster.confidence
            if representative:
                crop.save(store.root / crop_path)
                frame.save(store.root / frame_path, quality=85)
                cluster.text, cluster.confidence = text, confidence
                db.execute(
                    """UPDATE text_clusters SET canonical_text=?,normalized_text=?,
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
                "UPDATE text_clusters SET end_s=?,support_count=? WHERE id=?",
                (timestamp, len(cluster.timestamps), cluster.id),
            )
            observation_id = insert(
                db,
                "observations",
                video_scan_id=scan_id,
                sampled_frame_id=frame_ids[timestamp],
                geometry_key=f"legacy-row:{row}",
                raw_text=text,
                normalized_text=normalize(text),
                compact_text=compact(text),
                ocr_confidence=result.confidence,
                crop_path=crop_path if representative else None,
                legacy_metadata_json=json.dumps({"row_index": row, "hud_profile": profile_name}),
            )
            insert(
                db,
                "cluster_observations",
                video_scan_id=scan_id,
                cluster_id=cluster.id,
                observation_id=observation_id,
                support_score=1,
            )
            if representative:
                db.execute(
                    "UPDATE observations SET crop_path=NULL WHERE id IN "
                    "(SELECT observation_id FROM cluster_observations WHERE cluster_id=?) "
                    "AND id!=?",
                    (cluster.id, observation_id),
                )
                db.execute(
                    "UPDATE text_clusters SET representative_observation_id=? WHERE id=?",
                    (observation_id, cluster.id),
                )
                db.execute(
                    "UPDATE sampled_frames SET full_frame_path=NULL WHERE full_frame_path=?",
                    (frame_path,),
                )
                db.execute(
                    "UPDATE sampled_frames SET full_frame_path=? WHERE id=?",
                    (frame_path, frame_ids[timestamp]),
                )
        batch.clear()

    try:
        with store.transaction() as db:
            source = (
                frame_source
                if frame_source is not None
                else sample_frames(video["local_path"], settings["fps"])
            )
            sampled = 0
            with closing(iter(source)) as frames:
                for timestamp, frame in frames:
                    sampled += 1
                    frame_ids[timestamp] = insert(
                        db,
                        "sampled_frames",
                        video_scan_id=scan_id,
                        scan_chunk_id=chunk_id,
                        sample_key=f"legacy:{timestamp!r}",
                        timestamp_s=timestamp,
                        source_width=frame.width,
                        source_height=frame.height,
                        working_width=frame.width,
                        working_height=frame.height,
                    )
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
            db.execute(
                "UPDATE scan_chunks SET status='completed',completed_at=CURRENT_TIMESTAMP WHERE id=?",
                (chunk_id,),
            )
        store.finish_scan(scan_id)
    except BaseException as exc:
        store.finish_scan(scan_id, str(exc))
        with store.transaction() as db:
            db.execute(
                "UPDATE scan_chunks SET status='failed',error=? WHERE id=?", (str(exc), chunk_id)
            )
        store.record_attempt(chunk_id, str(exc))
        for path in absolute.iterdir():
            path.unlink()
        absolute.rmdir()
        raise
    log.info("%s: retained %d observations", video["id"], count)
    return True
