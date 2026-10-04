"""Ticket 2: compare disposable snapshots; never open the source with Store.

python eval/benchmark_evidence_indexes.py --corpus output --output output/ticket2
Add --clip output/gameplay-smoke.mp4 --config config.yaml for warm Paddle scans.
"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import statistics
import time
from dataclasses import asdict
from pathlib import Path

from tf2scan.config import Config, load_config
from tf2scan.frames import probe
from tf2scan.ingestion import ingest
from tf2scan.performance import ScanProfile
from tf2scan.storage import EVIDENCE_REFERENCE_QUERY, MIGRATIONS, Store

REFERENCES = (
    ("text_clusters", "evidence_path"),
    ("text_clusters", "frame_path"),
    ("observations", "crop_path"),
    ("observations", "legacy_evidence_path"),
    ("sampled_frames", "full_frame_path"),
)


def backup(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(destination) as db:
        source.backup(db)


def indexes(db, enabled):
    if enabled:
        db.executescript(MIGRATIONS[5])
    else:
        for table, column in REFERENCES:
            db.execute(f"DROP INDEX IF EXISTS {table}_{column}")
        db.commit()


def grow(db, multiplier):
    """Add unrelated reference rows without changing the timed scan's ownership.

    Clones keep real path lengths/null density. Existing lineage owners stay valid;
    these are synthetic scaling corpora, not new video generations.
    """
    for table in ("sampled_frames", "observations", "text_clusters"):
        columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})") if row[1] != "id"]
        original_max = db.execute(f"SELECT max(id) FROM {table}").fetchone()[0] or 0
        max_time = db.execute("SELECT max(timestamp_s) FROM sampled_frames").fetchone()[0] or 0
        for copy in range(1, multiplier):
            expressions = []
            for column in columns:
                if column in ("sample_key", "geometry_key"):
                    expressions.append(f"{column} || ':ticket2-{copy}'")
                elif table == "sampled_frames" and column == "timestamp_s":
                    expressions.append(f"timestamp_s + {(max_time + 1) * copy}")
                else:
                    expressions.append(column)
            db.execute(
                f"INSERT INTO {table} ({','.join(columns)}) SELECT {','.join(expressions)} "
                f"FROM {table} WHERE id<=?",
                (original_max,),
            )
    db.commit()
    assert not db.execute("PRAGMA foreign_key_check").fetchall()


def timed(call):
    start = time.perf_counter()
    call()
    return time.perf_counter() - start


def occupied_bytes(db):
    pages = db.execute("PRAGMA page_count").fetchone()[0]
    free = db.execute("PRAGMA freelist_count").fetchone()[0]
    return (pages - free) * db.execute("PRAGMA page_size").fetchone()[0]


def lookup_trial(store, paths):
    for path in paths:  # Warm SQLite's page cache before measuring.
        store.db.execute(EVIDENCE_REFERENCE_QUERY, (path,) * 5).fetchone()
    samples = []
    for _ in range(3):
        samples.append(
            timed(
                lambda: [
                    store.db.execute(EVIDENCE_REFERENCE_QUERY, (path,) * 5).fetchone()
                    for path in paths
                ]
            )
            / len(paths)
        )
    return samples


def write_trial(store):
    """1000 crop path changes + commit, alternating restoration, with fixed IDs."""
    rows = store.db.execute(
        "SELECT id,crop_path FROM observations WHERE crop_path IS NOT NULL ORDER BY id LIMIT 1000"
    ).fetchall()
    original = [(row[1], row[0]) for row in rows]
    changed = [(row[1] + ".ticket2", row[0]) for row in rows]
    samples = []
    for _ in range(3):
        for values in (changed, original):

            def update(values=values):
                with store.transaction() as db:
                    db.executemany("UPDATE observations SET crop_path=? WHERE id=?", values)

            samples.append(timed(update))
    return {"rows_per_commit": len(rows), "seconds": samples}


def link_or_copy(source, target):
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def scan_trial(snapshot, assets, root, enabled, clip, config, models):
    with sqlite3.connect(snapshot) as source:
        backup(source, root / "results.sqlite3")
    if assets.is_dir():
        # Scanner image saves use atomic replacement; cleanup unlinks only these links.
        shutil.copytree(assets, root / "report/assets", copy_function=link_or_copy)
    data = dict(config.data, output_dir=str(root.resolve()))
    trial_config = Config(config.path, data)
    detector, recognizer = models
    with Store(root) as store:
        indexes(store.db, enabled)
        video = {
            "id": "ticket2-fixed-clip",
            "title": clip.name,
            "source_url": clip.as_uri(),
            "duration_s": probe(clip)[1],
            "local_path": str(clip),
        }
        store.upsert_video(video)
        profile = ScanProfile()
        assert ingest(
            store,
            trial_config,
            store.video(video["id"]),
            recognizer,
            detector=detector,
            profile=profile,
            reprocess=True,
        )
        result = profile.result(video, json.loads(trial_config.effective_scan()[0]))
        scan = store.rows("SELECT id FROM selected_video_scans WHERE video_id=?", (video["id"],))[
            0
        ]["id"]
        rows = store.rows(
            "SELECT f.timestamp_s,o.geometry_key,o.raw_text,o.ocr_confidence FROM observations o "
            "JOIN sampled_frames f ON f.id=o.sampled_frame_id WHERE o.video_scan_id=? "
            "ORDER BY f.timestamp_s,o.geometry_key",
            (scan,),
        )
        result["observation_hash"] = hashlib.sha256(json.dumps(rows).encode()).hexdigest()
        result["timestamps"] = [
            row[0]
            for row in store.db.execute(
                "SELECT timestamp_s FROM sampled_frames WHERE video_scan_id=? ORDER BY timestamp_s",
                (scan,),
            )
        ]
        result["indexed"] = enabled
        result["missing_representatives"] = sum(
            not (root / row[0]).is_file()
            for row in store.db.execute(
                "SELECT evidence_path FROM text_clusters WHERE video_scan_id=? "
                "AND evidence_path IS NOT NULL",
                (scan,),
            )
        )
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--clip", type=Path)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--scan-multipliers", type=int, nargs="+", choices=(1, 2, 4), default=[1])
    parser.add_argument("--scan-repeats", type=int, default=3)
    args = parser.parse_args()
    if args.scan_repeats < 1:
        parser.error("--scan-repeats must be positive")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)  # Preserve earlier experiments.
    snapshot = output / "snapshot/results.sqlite3"
    uri = (args.corpus.resolve() / "results.sqlite3").as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as source:
        backup(source, snapshot)
    result = {
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "cpu": platform.processor(),
        "source": str(args.corpus.resolve()),
        "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        "lookup": [],
        "scans": [],
    }
    with Store(snapshot.parent) as store:
        paths = []
        for table, column in REFERENCES:
            paths.extend(
                row[0]
                for row in store.db.execute(
                    f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL LIMIT 10"
                )
            )
        paths.extend(f"report/assets/missing/{i}-row.png" for i in range(50))
    for multiplier in (1, 2, 4):
        root = output / f"lookup-{multiplier}"
        with sqlite3.connect(snapshot) as source:
            backup(source, root / "results.sqlite3")
        with Store(root) as store:
            grow(store.db, multiplier)
            counts = {
                table: store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("text_clusters", "observations", "sampled_frames")
            }
            for enabled in (False, True):
                storage_before = occupied_bytes(store.db)
                build_s = timed(lambda enabled=enabled: indexes(store.db, enabled))
                storage_delta = occupied_bytes(store.db) - storage_before
                plan = store.rows(
                    "EXPLAIN QUERY PLAN " + EVIDENCE_REFERENCE_QUERY, (paths[-1],) * 5
                )
                entry = {
                    "multiplier": multiplier,
                    "indexed": enabled,
                    "rows": counts,
                    "index_toggle_s": build_s,
                    "plan": plan,
                    "lookup_seconds": lookup_trial(store, paths),
                    "write": write_trial(store),
                }
                entry["index_occupied_bytes"] = storage_delta if enabled else -storage_delta
                result["lookup"].append(entry)
                print(
                    multiplier,
                    enabled,
                    "lookup ms",
                    statistics.median(entry["lookup_seconds"]) * 1000,
                    flush=True,
                )
    results_path = output / "results.json"
    results_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if args.clip:
        from tf2scan.paddle_backend import create_models

        config = load_config(args.config)
        assets = output / "snapshot/report/assets"
        if (args.corpus / "report/assets").is_dir():
            shutil.copytree(args.corpus / "report/assets", assets)
        setup = time.perf_counter()
        models = create_models(config)
        result["model_setup_s"] = time.perf_counter() - setup
        result["models"] = [asdict(model.metadata) for model in models]
        clip = args.clip.resolve()
        result["clip_sha256"] = hashlib.sha256(clip.read_bytes()).hexdigest()
        try:
            # Discard first inference, then alternate order to reduce warmup bias.
            result["warmup"] = scan_trial(
                snapshot, Path("__no_assets__"), output / "warmup", True, clip, config, models
            )
            for multiplier, repeat in (
                (multiplier, repeat)
                for multiplier in args.scan_multipliers
                for repeat in range(args.scan_repeats)
            ):
                for enabled in (False, True) if repeat % 2 == 0 else (True, False):
                    trial = scan_trial(
                        output / f"lookup-{multiplier}/results.sqlite3",
                        assets,
                        output / f"scan-{multiplier}-{repeat}-{enabled}",
                        enabled,
                        clip,
                        config,
                        models,
                    )
                    trial["corpus_multiplier"] = multiplier
                    result["scans"].append(trial)
                    results_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
                    print(
                        "scan",
                        repeat,
                        enabled,
                        "wall",
                        trial["wall_s"],
                        "compaction",
                        trial["persistence_detail_s"]["compaction_total"],
                        flush=True,
                    )
        finally:
            for model in models:
                model.close()
    results_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
