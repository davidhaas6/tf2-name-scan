import json
from dataclasses import replace
from itertools import permutations
from types import SimpleNamespace

import pytest
from test_detection import Recognizer, box, frame

from tf2scan.clustering import TextTracker
from tf2scan.config import Config
from tf2scan.contracts import AdapterMetadata
from tf2scan.crops import prepare_crop
from tf2scan.detection import FakeDetector
from tf2scan.frames import detector_frame
from tf2scan.ingestion import ingest
from tf2scan.paddle_backend import PaddleDetector, PaddleRecognizer
from tf2scan.query import run_query
from tf2scan.recognize import Recognition, file_hash
from tf2scan.report import export_report
from tf2scan.retention import overlap, select_crops, useful_text
from tf2scan.settings import merge_settings
from tf2scan.storage import Store


def test_polygon_overlap_and_retention_before_cap():
    settings = merge_settings({"crops": {"max_per_frame": 2}})
    proposals = [
        box("a"),
        replace(box("duplicate", x=21), confidence=0.8),
        box("b", x=120),
        box("c", x=220),
        box("tiny", height=2),
        replace(box("weak", y=80), confidence=0.1),
    ]
    for candidates in (proposals, proposals[::-1]):
        crops, duplicates, capped = select_crops(frame(), candidates, settings)
        assert [c.detection.identity for c in crops] == ["a", "b"]
        assert (duplicates, capped) == (1, 1)
    assert overlap(box().polygon, box().polygon) == (1, 1)
    assert overlap(box().polygon, box(x=120).polygon) == (0, 0)
    assert overlap(box().polygon, box(x=30, width=20).polygon)[1] == 1
    # Intersecting bounding rectangles are insufficient for tilted polygons.
    a = ((0, 0), (100, 100), (100, 110), (0, 10))
    b = ((0, 20), (100, 120), (100, 130), (0, 30))
    assert overlap(a, b) == (0, 0)
    for candidates in permutations([box("z"), box("a")]):
        crops, duplicates, capped = select_crops(frame(), candidates, settings)
        assert crops[0].detection.identity == "a"
        assert duplicates == 1


@pytest.mark.parametrize(
    "text,confidence,keep",
    [
        ("玩家", None, True),
        ("é2", 0.1, True),
        ("a!", 1, False),
        ("123", 0.09, False),
        (" \t", None, False),
        ("!?", 1, False),
        ("ab", float("nan"), False),
        ("ab", 1.1, False),
        ("ab", 0, False),
    ],
)
def test_unicode_retention(text, confidence, keep):
    assert useful_text(Recognition(text, confidence), 0.1) == keep


def accepted(y=30, text="Player", x=20):
    return (prepare_crop(frame(), box(x=x, y=y)), Recognition(text, 0.8))


def test_temporal_gap_motion_and_same_frame_association():
    tracker = TextTracker(merge_settings({})["clustering"])
    first = accepted()
    tracker.associate(frame(), [first])
    tracker.observe(1, frame(), *first)
    moving = accepted(y=24)
    matches, closed = tracker.associate(frame("1"), [moving, accepted(x=22, y=24)])
    assert len(matches) == 1 and not closed
    tracker.observe(1, frame("1"), *moving)
    matches, closed = tracker.associate(frame("2"), [accepted(y=18)])
    assert matches[0][0] == 1 and not closed
    tracker.observe(1, frame("2"), *accepted(y=18))
    # Returning to an old insertion position contradicts upward motion.
    matches, closed = tracker.associate(frame("3"), [accepted(y=24)])
    assert not matches and closed == {1: "incompatible_motion"}

    tracker = TextTracker(merge_settings({})["clustering"])
    tracker.associate(frame(), [first])
    tracker.observe(2, frame(), *first)
    assert tracker.associate(frame("3"), [first])[0][0][0] == 2
    assert tracker.associate(frame("3.001"), [first])[1] == {2: "time_gap"}
    with pytest.raises(ValueError, match="increasing"):
        tracker.associate(frame("3.001"), [])


