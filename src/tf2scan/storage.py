"""Versioned SQLite corpus with durable scan batches."""

import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .geometry import decode_polygon
from .lineage import LineageStore, migrate_legacy
from .lineage_schema import SCHEMA

MIGRATIONS = [
    """
CREATE TABLE videos (
 id TEXT PRIMARY KEY, source_url TEXT NOT NULL, channel_id TEXT, title TEXT NOT NULL,
 upload_date TEXT, duration_s REAL, resolution INTEGER, local_path TEXT,
 managed_download INTEGER NOT NULL DEFAULT 0, hud_profile TEXT,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN
 ('pending','downloaded','scanned','failed','skipped')),
 error TEXT, scanned_at TEXT, scan_signature TEXT, scan_seconds REAL, download_bytes INTEGER
);
CREATE TABLE row_clusters (
 id INTEGER PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
 start_s REAL NOT NULL, end_s REAL NOT NULL, row_index INTEGER NOT NULL,
 canonical_text TEXT NOT NULL, normalized_text TEXT NOT NULL, compact_text TEXT NOT NULL,
 best_confidence REAL, support_count INTEGER NOT NULL, hud_profile TEXT NOT NULL,
 hud_config_json TEXT NOT NULL, model_version TEXT NOT NULL,
 normalization_version TEXT NOT NULL, evidence_path TEXT, frame_path TEXT
);
CREATE TABLE observations (
 id INTEGER PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
 row_cluster_id INTEGER NOT NULL REFERENCES row_clusters(id) ON DELETE CASCADE,
 timestamp_s REAL NOT NULL, row_index INTEGER NOT NULL, raw_text TEXT NOT NULL,
 normalized_text TEXT NOT NULL, compact_text TEXT NOT NULL, ocr_confidence REAL,
 model_version TEXT NOT NULL, normalization_version TEXT NOT NULL,
 hud_profile TEXT NOT NULL, crop_path TEXT
);
CREATE INDEX observations_cluster ON observations(row_cluster_id, timestamp_s);
CREATE INDEX clusters_video ON row_clusters(video_id, start_s);
CREATE TABLE queries (
 id INTEGER PRIMARY KEY, target_name TEXT NOT NULL, aliases_json TEXT NOT NULL,
 matcher_version TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(target_name, aliases_json, matcher_version)
);
CREATE TABLE hits (
 id INTEGER PRIMARY KEY, row_cluster_id INTEGER NOT NULL REFERENCES row_clusters(id) ON DELETE CASCADE,
 query_id INTEGER NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
 matched_alias TEXT NOT NULL, best_text TEXT NOT NULL, best_score REAL NOT NULL,
 support_count INTEGER NOT NULL, evidence_path TEXT,
 review_status TEXT NOT NULL DEFAULT 'unreviewed'
 CHECK(review_status IN ('unreviewed','confirmed','rejected')), reviewer_note TEXT NOT NULL DEFAULT '',
 UNIQUE(row_cluster_id, query_id)
);
CREATE TABLE query_matches (
 query_id INTEGER NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
 observation_id INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
 matched_alias TEXT NOT NULL, match_score REAL NOT NULL,
 PRIMARY KEY(query_id, observation_id, matched_alias)
);
""",
    """
ALTER TABLE row_clusters ADD COLUMN evidence_timestamp_s REAL;
ALTER TABLE row_clusters ADD COLUMN evidence_row_index INTEGER;
ALTER TABLE videos ADD COLUMN scan_config_json TEXT;
""",
]


MIGRATIONS.append(SCHEMA)
MIGRATIONS.append("""
ALTER TABLE observations ADD COLUMN detection_identity TEXT;
ALTER TABLE observations ADD COLUMN crop_transform_json TEXT;
""")
MIGRATIONS.append("""
ALTER TABLE chunk_attempts ADD COLUMN requested_start_s REAL;
ALTER TABLE chunk_attempts ADD COLUMN requested_end_s REAL;
ALTER TABLE chunk_attempts ADD COLUMN elapsed_s REAL;
ALTER TABLE chunk_attempts ADD COLUMN media_bytes INTEGER;
""")


