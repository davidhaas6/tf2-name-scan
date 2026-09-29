"""Bounded detector scanning with durable frame batches and resumable lineage."""

import hashlib
import json
import logging
import math
import time
import uuid
from pathlib import Path

from .clustering import TextTracker
from .contracts import Frame
from .crops import geometry_metadata
from .detection import detect_frames
from .download import resolve_stream
from .frames import detector_frame, frame_iterator, probe, sample_frames
from .geometry import encode_polygon, geometry_key
from .lineage import insert
from .matching import NORMALIZATION_VERSION, compact, normalize
from .retention import select_crops, useful_text
from .settings import merge_settings

log = logging.getLogger(__name__)


def recognize_crops(prepared, recognizer, settings, profile=None):
    results = []
    size = settings["recognizer"]["batch_size"]
    for start in range(0, len(prepared), size):
        batch = prepared[start : start + size]
        if profile:
            with profile.measure("recognition"):
                recognized = recognizer.recognize([crop.image for crop in batch])
        else:
            recognized = recognizer.recognize([crop.image for crop in batch])
        if len(recognized) != len(batch):
            raise RuntimeError("Recognizer returned the wrong number of crops")
        results.extend(zip(batch, recognized))
    return results


def recognize_frame(frame, detections, recognizer, settings):
    prepared, _, _ = select_crops(frame, detections, settings)
    return recognize_crops(prepared, recognizer, settings)


def _prepare_frame(frame, detections, recognizer, settings, profile=None):
    if profile:
        with profile.measure("crop_preparation"):
            prepared, duplicates, capped = select_crops(frame, detections, settings)
        profile.crops += len(prepared)
    else:
        prepared, duplicates, capped = select_crops(frame, detections, settings)
    recognized = recognize_crops(prepared, recognizer, settings, profile)
    accepted = [
        (crop, result)
        for crop, result in recognized
        if useful_text(result, settings["crops"]["confidence_floor"])
    ]
    return frame, len(detections), duplicates, capped, len(recognized), accepted


def _prepare_frames(frames, proposals, recognizer, settings, profile=None):
    """Recognize one ordered crop pool, then restore each frame's result slice."""
    prepared_frames = []
    pooled = []
    for frame, detections in zip(frames, proposals):
        if profile:
            with profile.measure("crop_preparation"):
                crops, duplicates, capped = select_crops(frame, detections, settings)
            profile.crops += len(crops)
        else:
            crops, duplicates, capped = select_crops(frame, detections, settings)
        prepared_frames.append((frame, len(detections), duplicates, capped, len(crops)))
        pooled.extend(crops)
    recognized = recognize_crops(pooled, recognizer, settings, profile)
    entries = []
    offset = 0
    for frame, raw_count, duplicates, capped, count in prepared_frames:
        accepted = [
            (crop, result)
            for crop, result in recognized[offset : offset + count]
            if useful_text(result, settings["crops"]["confidence_floor"])
        ]
        entries.append((frame, raw_count, duplicates, capped, count, accepted))
        offset += count
    return entries


def _chunks(duration, seconds):
    for sequence in range(math.ceil(duration / seconds)):
        start = sequence * seconds
        yield sequence, start, min(duration, start + seconds)


def _save_image(image, path, **options):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        image.save(temporary, format="JPEG" if path.suffix == ".jpg" else "PNG", **options)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _save_profiled_image(image, path, profile=None, **options):
    if profile:
        with profile.measure("evidence_image_save"):
            _save_image(image, path, **options)
        profile.counts["images_saved"] += 1
    else:
        _save_image(image, path, **options)


