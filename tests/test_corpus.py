import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from tf2scan.config import DEFAULT_PROFILE, Config
from tf2scan.ingestion import ingest
from tf2scan.query import run_query
from tf2scan.recognize import Recognition
from tf2scan.report import export_report
from tf2scan.storage import Store


@pytest.fixture
def config(tmp_path):
    profile = {**DEFAULT_PROFILE, "row_count": 1}
    return Config(
        tmp_path / "config.yaml",
        {
            "pipeline": "legacy_hud",
            "profiles": {"default_tf2_720p": profile},
            "output_dir": "output",
            "scan": {
                "fps": 1,
                "batch_size": 2,
                "upscale": 2,
                "cluster_gap_s": 8,
                "min_contrast": 0,
                "min_edge_density": 0,
            },
        },
    )


class FakeRecognizer:
    model_version = "test-model-v1"

    def __init__(self, texts):
        self.texts = iter(texts)
        self.calls = 0

    def recognize(self, crops):
        self.calls += 1
        return [Recognition(next(self.texts), 0.9) for _ in crops]


def frames(times):
    for timestamp in times:
        frame = Image.new("RGB", (1280, 720), "black")
        ImageDraw.Draw(frame).text((700, 25), "Some killfeed text", fill="white")
        yield timestamp, frame


def add_video(store, video_id="test"):
    store.upsert_video(
        {
            "id": video_id,
            "source_url": "https://www.youtube.com/watch?v=test",
            "title": '<script>alert("title")</script>',
            "status": "downloaded",
            "hud_profile": "default_tf2_720p",
            "local_path": "missing.mp4",
        }
    )
    return store.video(video_id)


def test_rematch_uses_all_rows_and_preserves_corpus_and_reviews(config):
    with Store(config.root) as store:
        video = add_video(store)
        model = FakeRecognizer(
            ["AlphaPlayer killed Victim", "AlphaPlayer killed Victim", "OtherPlayer killed Enemy"]
        )
        ingest(store, config, video, model, frame_source=frames([0, 1, 20]))
        before = store.rows("SELECT * FROM observations")
        assert len(before) == 3
        assert len(store.rows("SELECT * FROM text_clusters")) == 2
        first = run_query(store, "AlphaPlayer")
        second = run_query(store, "OtherPlayer")
        assert first != second
        assert len(store.rows("SELECT * FROM hits WHERE query_id=?", (second,))) == 1
        assert store.rows("SELECT * FROM observations") == before
        assert model.calls == 2
        hit = store.rows("SELECT * FROM hits WHERE query_id=?", (second,))[0]
        with store.transaction() as db:
            db.execute("UPDATE hits SET review_status='confirmed' WHERE id=?", (hit["id"],))
        assert run_query(store, "OtherPlayer") == second
        assert (
            store.rows("SELECT * FROM hits WHERE id=?", (hit["id"],))[0]["review_status"]
            == "confirmed"
        )
        # Even after media disappears, completed ingestion is skipped before OCR.
        assert not ingest(store, config, store.video("test"), model)
        export_report(store, second, rows=True)
        report = (config.root / "report/index.html").read_text(encoding="utf-8")
        assert '<script>alert("title")' not in report
        assert "&lt;script&gt;" in report
        assert len((config.root / "rows.jsonl").read_text().splitlines()) == 2
        assert len(list((config.root / "report/assets").rglob("*.png"))) == 2


def test_failed_reprocess_rolls_back_corpus_and_evidence(config):
    with Store(config.root) as store:
        video = add_video(store)
        ingest(store, config, video, FakeRecognizer(["Original player"]), frame_source=frames([0]))
        before = store.rows("SELECT * FROM text_clusters")
        before_assets = set((config.root / "report/assets").rglob("*.png"))
        with pytest.raises(StopIteration):
            ingest(
                store,
                config,
                store.video("test"),
                FakeRecognizer([]),
                reprocess=True,
                frame_source=frames([0]),
            )
        assert store.rows("SELECT * FROM text_clusters") == before
        assert set((config.root / "report/assets").rglob("*.png")) == before_assets
        assert store.video("test")["status"] == "scanned"


def test_reprocess_preserves_history_and_delete_removes_evidence(config):
    with Store(config.root) as store:
        ingest(
            store,
            config,
            add_video(store),
            FakeRecognizer(["FirstPlayer"]),
            frame_source=frames([0]),
        )
        run_query(store, "FirstPlayer")
        old = store.rows("SELECT * FROM text_clusters")[0]
        ingest(
            store,
            config,
            store.video("test"),
            FakeRecognizer(["SecondPlayer"]),
            reprocess=True,
            frame_source=frames([1]),
        )
        assert store.rows("SELECT * FROM hits")
        assert (config.root / old["evidence_path"]).exists()
        assert len(store.rows("SELECT * FROM observations")) == 2
        assert store.selected_clusters()[0]["canonical_text"] == "SecondPlayer"
        assert export_report(store) == []
        assert export_report(store, scan_id=old["video_scan_id"])[0]["best_text"] == "FirstPlayer"
        store.delete_video("test")
        assert not store.rows("SELECT * FROM observations")
        assert not list((config.root / "report/assets").rglob("*.png"))


def test_blank_rows_discarded_but_borderline_retained(config):
    with Store(config.root) as store:
        ingest(
            store,
            config,
            add_video(store),
            FakeRecognizer(["", "abcdefghX"]),
            frame_source=frames([0, 1]),
        )
        assert len(store.rows("SELECT * FROM observations")) == 1
        run_query(store, "abcdefghY")
        assert not store.rows("SELECT * FROM hits")
        assert store.rows("SELECT * FROM query_matches")


def test_export_has_provenance(config):
    with Store(config.root) as store:
        ingest(
            store, config, add_video(store), FakeRecognizer(["PlayerOne"]), frame_source=frames([0])
        )
        export_report(store, rows=True)
        row = json.loads((config.root / "rows.jsonl").read_text())
        for key in ("model_version", "normalization_version", "hud_config_json", "source_url"):
            assert row[key]
        assert Path(config.root / row["evidence_path"]).exists()
