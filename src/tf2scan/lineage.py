"""Legacy import and shared lineage lifecycle/selection helpers."""

import json

from .settings import canonical_json


def insert(db, table, **values):
    return db.execute(
        f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
        tuple(values.values()),
    ).lastrowid


def migrate_legacy(db):
    for video in db.execute(
        "SELECT * FROM videos WHERE id IN (SELECT video_id FROM legacy_row_clusters) OR status='scanned'"
    ).fetchall():
        clusters = db.execute(
            "SELECT * FROM legacy_row_clusters WHERE video_id=?", (video["id"],)
        ).fetchall()
        first = dict(clusters[0]) if clusters else {}
        run = insert(
            db,
            "scan_runs",
            legacy=1,
            status="completed",
            started_at=None,
            config_hash=video["scan_signature"] or "legacy:unknown",
            config_json=video["scan_config_json"] or "{}",
            recognizer_name="legacy",
            recognizer_version=first.get("model_version"),
            normalization_version=first.get("normalization_version"),
            completed_at=video["scanned_at"],
        )
        scan = insert(
            db,
            "video_scans",
            video_id=video["id"],
            scan_run_id=run,
            status="completed",
            started_at=None,
            completed_at=video["scanned_at"],
        )
        chunk = insert(
            db,
            "scan_chunks",
            video_scan_id=scan,
            sequence_no=0,
            status="completed",
            started_at=None,
            chunk_start_s=0,
            download_mode="legacy-container",
        )
        frames = {}
        for old in db.execute(
            "SELECT * FROM legacy_observations WHERE video_id=? ORDER BY id", (video["id"],)
        ).fetchall():
            old = dict(old)
            timestamp = old["timestamp_s"]
            if timestamp not in frames:
                frames[timestamp] = insert(
                    db,
                    "sampled_frames",
                    video_scan_id=scan,
                    scan_chunk_id=chunk,
                    sample_key=f"legacy:{timestamp!r}",
                    timestamp_s=timestamp,
                )
            insert(
                db,
                "observations",
                id=old["id"],
                video_scan_id=scan,
                sampled_frame_id=frames[timestamp],
                geometry_key=f"legacy:{old['id']}",
                raw_text=old["raw_text"],
                normalized_text=old["normalized_text"],
                compact_text=old["compact_text"],
                ocr_confidence=old["ocr_confidence"],
                legacy_evidence_path=old["crop_path"],
                legacy_metadata_json=canonical_json(old),
            )
        for old in clusters:
            values = dict(old)
            cluster_id = values["id"]
            insert(db, "text_clusters", **values, video_scan_id=scan, close_reason="legacy-unknown")
            supports = db.execute(
                "SELECT * FROM legacy_observations WHERE row_cluster_id=?", (cluster_id,)
            ).fetchall()
            for obs in supports:
                insert(
                    db,
                    "cluster_observations",
                    cluster_id=cluster_id,
                    observation_id=obs["id"],
                    video_scan_id=scan,
                    support_score=1,
                )
            representatives = [
                o
                for o in supports
                if o["timestamp_s"] == values["evidence_timestamp_s"]
                and o["row_index"] == values["evidence_row_index"]
            ]
            if len(representatives) == 1:
                obs = representatives[0]
                db.execute(
                    "UPDATE observations SET crop_path=? WHERE id=?",
                    (values["evidence_path"], obs["id"]),
                )
                db.execute(
                    "UPDATE text_clusters SET representative_observation_id=? WHERE id=?",
                    (obs["id"], cluster_id),
                )
                db.execute(
                    "UPDATE sampled_frames SET full_frame_path=? WHERE id=?",
                    (values["frame_path"], frames[obs["timestamp_s"]]),
                )
    db.execute("""INSERT INTO hits(id,text_cluster_id,query_id,matched_alias,best_text,best_score,
               support_count,evidence_path,review_status,reviewer_note)
               SELECT id,row_cluster_id,query_id,matched_alias,best_text,best_score,
               support_count,evidence_path,review_status,reviewer_note FROM legacy_hits""")
    db.execute("INSERT INTO query_matches SELECT * FROM legacy_query_matches")
    for old, new in (
        ("legacy_row_clusters", "text_clusters"),
        ("legacy_observations", "observations"),
        ("legacy_hits", "hits"),
        ("legacy_query_matches", "query_matches"),
    ):
        if (
            db.execute(f"SELECT count(*) FROM {old}").fetchone()[0]
            != db.execute(f"SELECT count(*) FROM {new}").fetchone()[0]
        ):
            raise ValueError("Legacy migration count mismatch")
    if db.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("Legacy migration foreign key failure")


