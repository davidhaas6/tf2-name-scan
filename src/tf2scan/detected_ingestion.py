"""Local full-frame Paddle pipeline with deterministic retention and tracking."""

import hashlib
import json
import uuid
from itertools import islice
from pathlib import Path

from .clustering import TextTracker
from .contracts import Frame
from .crops import geometry_metadata
from .detection import detect_frames
from .frames import frame_iterator, sample_frames
from .geometry import encode_polygon, geometry_key
from .lineage import insert
from .matching import NORMALIZATION_VERSION, compact, normalize
from .retention import select_crops, useful_text
from .settings import merge_settings


def recognize_frame(frame, detections, recognizer, settings):
    """Preserve detection/result association across invalid crops and OCR batches."""
    prepared, _, _ = select_crops(frame, detections, settings)
    return recognize_crops(prepared, recognizer, settings)


def recognize_crops(prepared, recognizer, settings):
    results = []
    size = settings["recognizer"]["batch_size"]
    for start in range(0, len(prepared), size):
        batch = prepared[start : start + size]
        recognized = recognizer.recognize([crop.image for crop in batch])
        if len(recognized) != len(batch):
            raise RuntimeError("Recognizer returned the wrong number of crops")
        results.extend(zip(batch, recognized))
    return results


def ingest_detected(
    store, config, video, detector, recognizer, *, fps=None, reprocess=False, frame_source=None
):
    if (
        store.rows("SELECT id FROM selected_video_scans WHERE video_id=?", (video["id"],))
        and not reprocess
    ):
        return False
    overrides = {"sampling": {"fps": fps}} if fps is not None else None
    settings = merge_settings(config.data, overrides)
    for adapter in (detector, recognizer):
        if not hasattr(adapter, "metadata"):
            raise ValueError("Detection pipeline adapters must supply reproducibility metadata")
    snapshot, signature = config.effective_scan(
        overrides, {"detector": detector.metadata, "recognizer": recognizer.metadata}
    )
    scan_id = store.start_scan(video["id"], snapshot, signature)
    with store.transaction() as db:
        chunk = insert(
            db,
            "scan_chunks",
            video_scan_id=scan_id,
            sequence_no=0,
            chunk_start_s=0,
            download_mode="local-full-frame",
        )
    folder = Path("report/assets") / (
        hashlib.sha256(video["id"].encode()).hexdigest()[:16] + "-" + uuid.uuid4().hex
    )
    absolute = store.root / folder
    absolute.mkdir(parents=True)
    source = (
        frame_source
        if frame_source is not None
        else sample_frames(
            video["local_path"],
            fps=settings["sampling"]["fps"],
            max_height=settings["sampling"]["max_height"],
            chunk_id=chunk,
        )
    )
    tracker = TextTracker(settings["clustering"])
    attempt = store.record_attempt(chunk)
    try:
        with store.transaction() as db, frame_iterator(source) as frames:
            count = 0
            while batch := list(islice(frames, settings["detector"]["batch_size"])):
                if any(not isinstance(frame, Frame) for frame in batch):
                    raise TypeError("Detection ingestion requires source-aware Frame records")
                proposals = detect_frames(detector, batch)
                for frame, detections in zip(batch, proposals):
                    if frame.chunk_id not in (None, chunk):
                        raise ValueError("Frame belongs to another chunk")
                    count += 1
                    prepared, duplicates, capped = select_crops(frame, detections, settings)
                    recognized = recognize_crops(prepared, recognizer, settings)
                    accepted = [
                        (crop, result)
                        for crop, result in recognized
                        if useful_text(result, settings["crops"]["confidence_floor"])
                    ]
                    frame_id = insert(
                        db,
                        "sampled_frames",
                        scan_chunk_id=chunk,
                        video_scan_id=scan_id,
                        sample_key=frame.sample_key,
                        timestamp_s=frame.timestamp_s,
                        source_width=frame.source_width,
                        source_height=frame.source_height,
                        working_width=frame.image.width,
                        working_height=frame.image.height,
                        raw_detection_count=len(detections),
                        duplicate_detection_count=duplicates,
                        recognition_count=len(recognized),
                        accepted_observation_count=len(accepted),
                        cap_dropped_count=capped,
                    )
                    db.execute(
                        "UPDATE video_scans SET input_width=?,input_height=? WHERE id=?",
                        (frame.source_width, frame.source_height, scan_id),
                    )
                    frame_path = None
                    if settings["evidence"]["full_frames"] and accepted:
                        frame_path = (folder / f"{frame_id}-frame.jpg").as_posix()
                        frame.image.save(store.root / frame_path, quality=85)
                        db.execute(
                            "UPDATE sampled_frames SET full_frame_path=? WHERE id=?",
                            (frame_path, frame_id),
                        )
                    matches, closed = tracker.associate(frame, accepted)
                    for identity, reason in closed.items():
                        db.execute(
                            "UPDATE text_clusters SET close_reason=? WHERE id=?", (reason, identity)
                        )
                    for index, (crop, result) in enumerate(accepted):
                        detection = crop.detection
                        polygon = encode_polygon(detection.polygon)
                        region, width, height = geometry_metadata(
                            detection.polygon, frame.source_width, frame.source_height
                        )
                        observation = insert(
                            db,
                            "observations",
                            video_scan_id=scan_id,
                            sampled_frame_id=frame_id,
                            polygon_blob=polygon,
                            geometry_key=geometry_key(detection.polygon) + ":" + detection.identity,
                            detection_identity=detection.identity,
                            crop_transform_json=json.dumps(crop.crop_to_source),
                            screen_region=region,
                            normalized_width=width,
                            normalized_height=height,
                            detector_confidence=detection.confidence,
                            raw_text=result.text,
                            normalized_text=normalize(result.text),
                            compact_text=compact(result.text),
                            ocr_confidence=result.confidence,
                        )
                        crop_path = (folder / f"{observation}-row.png").as_posix()
                        crop.image.save(store.root / crop_path)
                        db.execute(
                            "UPDATE observations SET crop_path=? WHERE id=?",
                            (crop_path, observation),
                        )
                        match = matches.get(index)
                        score = match[1] if match else 1
                        if match:
                            cluster = match[0]
                            db.execute(
                                "UPDATE text_clusters SET end_s=?,support_count=support_count+1 WHERE id=?",
                                (frame.timestamp_s, cluster),
                            )
                            best = db.execute(
                                "SELECT best_confidence FROM text_clusters WHERE id=?", (cluster,)
                            ).fetchone()[0]
                            if result.confidence is not None and (
                                best is None or result.confidence > best
                            ):
                                db.execute(
                                    """UPDATE text_clusters SET representative_observation_id=?,
                                    representative_polygon_blob=?,screen_region=?,canonical_text=?,
                                    normalized_text=?,compact_text=?,best_confidence=?,evidence_path=?,
                                    frame_path=?,evidence_timestamp_s=? WHERE id=?""",
                                    (
                                        observation,
                                        polygon,
                                        region,
                                        result.text,
                                        normalize(result.text),
                                        compact(result.text),
                                        result.confidence,
                                        crop_path,
                                        frame_path,
                                        frame.timestamp_s,
                                        cluster,
                                    ),
                                )
                        else:
                            cluster = insert(
                                db,
                                "text_clusters",
                                video_scan_id=scan_id,
                                video_id=video["id"],
                                start_s=frame.timestamp_s,
                                end_s=frame.timestamp_s,
                                representative_observation_id=observation,
                                representative_polygon_blob=polygon,
                                screen_region=region,
                                canonical_text=result.text,
                                normalized_text=normalize(result.text),
                                compact_text=compact(result.text),
                                best_confidence=result.confidence,
                                support_count=1,
                                model_version=recognizer.metadata.version,
                                normalization_version=NORMALIZATION_VERSION,
                                evidence_path=crop_path,
                                frame_path=frame_path,
                                evidence_timestamp_s=frame.timestamp_s,
                            )
                        track = tracker.observe(cluster, frame, crop, result)
                        db.execute(
                            "UPDATE text_clusters SET motion_summary_json=? WHERE id=?",
                            (json.dumps(track.motion()), cluster),
                        )
                        insert(
                            db,
                            "cluster_observations",
                            video_scan_id=scan_id,
                            cluster_id=cluster,
                            observation_id=observation,
                            support_score=score,
                        )
                    db.execute(
                        "UPDATE scan_chunks SET last_processed_timestamp_s=? WHERE id=?",
                        (frame.timestamp_s, chunk),
                    )
            for identity in tracker.active:
                db.execute(
                    "UPDATE text_clusters SET close_reason='end_of_scan' WHERE id=?", (identity,)
                )
            if not count:
                raise RuntimeError("No video frames decoded")
            db.execute(
                "UPDATE scan_chunks SET status='completed',completed_at=CURRENT_TIMESTAMP WHERE id=?",
                (chunk,),
            )
            db.execute(
                "UPDATE chunk_attempts SET completed_at=CURRENT_TIMESTAMP WHERE id=?", (attempt,)
            )
            db.execute(
                "UPDATE videos SET status='scanned',error=NULL,scanned_at=CURRENT_TIMESTAMP WHERE id=?",
                (video["id"],),
            )
        store.finish_scan(scan_id)
    except BaseException as exc:
        store.finish_scan(scan_id, str(exc))
        with store.transaction() as db:
            db.execute(
                "UPDATE scan_chunks SET status='failed',error=? WHERE id=?", (str(exc), chunk)
            )
            db.execute(
                "UPDATE chunk_attempts SET error=?,completed_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(exc), attempt),
            )
        for path in absolute.iterdir():
            path.unlink()
        absolute.rmdir()
        raise
    return True