def test_association_is_frame_wide_order_independent_and_spatial():
    for items in ([accepted(x=28), accepted(x=20)], [accepted(x=20), accepted(x=28)]):
        tracker = TextTracker(merge_settings({})["clustering"])
        tracker.associate(frame(), [])
        tracker.observe(1, frame(), *accepted())
        matches, closed = tracker.associate(frame("1"), items)
        assert len(matches) == 1 and not closed
        assert items[next(iter(matches))][0].detection.polygon == box().polygon
    tracker = TextTracker(merge_settings({})["clustering"])
    tracker.associate(frame(), [])
    tracker.observe(1, frame(), *accepted())
    matches, closed = tracker.associate(frame("1"), [accepted(), accepted(text="Different")])
    assert matches[0][0] == 1 and not closed
    assert not tracker.associate(frame("2"), [accepted(x=200)])[0]


def test_accounting_clusters_and_truthful_representative(tmp_path):
    config = Config(tmp_path / "config.yaml", {"crops": {"max_per_frame": 2}})
    detector = FakeDetector(
        {
            str(i): [
                box(),
                box("duplicate", x=21),
                box("bad", x=120),
                box("capped", x=220),
                box("tiny", height=2),
            ]
            for i in range(2)
        }
    )
    rec = Recognizer()
    results = iter(
        [
            Recognition("玩家", 0.3),
            Recognition("!", 1),
            Recognition("玩家", 0.9),
            Recognition("ab", 0.01),
        ]
    )
    rec.recognize = lambda crops: [next(results) for _ in crops]
    with Store(config.root) as store:
        store.upsert_video({"id": "v", "title": "video", "source_url": "url"})
        ingest(
            store,
            config,
            store.video("v"),
            rec,
            detector=detector,
            frame_source=[frame(), frame("1"), frame("5")],
        )
        rows = store.rows("SELECT * FROM sampled_frames ORDER BY timestamp_s")
        keys = (
            "raw_detection_count",
            "duplicate_detection_count",
            "recognition_count",
            "accepted_observation_count",
            "cap_dropped_count",
        )
        assert [tuple(r[k] for k in keys) for r in rows] == [(5, 1, 2, 1, 1)] * 2 + [
            (0, 0, 0, 0, 0)
        ]
        clusters = store.rows("SELECT * FROM text_clusters")
        assert len(clusters) == 1
        c = clusters[0]
        assert (c["support_count"], c["start_s"], c["end_s"], c["close_reason"]) == (
            2,
            0,
            1,
            "time_gap",
        )
        representative = store.rows(
            "SELECT * FROM observations WHERE id=?", (c["representative_observation_id"],)
        )[0]
        assert c["best_confidence"] == 0.9 and c["evidence_timestamp_s"] == 1
        assert c["evidence_path"] == representative["crop_path"]
        assert (store.root / c["evidence_path"]).is_file()
        assert json.loads(c["motion_summary_json"])["last_timestamp_s"] == 1
        assert not store.rows("PRAGMA foreign_key_check")


def test_sparse_evidence_reports_actual_observation_and_missing_crop(tmp_path):
    config = Config(tmp_path / "config.yaml", {"evidence": {"full_frames": True}})
    detector = FakeDetector({"0": [box()], "1": [box()]})
    rec = Recognizer()
    results = iter([Recognition("PlayerOne", 0.9), Recognition("Player0ne", 0.8)])
    rec.recognize = lambda crops: [next(results) for _ in crops]
    with Store(config.root) as store:
        store.upsert_video({"id": "v", "title": "video", "source_url": "https://example.com/v"})
        ingest(store, config, store.video("v"), rec, detector=detector,
               frame_source=[frame(), frame("1")])
        observations = store.rows("SELECT * FROM observations ORDER BY id")
        assert len(observations) == 2
        assert observations[0]["crop_path"]
        assert observations[1]["crop_path"] is None
        assert store.rows("SELECT full_frame_path FROM sampled_frames ORDER BY id")[1]["full_frame_path"] is None
        query_id = run_query(store, "Player0ne")
        hit = export_report(store, query_id, text_clusters=True)[0]
        assert hit["matched_observation_id"] == observations[1]["id"]
        assert hit["matched_timestamp_s"] == 1
        assert hit["evidence_observation_id"] == observations[0]["id"]
        assert hit["evidence_kind"] == "representative"
        assert hit["observation_evidence_available"] is False
        assert "Matching observation crop unavailable" in (
            config.root / "report/index.html").read_text(encoding="utf-8")
        exported = json.loads((config.root / "text_clusters.jsonl").read_text(encoding="utf-8"))
        assert exported["schema_version"] == 2
        assert exported["observations"][1]["polygon"]
        assert exported["scan_run_id"]


