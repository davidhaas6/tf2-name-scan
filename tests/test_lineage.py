import json
import sqlite3
import struct

import pytest

from tf2scan.config import Config
from tf2scan.geometry import decode_polygon, encode_polygon, geometry_key
from tf2scan.lineage import insert
from tf2scan.query import run_query
from tf2scan.report import export_report
from tf2scan.storage import MIGRATIONS, Store


def legacy_database(root, version):
    db = sqlite3.connect(root / "results.sqlite3")
    for sql in MIGRATIONS[:version]:
        db.executescript(sql)
    db.execute(f"PRAGMA user_version={version}")
    insert(
        db,
        "videos",
        id="video",
        source_url="https://example.com/video",
        title="old",
        status="scanned",
        scan_signature="original-signature",
        scanned_at="2022-01-01",
    )
    if version == 2:
        db.execute("UPDATE videos SET scan_config_json=?", ('{"old": true}',))
    insert(
        db,
        "row_clusters",
        id=42,
        video_id="video",
        start_s=0,
        end_s=1,
        row_index=0,
        canonical_text="PlayerOne",
        normalized_text="playerone",
        compact_text="playerone",
        best_confidence=0.9,
        support_count=2,
        hud_profile="old-hud",
        hud_config_json="{}",
        model_version="old-model",
        normalization_version="old-normalization",
        evidence_path="report/assets/old.png",
        frame_path="report/assets/old.jpg",
    )
    if version == 2:
        db.execute("UPDATE row_clusters SET evidence_timestamp_s=1,evidence_row_index=0")
    for i in (0, 1):
        insert(
            db,
            "observations",
            id=i + 20,
            video_id="video",
            row_cluster_id=42,
            timestamp_s=i,
            row_index=0,
            raw_text="PlayerOne",
            normalized_text="playerone",
            compact_text="playerone",
            ocr_confidence=0.9,
            model_version="old-model",
            normalization_version="old-normalization",
            hud_profile="old-hud",
            crop_path="report/assets/old.png",
        )
    insert(
        db,
        "queries",
        id=7,
        target_name="PlayerOne",
        aliases_json='["PlayerOne"]',
        matcher_version="old-matcher",
    )
    insert(
        db,
        "hits",
        id=9,
        row_cluster_id=42,
        query_id=7,
        matched_alias="PlayerOne",
        best_text="PlayerOne",
        best_score=1,
        support_count=2,
        review_status="confirmed",
        reviewer_note="keep note",
        evidence_path="report/assets/old.png",
    )
    insert(
        db, "query_matches", query_id=7, observation_id=20, matched_alias="PlayerOne", match_score=1
    )
    db.commit()
    db.close()
    (root / "report/assets").mkdir(parents=True)
    for name in ("old.png", "old.jpg"):
        (root / "report/assets" / name).write_bytes(b"unchanged-evidence")