class LineageStore:
    def selected_scan_ids(self, *, scan_id=None, run_id=None):
        if scan_id is not None and run_id is not None:
            raise ValueError("Select either scan_id or run_id")
        if scan_id is not None:
            rows = self.rows("SELECT id FROM video_scans WHERE id=?", (scan_id,))
        elif run_id is not None:
            rows = self.rows("SELECT id FROM video_scans WHERE scan_run_id=?", (run_id,))
        else:
            rows = self.rows("SELECT id FROM selected_video_scans")
        return [row["id"] for row in rows]

    def selected_clusters(self, **selection):
        ids = self.selected_scan_ids(**selection)
        return self.rows(
            f"SELECT * FROM text_clusters WHERE video_scan_id IN ({','.join('?' for _ in ids)}) ORDER BY video_id,start_s",
            ids,
        )

    def start_scan(self, video_id, config_json, config_hash, *, resume=False, legacy=False):
        snapshot = json.loads(config_json)
        if resume:
            rows = self.rows(
                """SELECT s.id,r.config_json,r.config_hash,r.id AS run_id FROM video_scans s
                JOIN scan_runs r ON r.id=s.scan_run_id WHERE s.video_id=? AND s.status!='completed'
                ORDER BY s.id DESC LIMIT 1""",
                (video_id,),
            )
            if rows:
                row = rows[0]
                if row["config_hash"] != config_hash or row["config_json"] != config_json:
                    raise ValueError("Resume configuration/model mismatch; create a new run")
                with self.db:
                    self.db.execute(
                        "UPDATE video_scans SET status='running',error=NULL,completed_at=NULL WHERE id=?",
                        (row["id"],),
                    )
                    self.db.execute(
                        "UPDATE scan_runs SET status='running',completed_at=NULL WHERE id=?",
                        (row["run_id"],),
                    )
                return row["id"]
        settings = snapshot.get("settings", {})
        adapters = snapshot.get("adapters", {})
        versions = snapshot.get("versions", {})
        metadata = {}
        for kind in ("detector", "recognizer"):
            adapter = adapters.get(kind, settings.get(kind, {}))
            for field in ("name", "version", "weights_hash"):
                metadata[f"{kind}_{field}"] = adapter.get(field)
        with self.db:
            run = insert(
                self.db,
                "scan_runs",
                config_json=config_json,
                config_hash=config_hash,
                legacy=int(legacy),
                **metadata,
                preprocessing_version=versions.get("preprocessing"),
                normalization_version=versions.get("normalization"),
                geometry_encoding_version=versions.get("geometry"),
                software_versions_json=canonical_json(
                    {k: v.get("dependencies", {}) for k, v in adapters.items()}
                ),
            )
            return insert(
                self.db,
                "video_scans",
                video_id=video_id,
                scan_run_id=run,
                sample_fps=settings.get("sampling", {}).get("fps"),
            )

    def finish_scan(self, scan_id, error=None):
        status = "failed" if error is not None else "completed"
        with self.db:
            self.db.execute(
                "UPDATE video_scans SET status=?,error=?,completed_at=CURRENT_TIMESTAMP WHERE id=?",
                (status, error, scan_id),
            )
            self.db.execute(
                "UPDATE scan_runs SET status=?,completed_at=CURRENT_TIMESTAMP WHERE id=(SELECT scan_run_id FROM video_scans WHERE id=?)",
                (status, scan_id),
            )

    def record_attempt(self, chunk_id, error=None, *, requested_start_s=None, requested_end_s=None):
        with self.db:
            number = self.db.execute(
                "SELECT coalesce(max(attempt_no),0)+1 FROM chunk_attempts WHERE scan_chunk_id=?",
                (chunk_id,),
            ).fetchone()[0]
            return insert(
                self.db,
                "chunk_attempts",
                scan_chunk_id=chunk_id,
                attempt_no=number,
                error=error,
                requested_start_s=requested_start_s,
                requested_end_s=requested_end_s,
            )