def _commit_batch(
    store,
    scan_id,
    chunk_id,
    video_id,
    folder,
    tracker,
    entries,
    recognizer,
    settings,
    profile=None,
):
    """Inference finishes before entering a write transaction; watermark commits last."""
    transaction_started = time.perf_counter() if profile else None
    changed_clusters = set()
    if profile:
        profile.counts["batch_commits"] += 1
        keys = (
            "persistence_transaction",
            "evidence_image_save",
            "evidence_compaction",
            "evidence_reference_lookup",
            "evidence_file_walk",
        )
        before = {key: profile.seconds[key] for key in keys}
        files_before = profile.counts["asset_files_checked"]
    with store.transaction() as db:
        for frame, raw_count, duplicates, capped, recognition_count, accepted in entries:
            if frame.chunk_id not in (None, chunk_id):
                raise ValueError("Frame belongs to another chunk")
            if frame.timestamp_s <= tracker.last_time:
                continue
            frame_id = insert(
                db,
                "sampled_frames",
                scan_chunk_id=chunk_id,
                video_scan_id=scan_id,
                sample_key=frame.sample_key,
                timestamp_s=frame.timestamp_s,
                source_width=frame.source_width,
                source_height=frame.source_height,
                working_width=frame.image.width,
                working_height=frame.image.height,
                raw_detection_count=raw_count,
                duplicate_detection_count=duplicates,
                recognition_count=recognition_count,
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
                _save_profiled_image(frame.image, store.root / frame_path, profile, quality=85)
                db.execute(
                    "UPDATE sampled_frames SET full_frame_path=? WHERE id=?", (frame_path, frame_id)
                )
            matches, closed = tracker.associate(frame, accepted)
            for identity, reason in closed.items():
                db.execute("UPDATE text_clusters SET close_reason=? WHERE id=?", (reason, identity))
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
                    representative = result.confidence is not None and (
                        best is None or result.confidence > best
                    )
                else:
                    representative = True
                crop_path = None
                if representative:
                    crop_path = (folder / f"{observation}-row.png").as_posix()
                    _save_profiled_image(crop.image, store.root / crop_path, profile)
                    db.execute(
                        "UPDATE observations SET crop_path=? WHERE id=?", (crop_path, observation)
                    )
                if match:
                    if representative:
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
                        video_id=video_id,
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
                changed_clusters.add(cluster)
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
                (frame.timestamp_s, chunk_id),
            )
    if profile:
        profile.seconds["persistence_transaction"] += time.perf_counter() - transaction_started
    if settings["evidence"]["compact"]:
        if profile:
            profile.counts["compaction_calls"] += 1
            with profile.measure("evidence_compaction"):
                store.compact_evidence(
                    scan_id,
                    profile=profile,
                    cluster_ids=changed_clusters,
                )
        else:
            store.compact_evidence(
                scan_id,
                cluster_ids=changed_clusters,
            )
    if profile:
        profile.persistence_batches.append(
            {
                "frames": len(entries),
                "accepted_observations": sum(len(entry[-1]) for entry in entries),
                "transaction_s": round(
                    profile.seconds["persistence_transaction"] - before["persistence_transaction"],
                    3,
                ),
                "image_save_s": round(
                    profile.seconds["evidence_image_save"] - before["evidence_image_save"], 3
                ),
                "compaction_s": round(
                    profile.seconds["evidence_compaction"] - before["evidence_compaction"], 3
                ),
                "reference_lookup_s": round(
                    profile.seconds["evidence_reference_lookup"]
                    - before["evidence_reference_lookup"],
                    3,
                ),
                "file_walk_s": round(
                    profile.seconds["evidence_file_walk"] - before["evidence_file_walk"], 3
                ),
                "asset_files_checked": profile.counts["asset_files_checked"] - files_before,
            }
        )


