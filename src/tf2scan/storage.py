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
        self._orphan_recovery_done = False
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
            self.db.execute(
                "DELETE FROM scan_runs WHERE id NOT IN (SELECT scan_run_id FROM video_scans)"
            )
        if video["managed_download"] and video["local_path"]:
            path = Path(video["local_path"]).resolve()
            if path.is_relative_to((self.root / "downloads").resolve()):
                path.unlink(missing_ok=True)
        referenced = {
            (self.root / row[0]).resolve()
            for row in self.db.execute(
                "SELECT evidence_path FROM text_clusters UNION SELECT frame_path FROM text_clusters "
                "UNION SELECT crop_path FROM observations UNION SELECT legacy_evidence_path FROM observations "
                "UNION SELECT full_frame_path FROM sampled_frames"
            )
            if row[0]
        }
        assets = (self.root / "report/assets").resolve()
        for row in paths:
            for value in row.values():
                if value:
                    path = (self.root / value).resolve()
                    if (
                        assets.is_relative_to(self.root.resolve())
                        and path.is_relative_to(assets)
                        and path not in referenced
                    ):
                        path.unlink(missing_ok=True)
        self.prune_orphan_evidence()

    def compact_evidence(self, scan_id, profile=None, cluster_ids=None):
        """Keep cluster representative crops independently of search targets."""

        if cluster_ids is not None and not cluster_ids:
            return
        query = "SELECT id,representative_observation_id FROM text_clusters WHERE video_scan_id=?"
        parameters = [scan_id]
        if cluster_ids is not None:
            query += f" AND id IN ({','.join('?' for _ in cluster_ids)})"
            parameters.extend(cluster_ids)
        clusters = self.rows(query, parameters)
        if profile:
            profile.counts["compaction_clusters_examined"] += len(clusters)
        keep = set()
        for cluster in clusters:
            keep.add(cluster["representative_observation_id"])
        if cluster_ids is None:
            discard = self.rows(
                "SELECT id,crop_path FROM observations WHERE video_scan_id=? AND crop_path IS NOT NULL",
                (scan_id,),
            )
        else:
            discard = self.rows(
                "SELECT DISTINCT o.id,o.crop_path FROM observations o "
                "JOIN cluster_observations co ON co.observation_id=o.id "
                f"WHERE co.cluster_id IN ({','.join('?' for _ in cluster_ids)}) "
                "AND o.crop_path IS NOT NULL",
                tuple(cluster_ids),
            )
        if profile:
            profile.counts["compaction_crops_examined"] += len(discard)
        removed = [row["crop_path"] for row in discard if row["id"] not in keep]
        with self.transaction() as db:
            for row in discard:
                if row["id"] not in keep:
                    db.execute("UPDATE observations SET crop_path=NULL WHERE id=?", (row["id"],))
            if cluster_ids is None:
                frames = db.execute(
                    """SELECT full_frame_path FROM sampled_frames WHERE video_scan_id=?
                    AND full_frame_path IS NOT NULL AND id NOT IN
                    (SELECT o.sampled_frame_id FROM observations o JOIN text_clusters c
                    ON c.representative_observation_id=o.id WHERE c.video_scan_id=?)""",
                    (scan_id, scan_id),
                ).fetchall()
                removed.extend(row[0] for row in frames)
                db.execute(
                    """UPDATE sampled_frames SET full_frame_path=NULL WHERE video_scan_id=?
                    AND id NOT IN (SELECT o.sampled_frame_id FROM observations o
                    JOIN text_clusters c ON c.representative_observation_id=o.id
                    WHERE c.video_scan_id=?)""",
                    (scan_id, scan_id),
                )
            else:
                placeholders = ",".join("?" for _ in cluster_ids)
                frames = db.execute(
                    "SELECT DISTINCT f.full_frame_path FROM sampled_frames f "
                    "JOIN observations o ON o.sampled_frame_id=f.id "
                    "JOIN cluster_observations co ON co.observation_id=o.id "
                    f"WHERE co.cluster_id IN ({placeholders}) "
                    "AND f.full_frame_path IS NOT NULL AND f.id NOT IN "
                    "(SELECT o2.sampled_frame_id FROM observations o2 JOIN text_clusters c "
                    "ON c.representative_observation_id=o2.id)",
                    tuple(cluster_ids),
                ).fetchall()
                removed.extend(row[0] for row in frames)
                db.executemany(
                    "UPDATE sampled_frames SET full_frame_path=NULL WHERE full_frame_path=?",
                    [(row[0],) for row in frames],
                )
        if cluster_ids is None:
            self.prune_orphan_evidence(profile=profile)
        else:
            self.prune_evidence_paths(removed, profile=profile)

    def prune_evidence_paths(self, paths, profile=None):
        """Delete only committed, unreferenced scanner-owned evidence paths."""
        assets = (self.root / "report/assets").resolve()
        root = self.root.resolve()
        if not assets.is_relative_to(root):
            return
        for value in set(paths):
            if not value:
                continue
            relative = Path(value)
            if (
                relative.is_absolute()
                or len(relative.parts) != 4
                or relative.parts[:2] != ("report", "assets")
                or not re.fullmatch(r"[0-9a-f]{16}-[0-9a-f]{32}", relative.parts[2])
            ):
                continue
            if not re.fullmatch(r"\d+-(row\.png|frame\.jpg)", relative.parts[3]):
                continue
            candidate = self.root / relative
            if (
                candidate.is_symlink()
                or candidate.parent.is_symlink()
                or not candidate.resolve().is_relative_to(assets)
            ):
                continue
            if profile:
                profile.counts["asset_files_checked"] += 1
            referenced = self.db.execute(
                "SELECT 1 FROM text_clusters WHERE evidence_path=? OR frame_path=? "
                "UNION SELECT 1 FROM observations WHERE crop_path=? OR legacy_evidence_path=? "
                "UNION SELECT 1 FROM sampled_frames WHERE full_frame_path=? LIMIT 1",
                (value,) * 5,
            ).fetchone()
            if profile:
                profile.counts["referenced_paths_examined"] += 1
            if not referenced and candidate.is_file():
                candidate.unlink()
                if profile:
                    profile.counts["orphan_files_deleted"] += 1

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
                    re.fullmatch(r"\d+-(row\.png|frame\.jpg)(\.[0-9a-f]{32}\.tmp)?", path.name)
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

    def recover_orphan_evidence(self):
        """Run crash recovery once per open corpus, before its first scan write."""
        if not self._orphan_recovery_done:
            self.prune_orphan_evidence()
            self._orphan_recovery_done = True
