import json
import shutil
import subprocess

import numpy as np
import pytest
from PIL import Image, ImageDraw

from tf2scan.config import Config
from tf2scan.contracts import AdapterMetadata, Detection, Frame
from tf2scan.crops import canonicalize, geometry_metadata, prepare_crop
from tf2scan.detected_ingestion import recognize_frame
from tf2scan.detection import FakeDetector, detect_frames, detector_input, to_source_detection
from tf2scan.frames import detector_frame, sample_frames
from tf2scan.geometry import decode_polygon
from tf2scan.ingestion import ingest
from tf2scan.recognize import Recognition
from tf2scan.report import export_report
from tf2scan.settings import merge_settings
from tf2scan.storage import Store


def frame(key="0", size=(320, 180), source=None):
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).rectangle((20, 30, 99, 49), fill="black")
    return Frame(float(key), key, *(source or size), image)


def box(identity="box", x=20, y=30, width=80, height=20):
    return Detection(
        identity, ((x, y), (x + width, y), (x + width, y + height), (x, y + height)), 0.9
    )


class Recognizer:
    metadata = AdapterMetadata("fake-recognizer", "1", None, "python")

    def __init__(self):
        self.calls = []
        self.count = 0

    def recognize(self, crops):
        self.calls.append(crops)
        results = []
        for _ in crops:
            self.count += 1
            results.append(Recognition(f"Player{self.count}", None))
        return results


def test_rectification_pixels_padding_and_transform():
    crop = prepare_crop(frame(), box(), padding=2)
    assert crop.image.size == (84, 24)
    assert crop.image.getpixel((40, 10)) == (0, 0, 0)
    assert crop.image.getpixel((0, 0)) == (255, 255, 255)
    mapping = np.array(crop.crop_to_source)
    assert np.allclose(mapping @ (2, 2, 1), (20, 30, 1))
    edge = prepare_crop(frame(), box(x=0, y=0), padding=3)
    assert edge.image.size == (86, 26)
    assert edge.image.getpixel((0, 0)) == (255, 255, 255)
    assert np.allclose(np.array(edge.crop_to_source) @ (3, 3, 1), (0, 0, 1))


def test_rotated_perspective_and_reordered_quad():
    polygon = ((20, 20), (120, 40), (110, 65), (15, 45))
    crop = prepare_crop(frame(), Detection("rotated", polygon[::-1], 1), padding=2)
    assert canonicalize(polygon[::-1]) == polygon
    mapping = np.asarray(crop.crop_to_source)
    w, h = crop.image.size
    for point, expected in zip(((2, 2), (w - 2, 2), (w - 2, h - 2), (2, h - 2)), polygon):
        actual = mapping @ (*point, 1)
        assert np.allclose(actual[:2] / actual[2], expected)


@pytest.mark.parametrize(
    "polygon",
    [
        ((0, 0),) * 4,
        ((0, 0), (10, 0), (5, 1), (0, 10)),
        ((float("nan"), 0), (10, 0), (10, 10), (0, 10)),
        ((-1, 0), (10, 0), (10, 10), (-1, 10)),
        ((0, 0), (10, 0), (10, 5), (0, 5)),
    ],
)
def test_invalid_or_tiny_boxes(polygon):
    with pytest.raises(ValueError):
        prepare_crop(frame(), Detection("invalid", polygon, 1))


def test_minimum_height_uses_source_pixels_and_resize_maps_back():
    source = frame(size=(320, 180), source=(640, 360))
    crop = prepare_crop(source, box(width=80, height=6), padding=0)
    assert crop.image.height == 3
    tensor, scale = detector_input(source, "min", 960)
    assert tensor.size == (1707, 960)
    detection = to_source_detection(
        "scaled",
        ((0, 0), (tensor.width, 0), (tensor.width, tensor.height), (0, tensor.height)),
        0.8,
        scale,
    )
    assert np.allclose(detection.polygon, ((0, 0), (640, 0), (640, 360), (0, 360)))
    native, native_scale = detector_input(source)
    assert native is source.image and native_scale == (2, 2)


def test_region_metadata():
    assert geometry_metadata(box(x=0, y=0, width=30, height=12).polygon, 300, 180) == (
        "top_left",
        0.1,
        12 / 180,
    )
    assert geometry_metadata(box(x=230, y=140, width=60).polygon, 300, 180)[0] == "bottom_right"


def test_top_right_detector_crop_preserves_source_coordinates():
    original = frame(size=(320, 180), source=(640, 360))
    cropped = detector_frame(original, "top_right")
    assert cropped.image.size == (160, 90)
    assert cropped.source_region == (320, 0, 640, 180)
    assert cropped.working_to_source == (2, 2)
    assert detector_input(cropped)[1] == (2, 2)
    assert to_source_detection("name", ((0, 0), (40, 0), (40, 10), (0, 10)),
                               0.9, cropped.working_to_source,
                               cropped.source_offset).polygon == (
                                   (320, 0), (400, 0), (400, 20), (320, 20))
    assert detector_frame(original) is original


