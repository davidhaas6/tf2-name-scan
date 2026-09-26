"""Versioned SQLite corpus. Ingestion commits once per complete video."""

import re
import sqlite3
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
        for row in paths:
            for value in row.values():
                if value:
                    path = (self.root / value).resolve()
                    if path.is_relative_to(self.root.resolve()):
                        path.unlink(missing_ok=True)
        if video["managed_download"] and video["local_path"]:
            path = Path(video["local_path"]).resolve()
            if path.is_relative_to((self.root / "downloads").resolve()):
                path.unlink(missing_ok=True)
        with self.db:
            self.db.execute("DELETE FROM videos WHERE id=?", (video_id,))

    def prune_orphan_evidence(self):
        """Recover files left by a killed process; only touch scanner-owned names.

        Call after committed ingestion/reporting, never during an active scan.
        The output directory supports a single writer, as documented.
        """
        assets = (self.root / "report/assets").resolve()
        if not assets.is_dir() or not assets.is_relative_to(self.root.resolve()):
            return
        referenced = set()
        for row in self.db.execute(
            "SELECT evidence_path FROM text_clusters UNION SELECT frame_path FROM text_clusters "
            "UNION SELECT crop_path FROM observations UNION SELECT legacy_evidence_path FROM observations "
            "UNION SELECT full_frame_path FROM sampled_frames"
        ):
            referenced.update((self.root / value).resolve() for value in row if value)
        for folder in assets.iterdir():
            if not re.fullmatch(r"[0-9a-f]{16}-[0-9a-f]{32}", folder.name):
                continue
            if folder.is_symlink() or not folder.is_dir():
                continue
            for path in folder.iterdir():
                if (
                    re.fullmatch(r"\d+-(row\.png|frame\.jpg)", path.name)
                    and path.is_file()
                    and not path.is_symlink()
                    and path.resolve().is_relative_to(assets)
                    and path.resolve() not in referenced
                ):
                    path.unlink()
            if not any(folder.iterdir()):
                folder.rmdir()