def test_configured_query_candidate_crops_are_bounded(tmp_path):
    config = Config(tmp_path / "config.yaml", {
        "query": {"target_name": "Player0ne"},
        "evidence": {"max_candidates": 1},
    })
    detector = FakeDetector({str(i): [box()] for i in range(3)})
    rec = Recognizer()
    results = iter([Recognition("PlayerOne", 0.9),
                    Recognition("Player0ne", 0.8), Recognition("Player0ne", 0.7)])
    rec.recognize = lambda crops: [next(results) for _ in crops]
    with Store(config.root) as store:
        store.upsert_video({"id": "v", "title": "video", "source_url": "url"})
        ingest(store, config, store.video("v"), rec, detector=detector,
               frame_source=[frame(str(i)) for i in range(3)])
        observations = store.rows("SELECT id,crop_path FROM observations ORDER BY id")
        assert len(observations) == 3
        assert sum(bool(o["crop_path"]) for o in observations) == 2
        assert observations[0]["crop_path"]
        query_id = run_query(store, "Player0ne")
        hit = export_report(store, query_id)[0]
        assert hit["observation_evidence_available"]


def test_paddle_order_mapping_color_and_cardinality(monkeypatch):
    monkeypatch.setattr(
        "tf2scan.paddle_backend.metadata", lambda *args: AdapterMetadata("test", "1", None, "test")
    )
    calls = []

    def detect(image, **kwargs):
        calls.append(kwargs)
        return [{"dt_polys": [box().polygon], "dt_scores": [0.9]}]

    detector = PaddleDetector(SimpleNamespace(predict=detect), merge_settings({})["detector"])
    assert detector.detect([frame(source=(640, 360))])[0][0].polygon[0] == (40, 60)
    assert detector.detect([detector_frame(frame(source=(640, 360)), "top_right")])[0][0].polygon[0] == (360, 60)
    assert calls[0]["limit_side_len"] == 320 and calls[0]["limit_type"] == "max"
    engine = SimpleNamespace(
        predict=lambda images, **kwargs: [
            {"rec_text": str(int(im[0, 0, 2])), "rec_score": 0.8} for im in images
        ]
    )
    rec = PaddleRecognizer(engine, merge_settings({"recognizer": {"batch_size": 1}})["recognizer"])
    from PIL import Image

    assert [
        r.text for r in rec.recognize([Image.new("RGB", (5, 5), (v, 2, 3)) for v in [10, 20]])
    ] == ["10", "20"]
    engine.predict = lambda *args, **kwargs: []
    with pytest.raises(RuntimeError, match="number of crops"):
        rec.recognize([frame().image])


def test_model_directory_hash_includes_dictionary_and_weights(tmp_path):
    (tmp_path / "inference.pdiparams").write_bytes(b"weights")
    (tmp_path / "inference.yml").write_text("dictionary: first")
    before = file_hash(tmp_path)
    (tmp_path / "inference.yml").write_text("dictionary: second")
    assert before != file_hash(tmp_path)


def test_cli_local_detection_wiring_and_model_lifetime(tmp_path, monkeypatch):
    from tf2scan.cli import execute, parser

    config = tmp_path / "config.yaml"
    config.write_text("output_dir: output\n")
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fixture")
    monkeypatch.setattr("tf2scan.indexing.probe", lambda _: ({"height": 180}, 1))
    detector, recognizer = FakeDetector({"0": [box()]}), Recognizer()
    closed = []
    detector.close = lambda: closed.append("detector")
    recognizer.close = lambda: closed.append("recognizer")
    monkeypatch.setattr("tf2scan.paddle_backend.create_models", lambda _: (detector, recognizer))
    monkeypatch.setattr("tf2scan.detected_ingestion.sample_frames", lambda *a, **k: [frame()])
    args = parser().parse_args(["scan", "--config", str(config), "--local", str(video)])
    assert execute(args) == 0
    assert closed == ["recognizer", "detector"]
    with Store(tmp_path / "output") as store:
        assert len(store.selected_scan_ids()) == 1
        assert (
            store.rows("SELECT close_reason FROM text_clusters")[0]["close_reason"] == "end_of_scan"
        )


def test_model_identity_mismatch_fails(tmp_path):
    from tf2scan.paddle_backend import metadata

    (tmp_path / "inference.yml").write_text("Global: {model_name: other}")
    engine = SimpleNamespace(paddlex_predictor=SimpleNamespace(model_dir=tmp_path))
    with pytest.raises(ValueError, match="loaded artifacts"):
        metadata(engine, "expected", {})