def test_top_right_crop_is_applied_before_detection(tmp_path):
    config = Config(tmp_path / "config.yaml", {"sampling": {"region": "top_right"}})
    detector = FakeDetector({"0": [box(x=220, y=30)]})
    seen = []
    original_detect = detector.detect

    def detect(frames):
        seen.extend((item.image.size, item.source_offset) for item in frames)
        return original_detect(frames)

    detector.detect = detect
    with Store(config.root) as store:
        store.upsert_video({"id": "v", "title": "video", "source_url": "url"})
        assert ingest(store, config, store.video("v"), Recognizer(), detector=detector,
                      frame_source=[frame(size=(320, 180))])
        assert seen == [((160, 90), (160.0, 0))]
        observation = store.rows("SELECT polygon_blob FROM observations")[0]
        assert decode_polygon(observation["polygon_blob"]) == box(x=220, y=30).polygon


def test_order_identity_and_batch_cardinality():
    recognizer = Recognizer()
    settings = merge_settings({"recognizer": {"batch_size": 1}})
    detections = [box("second"), box("invalid", height=2), box("first", x=110)]
    results = recognize_frame(frame(), detections, recognizer, settings)
    assert [(crop.detection.identity, result.text) for crop, result in results] == [
        ("second", "Player1"),
        ("first", "Player2"),
    ]
    assert len(recognizer.calls) == 2
    recognizer.recognize = lambda _: []
    with pytest.raises(RuntimeError, match="number of crops"):
        recognize_frame(frame(), detections, recognizer, settings)
    detector = FakeDetector({"0": [box(), box()]})
    with pytest.raises(RuntimeError, match="unique"):
        detect_frames(detector, [frame()])
    detector.detect = lambda _: []
    with pytest.raises(RuntimeError, match="frame results"):
        detect_frames(detector, [frame()])


def test_full_frame_ingestion_lineage_and_empty_frames(tmp_path):
    config = Config(tmp_path / "config.yaml", {"detector": {"batch_size": 2}})
    detector = FakeDetector(
        {"0": [box("right", x=220), box("left", x=0)], "2": [box("tiny", height=2)]}
    )
    with Store(config.root) as store:
        store.upsert_video({"id": "v", "title": "video", "source_url": "url"})
        assert ingest(
            store,
            config,
            store.video("v"),
            Recognizer(),
            detector=detector,
            frame_source=(frame(str(i)) for i in range(3)),
        )
        assert detector.calls == [("0", "1"), ("2",)]
        frames = store.rows("SELECT * FROM sampled_frames ORDER BY timestamp_s")
        assert [f["raw_detection_count"] for f in frames] == [2, 0, 1]
        assert [f["recognition_count"] for f in frames] == [2, 0, 0]
        observations = store.rows("SELECT * FROM observations ORDER BY id")
        assert [o["detection_identity"] for o in observations] == ["left", "right"]
        assert observations[0]["raw_text"] == "Player1"
        assert decode_polygon(observations[0]["polygon_blob"]) == box(x=0).polygon
        assert observations[0]["screen_region"] == "top_left"
        assert json.loads(observations[0]["crop_transform_json"])
        assert all((config.root / o["crop_path"]).exists() for o in observations)
        assert not store.rows("PRAGMA foreign_key_check")
        export_report(store, rows=True)
        exported = json.loads((config.root / "rows.jsonl").read_text().splitlines()[0])
        assert exported["representative_polygon"]
        assert "representative_polygon_blob" not in exported
        assert len(store.rows("SELECT * FROM cluster_observations")) == 2


def test_detector_failure_preserves_completed_selection(tmp_path):
    config = Config(tmp_path / "config.yaml", {})
    with Store(config.root) as store:
        store.upsert_video({"id": "v", "title": "video", "source_url": "url"})
        detector = FakeDetector({"0": [box()]})
        ingest(
            store,
            config,
            store.video("v"),
            Recognizer(),
            detector=detector,
            frame_source=iter([frame()]),
        )
        selected = store.selected_scan_ids()
        detector.detect = lambda _: []
        with pytest.raises(RuntimeError):
            ingest(
                store,
                config,
                store.video("v"),
                Recognizer(),
                detector=detector,
                reprocess=True,
                frame_source=iter([frame()]),
            )
        assert store.selected_scan_ids() == selected
        assert store.rows("SELECT error FROM chunk_attempts WHERE error IS NOT NULL")


def test_source_closed_on_detector_error(tmp_path):
    closed = []

    def source():
        try:
            yield frame()
            yield frame("1")
        finally:
            closed.append(True)

    config = Config(tmp_path / "config.yaml", {})
    detector = FakeDetector({})
    detector.detect = lambda _: []
    with Store(config.root) as store:
        store.upsert_video({"id": "v", "title": "video", "source_url": "url"})
        with pytest.raises(RuntimeError):
            ingest(
                store,
                config,
                store.video("v"),
                Recognizer(),
                detector=detector,
                frame_source=source(),
            )
        assert closed == [True]
        assert not list((config.root / "report/assets").rglob("*.png"))
        assert not store.rows("SELECT * FROM observations")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg not installed")
def test_real_ffmpeg_bounded_working_resolution(tmp_path):
    path = tmp_path / "large.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=1920x1080:r=2",
            "-t",
            "1",
            "-c:v",
            "mpeg4",
            str(path),
        ],
        check=True,
    )
    records = list(sample_frames(path, fps=2, chunk_id=7))
    assert [f.timestamp_s for f in records] == [0, 0.5]
    assert records[0].image.size == (1280, 720)
    assert (records[0].source_width, records[0].source_height) == (1920, 1080)
    assert records[0].working_to_source == (1.5, 1.5)
    assert records[0].chunk_id == 7
