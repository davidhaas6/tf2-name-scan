"""Indexed reference checks must preserve every evidence owner and path boundary."""

import sqlite3

import pytest

from tf2scan.lineage import insert
from tf2scan.performance import ScanProfile
from tf2scan.storage import EVIDENCE_REFERENCE_QUERY, MIGRATIONS, Store

REFERENCES = (
    ("text_clusters", "evidence_path"),
    ("text_clusters", "frame_path"),
    ("observations", "crop_path"),
    ("observations", "legacy_evidence_path"),
    ("sampled_frames", "full_frame_path"),
)
FOLDER = "report/assets/" + "a" * 16 + "-" + "b" * 32


def add_source(store, video):
    store.upsert_video({"id": video, "title": video, "source_url": "fixture"})
    scan = store.start_scan(video, "{}", video, legacy=True)
    with store.transaction() as db:
        chunk = insert(
            db,
            "scan_chunks",
            video_scan_id=scan,
            sequence_no=0,
            chunk_start_s=0,
            chunk_end_s=1,
            download_mode="fixture",
        )
        frame = insert(
            db,
            "sampled_frames",
            scan_chunk_id=chunk,
            video_scan_id=scan,
            sample_key="0",
            timestamp_s=0,
        )
        observation = insert(
            db,
            "observations",
            sampled_frame_id=frame,
            video_scan_id=scan,
            geometry_key="legacy",
            raw_text="Player",
            normalized_text="player",
            compact_text="player",
        )
        cluster = insert(
            db,
            "text_clusters",
            video_scan_id=scan,
            video_id=video,
            start_s=0,
            end_s=0,
            canonical_text="Player",
            normalized_text="player",
            compact_text="player",
            support_count=1,
            representative_observation_id=observation,
        )
    return {"text_clusters": cluster, "observations": observation, "sampled_frames": frame}


def test_new_and_existing_corpora_use_all_five_partial_indexes(tmp_path):
    with Store(tmp_path) as store:
        owners = add_source(store, "video")
        store.db.execute("UPDATE observations SET legacy_evidence_path='old.png'")
        store.db.commit()
        # Reconstruct the previous schema with a populated corpus.
        for table, column in REFERENCES:
            store.db.execute(f"DROP INDEX {table}_{column}")
        store.db.execute("PRAGMA user_version=5")
    for _ in range(2):
        with Store(tmp_path) as store:
            assert store.db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
            assert store.rows("SELECT id,legacy_evidence_path FROM observations") == [
                {"id": owners["observations"], "legacy_evidence_path": "old.png"}
            ]
            assert not store.rows("PRAGMA foreign_key_check")
            plan = store.rows("EXPLAIN QUERY PLAN " + EVIDENCE_REFERENCE_QUERY, ("missing",) * 5)
            details = [row["detail"] for row in plan]
            for table, column in REFERENCES:
                name = f"{table}_{column}"
                assert any(f"INDEX {name} " in line and "SEARCH " in line for line in details)
                definition = store.rows("SELECT sql FROM sqlite_master WHERE name=?", (name,))
                assert f"WHERE {column} IS NOT NULL" in definition[0]["sql"]
            assert not any(line.startswith("SCAN ") for line in details)
            # The upgrade SQL itself is safe to replay as well as reopening Store.
            store.db.executescript(MIGRATIONS[5])
    with sqlite3.connect(tmp_path / "results.pre-v5-migration.sqlite3") as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 5
        assert backup.execute("SELECT legacy_evidence_path FROM observations").fetchone()[0]


def test_fresh_database_reference_plan(tmp_path):
    with Store(tmp_path) as store:
        plan = store.rows("EXPLAIN QUERY PLAN " + EVIDENCE_REFERENCE_QUERY, ("missing",) * 5)
        assert not any(row["detail"].startswith("SCAN ") for row in plan)
        assert sum("SEARCH " in row["detail"] for row in plan) == 5


def test_index_upgrade_failure_rolls_back_and_keeps_backup(tmp_path, monkeypatch):
    with Store(tmp_path) as store:
        add_source(store, "video")
        for table, column in REFERENCES:
            store.db.execute(f"DROP INDEX {table}_{column}")
        store.db.execute("PRAGMA user_version=5")
    migrations = [*MIGRATIONS[:5], MIGRATIONS[5] + "\nSELECT * FROM missing_table;\n"]
    monkeypatch.setattr("tf2scan.storage.MIGRATIONS", migrations)
    with pytest.raises(sqlite3.OperationalError, match="missing_table"):
        Store(tmp_path)
    for path in (tmp_path / "results.sqlite3", tmp_path / "results.pre-v5-migration.sqlite3"):
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 5
            assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == 1
            for table, column in REFERENCES:
                assert not db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name=?", (f"{table}_{column}",)
                ).fetchall()


@pytest.mark.parametrize("table,column", REFERENCES)
def test_shared_reference_survives_until_last_owner_commits(tmp_path, table, column):
    value = f"{FOLDER}/1-row.png"
    path = tmp_path / value
    path.parent.mkdir(parents=True)
    path.write_bytes(b"shared evidence")
    with Store(tmp_path) as store:
        first, second = add_source(store, "first"), add_source(store, "second")
        with store.transaction() as db:
            db.execute(
                "UPDATE observations SET crop_path=? WHERE id=?", (value, first["observations"])
            )
            db.execute(f"UPDATE {table} SET {column}=? WHERE id=?", (value, second[table]))
        with store.transaction() as db:
            db.execute(
                "UPDATE observations SET crop_path=NULL WHERE id=?", (first["observations"],)
            )
        store.prune_evidence_paths([value, value, None])
        assert path.read_bytes() == b"shared evidence"
        with pytest.raises(RuntimeError), store.transaction() as db:
            db.execute(f"UPDATE {table} SET {column}=NULL WHERE id=?", (second[table],))
            raise RuntimeError("failed reference removal")
        store.prune_evidence_paths([value])
        assert path.exists()
        with store.transaction() as db:
            db.execute(f"UPDATE {table} SET {column}=NULL WHERE id=?", (second[table],))
        profile = ScanProfile()
        store.prune_evidence_paths([value, value], profile)
        assert not path.exists()
        assert profile.counts["referenced_paths_examined"] == 1
        assert profile.seconds["evidence_reference_lookup"] > 0


def test_targeted_cleanup_preserves_unowned_paths(tmp_path):
    values = [
        "outside-row.png",
        "report/assets/old/1-row.png",
        f"{FOLDER}/notes.txt",
        f"{FOLDER}/1-row.png." + "c" * 32 + ".tmp",
        f"{FOLDER}/../../../escaped-row.png",
    ]
    for value in values:
        path = tmp_path / value
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"keep")
    absolute = tmp_path / FOLDER / "2-row.png"
    absolute.write_bytes(b"keep absolute")
    with Store(tmp_path) as store:
        profile = ScanProfile()
        store.prune_evidence_paths([*values, str(absolute)], profile)
        assert profile.counts["referenced_paths_examined"] == 0
        assert all((tmp_path / value).read_bytes() == b"keep" for value in values)
        assert absolute.exists()