class Store(LineageStore):
    def __init__(self, root):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "results.sqlite3")
        self.db.row_factory = sqlite3.Row

        def valid_polygon(blob):
            try:
                decode_polygon(blob)
                return 1
            except (ValueError, TypeError):
                return 0

        self.db.create_function("valid_polygon", 1, valid_polygon, deterministic=True)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > len(MIGRATIONS):
            raise ValueError("Database was created by a newer tf2scan version")
        self.db.execute("PRAGMA synchronous=NORMAL")
        if 0 < version < len(MIGRATIONS):
            backup = root / f"results.pre-v{version}-migration.sqlite3"
            if backup.exists():
                raise FileExistsError(f"Preserve or rename existing migration backup: {backup}")
            with sqlite3.connect(backup) as destination:
                self.db.backup(destination)
        for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                statement = ""
                for line in sql.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        self.db.execute(statement)
                        statement = ""
                if i == 3:
                    migrate_legacy(self.db)
                self.db.execute(f"PRAGMA user_version={i}")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    @contextmanager
    def transaction(self):
        with self.db:
            yield self.db

    def rows(self, sql, params=()):
        return [dict(row) for row in self.db.execute(sql, params)]

    def video(self, video_id):
        rows = self.rows("SELECT * FROM videos WHERE id=?", (video_id,))
        if not rows:
            raise ValueError(f"Unknown video: {video_id}")
        return rows[0]

    def upsert_video(self, values):
        columns = list(values)
        updates = [
            f"{key}=excluded.{key}"
            for key in columns
            if key not in ("id", "status", "error", "local_path", "managed_download")
        ]
        with self.db:
            self.db.execute(
                f"INSERT INTO videos ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
                f"ON CONFLICT(id) DO UPDATE SET {','.join(updates)}",
                list(values.values()),
            )
            if "status" in values:
                self.db.execute(
                    "UPDATE videos SET status=?,error=? WHERE id=? "
                    "AND status IN ('pending','skipped')",
                    (values["status"], values.get("error"), values["id"]),
                )

    def update_video(self, video_id, **values):
        with self.db:
            self.db.execute(
                f"UPDATE videos SET {','.join(f'{k}=?' for k in values)} WHERE id=?",
                [*values.values(), video_id],
            )

    def delete_video(self, video_id):
        """Remove only this source's owned evidence and managed download."""
        video = self.video(video_id)
        paths = self.rows(
            "SELECT evidence_path AS path FROM text_clusters WHERE video_id=? "
            "UNION SELECT frame_path FROM text_clusters WHERE video_id=? "
            "UNION SELECT crop_path FROM observations WHERE video_scan_id IN "
            "(SELECT id FROM video_scans WHERE video_id=?) "
            "UNION SELECT legacy_evidence_path FROM observations WHERE video_scan_id IN "
            "(SELECT id FROM video_scans WHERE video_id=?) "
            "UNION SELECT full_frame_path FROM sampled_frames WHERE video_scan_id IN "
            "(SELECT id FROM video_scans WHERE video_id=?)",
            (video_id,) * 5,
        )
        # Commit the cascade first. A failed delete must never remove live evidence.
        with self.db:
            self.db.execute("DELETE FROM videos WHERE id=?", (video_id,))
            self.db.execute("DELETE FROM scan_runs WHERE id NOT IN "
                            "(SELECT scan_run_id FROM video_scans)")
        if video["managed_download"] and video["local_path"]:
            path = Path(video["local_path"]).resolve()
            if path.is_relative_to((self.root / "downloads").resolve()):
                path.unlink(missing_ok=True)
        referenced = {
            (self.root / row[0]).resolve()
            for row in self.db.execute(
                "SELECT evidence_path FROM text_clusters UNION SELECT frame_path FROM text_clusters "
                "UNION SELECT crop_path FROM observations UNION SELECT legacy_evidence_path FROM observations "
                "UNION SELECT full_frame_path FROM sampled_frames")
            if row[0]
        }
        assets = (self.root / "report/assets").resolve()
        for row in paths:
            for value in row.values():
                if value:
                    path = (self.root / value).resolve()
                    if (assets.is_relative_to(self.root.resolve())
                            and path.is_relative_to(assets)
                            and path not in referenced):
                        path.unlink(missing_ok=True)
        self.prune_orphan_evidence()

    def compact_evidence(self, scan_id, max_candidates=4, aliases=(), profile=None):
        """Keep actual representative and bounded query-relevant observation crops."""
        from .matching import alias_score

        clusters = self.rows("SELECT id,representative_observation_id FROM text_clusters "
                             "WHERE video_scan_id=?", (scan_id,))
        if profile:
            profile.counts["compaction_clusters_examined"] += len(clusters)
        keep = set()
        for cluster in clusters:
            keep.add(cluster["representative_observation_id"])
            if aliases:
                observations = self.rows(
                    "SELECT o.id,o.raw_text,o.crop_path FROM observations o "
                    "JOIN cluster_observations co ON co.observation_id=o.id "
                    "WHERE co.cluster_id=?", (cluster["id"],))
                ranked = sorted(
                    ((max(alias_score(o["raw_text"], a) for a in aliases), o["id"])
                     for o in observations if o["crop_path"]
                     and o["id"] != cluster["representative_observation_id"]),
                    key=lambda item: (-item[0], item[1]))
                keep.update(identity for score, identity in ranked[:max_candidates] if score >= 0.65)
        discard = self.rows(
            "SELECT id,crop_path FROM observations WHERE video_scan_id=? AND crop_path IS NOT NULL",
            (scan_id,))
        if profile:
            profile.counts["compaction_crops_examined"] += len(discard)
        with self.transaction() as db:
            for row in discard:
                if row["id"] not in keep:
                    db.execute("UPDATE observations SET crop_path=NULL WHERE id=?", (row["id"],))
            db.execute(
                """UPDATE sampled_frames SET full_frame_path=NULL WHERE video_scan_id=?
                AND id NOT IN (SELECT o.sampled_frame_id FROM observations o
                JOIN text_clusters c ON c.representative_observation_id=o.id
                WHERE c.video_scan_id=?)""", (scan_id, scan_id))
        self.prune_orphan_evidence(profile=profile)

    def prune_orphan_evidence(self, profile=None):
        """Recover files left by a killed process; only touch scanner-owned names.

        Call after a committed batch or report, never while a batch is writing files.
        The output directory supports a single writer, as documented.
        """
        assets = (self.root / "report/assets").resolve()
        if not assets.is_dir() or not assets.is_relative_to(self.root.resolve()):
            return
        reference_started = time.perf_counter() if profile else None
        if profile:
            profile.counts["orphan_sweep_calls"] += 1
        referenced = set()
        for row in self.db.execute(
            "SELECT evidence_path FROM text_clusters UNION SELECT frame_path FROM text_clusters "
            "UNION SELECT crop_path FROM observations UNION SELECT legacy_evidence_path FROM observations "
            "UNION SELECT full_frame_path FROM sampled_frames"
        ):
            referenced.update((self.root / value).resolve() for value in row if value)
        if profile:
            profile.seconds["evidence_reference_lookup"] += time.perf_counter() - reference_started
            profile.counts["referenced_paths_examined"] += len(referenced)
        walk_started = time.perf_counter() if profile else None
        for folder in assets.iterdir():
            if not re.fullmatch(r"[0-9a-f]{16}-[0-9a-f]{32}", folder.name):
                continue
            if folder.is_symlink() or not folder.is_dir():
                continue
            for path in folder.iterdir():
                if profile:
                    profile.counts["asset_files_checked"] += 1
                if (
                    re.fullmatch(r"\d+-(row\.png|frame\.jpg)(\.[0-9a-f]{32}\.tmp)?",
                                 path.name)
                    and path.is_file()
                    and not path.is_symlink()
                    and path.resolve().is_relative_to(assets)
                    and path.resolve() not in referenced
                ):
                    path.unlink()
                    if profile:
                        profile.counts["orphan_files_deleted"] += 1
            if not any(folder.iterdir()):
                folder.rmdir()
        if profile:
            profile.seconds["evidence_file_walk"] += time.perf_counter() - walk_started