@pytest.mark.parametrize("version", [1, 2])
def test_populated_migration_preserves_corpus_reviews_and_evidence(tmp_path, version):
    legacy_database(tmp_path, version)
    with Store(tmp_path) as store:
        assert not store.rows("PRAGMA foreign_key_check")
        assert store.selected_clusters()[0]["id"] == 42
        run = store.rows("SELECT * FROM scan_runs")[0]
        assert run["legacy"] == 1 and run["config_hash"] == "original-signature"
        assert run["detector_weights_hash"] is None
        assert json.loads(run["config_json"]) == ({"old": True} if version == 2 else {})
        assert all(
            f["raw_detection_count"] is None for f in store.rows("SELECT * FROM sampled_frames")
        )
        observations = store.rows("SELECT * FROM observations ORDER BY id")
        assert all(o["polygon_blob"] is None for o in observations)
        assert observations[0]["crop_path"] is None
        assert observations[1]["crop_path"] == ("report/assets/old.png" if version == 2 else None)
        assert all(o["legacy_evidence_path"] for o in observations)
        hit = export_report(store, 7, rows=True)[0]
        assert (hit["id"], hit["review_status"], hit["reviewer_note"]) == (
            9,
            "confirmed",
            "keep note",
        )
        assert len(store.rows("SELECT * FROM cluster_observations")) == 2
        assert len(store.rows("SELECT * FROM query_matches")) == 1
        assert run_query(store, "PlayerOne")
        assert (tmp_path / "report/assets/old.png").read_bytes() == b"unchanged-evidence"
        assert json.loads((tmp_path / "rows.jsonl").read_text())["canonical_text"] == "PlayerOne"
    with sqlite3.connect(tmp_path / f"results.pre-v{version}-migration.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == version
        assert db.execute("SELECT reviewer_note FROM hits").fetchone()[0] == "keep note"


def test_migration_failure_is_atomic_and_backup_recoverable(tmp_path, monkeypatch):
    legacy_database(tmp_path, 2)

    def fail(db):
        raise RuntimeError("simulated import failure")

    monkeypatch.setattr("tf2scan.storage.migrate_legacy", fail)
    with pytest.raises(RuntimeError):
        Store(tmp_path)
    with sqlite3.connect(tmp_path / "results.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == 2
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='scan_runs'").fetchone() is None
        )


def test_scan_selection_resume_and_immutable_provenance(tmp_path):
    config = Config(tmp_path / "config.yaml", {})
    snapshot, signature = config.effective_scan()
    with Store(tmp_path) as store:
        store.upsert_video({"id": "v", "title": "video", "source_url": "url"})
        first = store.start_scan("v", snapshot, signature)
        store.finish_scan(first)
        second = store.start_scan("v", snapshot, signature)
        assert store.selected_scan_ids() == [first]
        store.finish_scan(second, "failed")
        assert store.selected_scan_ids() == [first]
        assert store.selected_scan_ids(scan_id=second) == [second]
        assert store.start_scan("v", snapshot, signature, resume=True) == second
        with pytest.raises(ValueError, match="mismatch"):
            store.start_scan("v", snapshot, "changed", resume=True)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"), store.transaction() as db:
            db.execute("UPDATE scan_runs SET config_json='{}'")
        store.finish_scan(second)
        assert store.selected_scan_ids() == [second]
        assert store.selected_scan_ids(run_id=1) == [first]


def test_delete_video_keeps_shared_run_and_other_source(tmp_path):
    with Store(tmp_path) as store:
        for video_id in ("a", "b"):
            store.upsert_video({"id": video_id, "title": video_id, "source_url": "url"})
        scan_a = store.start_scan("a", "{}", "shared", legacy=True)
        run_id = store.rows("SELECT scan_run_id FROM video_scans WHERE id=?", (scan_a,))[0][
            "scan_run_id"
        ]
        with store.transaction() as db:
            insert(db, "video_scans", scan_run_id=run_id, video_id="b", status="completed")
        store.delete_video("a")
        assert store.video("b")["title"] == "b"
        assert store.rows("SELECT id FROM scan_runs WHERE id=?", (run_id,))
        assert len(store.rows("SELECT id FROM video_scans WHERE scan_run_id=?", (run_id,))) == 1


def test_geometry_codec():
    polygon = ((1, 2), (11, 2), (11, 8), (1, 8))
    encoded = encode_polygon(polygon)
    assert encoded == struct.pack("<8f", 1, 2, 11, 2, 11, 8, 1, 8)
    assert decode_polygon(encoded) == polygon
    assert isinstance(geometry_key(polygon), str)
    for invalid in (polygon[::-1], ((0, 0),) * 4, ((float("nan"), 0), *polygon[1:])):
        with pytest.raises(ValueError):
            encode_polygon(invalid)
    with pytest.raises(ValueError):
        decode_polygon(encoded, "unknown")


def test_cross_scan_links_representatives_and_sample_uniqueness(tmp_path):
    with Store(tmp_path) as store:
        store.upsert_video({"id": "v", "title": "video", "source_url": "url"})
        for scan in (1, 2):
            store.start_scan("v", "{}", "legacy", legacy=True)
            with store.transaction() as db:
                chunk = insert(
                    db,
                    "scan_chunks",
                    video_scan_id=scan,
                    sequence_no=0,
                    chunk_start_s=0,
                    download_mode="test",
                )
                frame = insert(
                    db,
                    "sampled_frames",
                    video_scan_id=scan,
                    scan_chunk_id=chunk,
                    sample_key="0",
                    timestamp_s=0,
                )
                insert(
                    db,
                    "observations",
                    video_scan_id=scan,
                    sampled_frame_id=frame,
                    geometry_key="test",
                    raw_text="hi",
                    normalized_text="hi",
                    compact_text="hi",
                )
                insert(
                    db,
                    "text_clusters",
                    video_scan_id=scan,
                    video_id="v",
                    start_s=0,
                    end_s=0,
                    canonical_text="hi",
                    normalized_text="hi",
                    compact_text="hi",
                    support_count=1,
                )
        for sql in (
            "INSERT INTO cluster_observations VALUES(1,2,1,1)",
            "UPDATE text_clusters SET representative_observation_id=2 WHERE id=1",
            "INSERT INTO sampled_frames(scan_chunk_id,video_scan_id,sample_key,timestamp_s) VALUES(1,1,'0',1)",
            "INSERT INTO scan_chunks(video_scan_id,sequence_no,chunk_start_s,download_mode) VALUES(1,0,0,'test')",
            "UPDATE observations SET polygon_blob=zeroblob(32) WHERE id=1",
        ):
            with pytest.raises(sqlite3.IntegrityError), store.transaction() as db:
                db.execute(sql)