def ingest_detected(
    store,
    config,
    video,
    detector,
    recognizer,
    *,
    fps=None,
    reprocess=False,
    frame_source=None,
    profile=None,
):
    overrides = {"sampling": {"fps": fps}} if fps is not None else None
    settings = merge_settings(config.data, overrides)
    for adapter in (detector, recognizer):
        if not hasattr(adapter, "metadata"):
            raise ValueError("Detection adapters must supply reproducibility metadata")
    snapshot, signature = config.effective_scan(
        overrides, {"detector": detector.metadata, "recognizer": recognizer.metadata}
    )
    unfinished = store.rows(
        "SELECT s.id,r.config_hash FROM video_scans s JOIN scan_runs r ON r.id=s.scan_run_id "
        "WHERE s.video_id=? AND s.status!='completed' ORDER BY s.id DESC LIMIT 1",
        (video["id"],),
    )
    if unfinished and not reprocess:
        scan_id = store.start_scan(video["id"], snapshot, signature, resume=True)
    elif not reprocess and store.rows(
        "SELECT id FROM selected_video_scans WHERE video_id=?", (video["id"],)
    ):
        return False
    else:
        scan_id = store.start_scan(video["id"], snapshot, signature)
    local = bool(video["local_path"] and Path(video["local_path"]).is_file())
    headers = None
    try:
        if frame_source is not None:
            duration = video["duration_s"] or settings["sampling"]["chunk_seconds"]
            path, dimensions = None, None
        elif local:
            path = video["local_path"]
            if video["duration_s"]:
                dimensions, duration = None, video["duration_s"]
            else:
                dimensions, duration = probe(path)
        else:
            if video["source_url"].startswith("file:"):
                raise FileNotFoundError(video["local_path"])
            for retry in range(settings["acquisition"]["attempts"]):
                try:
                    path, duration, dimensions, _, headers = resolve_stream(video)
                    break
                except Exception:
                    if retry + 1 == settings["acquisition"]["attempts"]:
                        raise
                    time.sleep(
                        min(
                            settings["acquisition"]["max_backoff_s"],
                            settings["acquisition"]["backoff_s"] * 2**retry,
                        )
                    )
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("A finite VOD duration is required for bounded scanning")
    except BaseException as exc:
        store.finish_scan(scan_id, str(exc))
        raise
    folder = Path("report/assets") / (
        hashlib.sha256(video["id"].encode()).hexdigest()[:16] + "-" + uuid.uuid4().hex
    )
    if profile:
        with profile.measure("startup_cleanup"):
            store.recover_orphan_evidence()
    else:
        store.recover_orphan_evidence()
    tracker = TextTracker.restore(settings["clustering"], store, scan_id)
    last_progress = time.monotonic()

    def report_progress(timestamp, *, force=False):
        nonlocal last_progress
        now = time.monotonic()
        if not force and now - last_progress < 5:
            return
        last_progress = now
        timestamp = min(duration, max(0, timestamp))
        elapsed_minutes, elapsed_seconds = divmod(int(timestamp), 60)
        total_minutes, total_seconds = divmod(int(duration), 60)
        # Fixed wall-time estimate based on video time still to scan.
        remaining_minutes, remaining_seconds = divmod(math.ceil((duration - timestamp) * 0.65), 60)
        estimated_minutes, estimated_seconds = divmod(math.ceil(duration * 0.65), 60)
        log.info(
            "%s: %02d:%02d / %02d:%02d (%.1f%%); "
            "estimated remaining %02d:%02d (full video %02d:%02d at 0.65x)",
            video["id"],
            elapsed_minutes,
            elapsed_seconds,
            total_minutes,
            total_seconds,
            100 * timestamp / duration,
            remaining_minutes,
            remaining_seconds,
            estimated_minutes,
            estimated_seconds,
        )

    report_progress(tracker.last_time, force=True)
    source_used = False
    try:
        for sequence, start, end in _chunks(duration, settings["sampling"]["chunk_seconds"]):
            with store.transaction() as db:
                db.execute(
                    """INSERT INTO scan_chunks(video_scan_id,sequence_no,chunk_start_s,chunk_end_s,
                    download_mode) VALUES (?,?,?,?,?) ON CONFLICT(video_scan_id,sequence_no) DO NOTHING""",
                    (
                        scan_id,
                        sequence,
                        start,
                        end,
                        "fixture"
                        if frame_source is not None
                        else "local-section"
                        if local
                        else "remote-section",
                    ),
                )
            chunk = store.rows(
                "SELECT * FROM scan_chunks WHERE video_scan_id=? AND sequence_no=?",
                (scan_id, sequence),
            )[0]
            if chunk["chunk_start_s"] != start or chunk["chunk_end_s"] != end:
                raise ValueError("Source duration changed during resume; start a new scan")
            if chunk["status"] == "completed":
                continue
            attempts = settings["acquisition"]["attempts"]
            for retry in range(attempts):
                overlap = settings["acquisition"]["overlap_s"]
                source_start = max(
                    0,
                    math.floor((start - overlap) * settings["sampling"]["fps"])
                    / settings["sampling"]["fps"],
                )
                attempt = store.record_attempt(
                    chunk["id"], requested_start_s=source_start, requested_end_s=end
                )
                attempt_started = time.monotonic()
                with store.transaction() as db:
                    db.execute(
                        "UPDATE scan_chunks SET status='running',error=NULL WHERE id=?",
                        (chunk["id"],),
                    )
                try:
                    if frame_source is not None:
                        if source_used:
                            raise ValueError("A fixture source cannot be replayed after failure")
                        source = frame_source
                        source_used = True
                    else:
                        source = sample_frames(
                            path,
                            settings["sampling"]["fps"],
                            source_start,
                            max_height=settings["sampling"]["max_height"],
                            chunk_id=chunk["id"],
                            end=end,
                            dimensions=dimensions,
                            http_headers=headers,
                        )
                    pending, pending_bytes, first_time, seen = [], 0, None, 0
                    with frame_iterator(source) as frames:
                        while True:
                            if profile:
                                with profile.measure("decoding"):
                                    batch = list(
                                        _take(
                                            frames,
                                            max(
                                                settings["detector"]["batch_size"],
                                                settings["recognizer"]["pool_frames"],
                                            ),
                                        )
                                    )
                            else:
                                batch = list(
                                    _take(
                                        frames,
                                        max(
                                            settings["detector"]["batch_size"],
                                            settings["recognizer"]["pool_frames"],
                                        ),
                                    )
                                )
                            if not batch:
                                break
                            if any(not isinstance(frame, Frame) for frame in batch):
                                raise TypeError(
                                    "Detection ingestion requires source-aware Frame records"
                                )
                            if profile:
                                with profile.measure("detection"):
                                    detector_batch = [
                                        detector_frame(frame, settings["sampling"]["region"])
                                        for frame in batch
                                    ]
                                    if profile.detector_resolution is None:
                                        profile.detector_resolution = list(
                                            detector_batch[0].image.size
                                        )
                                    proposals = detect_frames(detector, detector_batch)
                            else:
                                detector_batch = [
                                    detector_frame(frame, settings["sampling"]["region"])
                                    for frame in batch
                                ]
                                proposals = detect_frames(detector, detector_batch)
                            eligible, eligible_proposals = [], []
                            for frame, detections in zip(batch, proposals):
                                if (
                                    frame.timestamp_s < start - 1e-8
                                    or frame.timestamp_s >= end - 1e-8
                                ):
                                    continue
                                seen += 1
                                if frame.timestamp_s <= tracker.last_time:
                                    continue
                                if profile:
                                    profile.frames += 1
                                    if profile.source_resolution is None:
                                        profile.source_resolution = [
                                            frame.source_width,
                                            frame.source_height,
                                        ]
                                        profile.working_resolution = [
                                            frame.image.width,
                                            frame.image.height,
                                        ]
                                eligible.append(frame)
                                eligible_proposals.append(detections)
                            for entry in _prepare_frames(
                                eligible, eligible_proposals, recognizer, settings, profile
                            ):
                                frame = entry[0]
                                pending.append(entry)
                                pending_bytes += frame.image.width * frame.image.height * 3
                                first_time = frame.timestamp_s if first_time is None else first_time
                                limits = settings["persistence"]
                                if (
                                    frame.timestamp_s - first_time >= limits["commit_interval_s"]
                                    or len(pending) >= limits["max_rows"]
                                    or pending_bytes >= limits["max_bytes"]
                                ):
                                    if profile:
                                        with profile.measure("persistence_evidence"):
                                            _commit_batch(
                                                store,
                                                scan_id,
                                                chunk["id"],
                                                video["id"],
                                                folder,
                                                tracker,
                                                pending,
                                                recognizer,
                                                settings,
                                                profile,
                                            )
                                    else:
                                        _commit_batch(
                                            store,
                                            scan_id,
                                            chunk["id"],
                                            video["id"],
                                            folder,
                                            tracker,
                                            pending,
                                            recognizer,
                                            settings,
                                        )
                                    pending, pending_bytes, first_time = [], 0, None
                                    report_progress(tracker.last_time)
                    if pending:
                        if profile:
                            with profile.measure("persistence_evidence"):
                                _commit_batch(
                                    store,
                                    scan_id,
                                    chunk["id"],
                                    video["id"],
                                    folder,
                                    tracker,
                                    pending,
                                    recognizer,
                                    settings,
                                    profile,
                                )
                        else:
                            _commit_batch(
                                store,
                                scan_id,
                                chunk["id"],
                                video["id"],
                                folder,
                                tracker,
                                pending,
                                recognizer,
                                settings,
                            )
                    if not seen and not store.rows(
                        "SELECT id FROM sampled_frames WHERE scan_chunk_id=? LIMIT 1",
                        (chunk["id"],),
                    ):
                        raise RuntimeError("No video frames decoded")
                    with store.transaction() as db:
                        db.execute(
                            "UPDATE scan_chunks SET status='completed',error=NULL,"
                            "completed_at=CURRENT_TIMESTAMP WHERE id=?",
                            (chunk["id"],),
                        )
                        db.execute(
                            "UPDATE chunk_attempts SET completed_at=CURRENT_TIMESTAMP,"
                            "elapsed_s=? WHERE id=?",
                            (time.monotonic() - attempt_started, attempt),
                        )
                    report_progress(end)
                    break
                except Exception as exc:
                    with store.transaction() as db:
                        db.execute(
                            "UPDATE scan_chunks SET status='failed',error=? WHERE id=?",
                            (str(exc), chunk["id"]),
                        )
                        db.execute(
                            "UPDATE chunk_attempts SET error=?,completed_at=CURRENT_TIMESTAMP,"
                            "elapsed_s=? WHERE id=?",
                            (str(exc), time.monotonic() - attempt_started, attempt),
                        )
                    tracker = TextTracker.restore(settings["clustering"], store, scan_id)
                    if retry + 1 == attempts or frame_source is not None:
                        raise
                    time.sleep(
                        min(
                            settings["acquisition"]["max_backoff_s"],
                            settings["acquisition"]["backoff_s"] * 2**retry,
                        )
                    )
        chunks = store.rows("SELECT status FROM scan_chunks WHERE video_scan_id=?", (scan_id,))
        if len(chunks) != math.ceil(duration / settings["sampling"]["chunk_seconds"]) or any(
            row["status"] != "completed" for row in chunks
        ):
            raise ValueError("Source duration changed during resume; chunks are incomplete")
        with store.transaction() as db:
            for identity in tracker.active:
                db.execute(
                    "UPDATE text_clusters SET close_reason='end_of_scan' WHERE id=?", (identity,)
                )
            db.execute(
                "UPDATE videos SET status='scanned',error=NULL,scanned_at=CURRENT_TIMESTAMP "
                "WHERE id=?",
                (video["id"],),
            )
        store.finish_scan(scan_id)
        report_progress(duration, force=True)
        # Every committed batch has already compacted its changed clusters.
        # A crash between file save and commit is recovered at the next startup.
    except BaseException as exc:
        store.finish_scan(scan_id, str(exc))
        raise
    return True


def _take(iterator, count):
    from itertools import islice

    return islice(iterator, count)
