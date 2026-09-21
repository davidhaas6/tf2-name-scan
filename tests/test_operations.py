import json
import shutil
import subprocess

import pytest
from PIL import Image

from tf2scan.benchmark import benchmark
from tf2scan.config import load_config
from tf2scan.frames import sample_frames
from tf2scan.indexing import rejection
from tf2scan.recognize import Recognition
from tf2scan.storage import MIGRATIONS, Store


def test_filters():
    info = {"title": "TF2 gameplay", "upload_date": "20220101", "duration": 100}
    assert rejection(info, {"date_from": "2022-01-01", "date_to": "2022-01-01"}) is None
    assert rejection(info, {"title_include": "tf2", "max_duration_s": 200}) is None
    assert rejection(info, {"title_exclude": "gameplay"})
    assert rejection({}, {"min_duration_s": 10})
    assert rejection(info, {"date_from": "2023-01-01"})


def test_config_validation(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("scan: {fps: 0}")
    with pytest.raises(ValueError, match="fps"):
        load_config(config)
    config.write_text("scan: {fps: 2}\noutput_dir: elsewhere")
    assert load_config(config).root == tmp_path / "elsewhere"


def test_idempotent_index_and_migrations(tmp_path):
    with Store(tmp_path) as store:
        values = {"id": "v", "source_url": "url", "title": "first", "status": "pending"}
        store.upsert_video(values)
        store.update_video("v", status="scanned")
        store.upsert_video({**values, "title": "new", "status": "skipped"})
        assert store.video("v")["status"] == "scanned"
        assert store.video("v")["title"] == "new"
    with Store(tmp_path) as reopened:
        assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        assert reopened.video("v")["status"] == "scanned"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg not installed")
def test_real_ffmpeg_streaming_and_failure(tmp_path):
    video = tmp_path / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:r=10",
            "-t",
            "2",
            "-c:v",
            "mpeg4",
            str(video),
        ],
        check=True,
    )
    frames = list(sample_frames(video))
    assert [t for t, _ in frames] == [0, 1]
    assert frames[0][1].size == (1280, 720)
    with pytest.raises(subprocess.CalledProcessError):
        list(sample_frames(tmp_path / "missing.mp4"))


def test_benchmark_occurrences_and_split_leakage(tmp_path):
    class Model:
        model_version = "fake"

        def recognize(self, crops):
            return [Recognition("PlayerOne", 1) for _ in crops]

    Image.new("RGB", (100, 20)).save(tmp_path / "row.png")
    records = [
        {
            "image": "row.png",
            "source_id": "source",
            "visible_names": ["PlayerOne"],
            "text": "PlayerOne",
            "occurrence_id": "event",
            "timestamp_s": t,
        }
        for t in (0, 1)
    ]
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("\n".join(json.dumps(r) for r in records))
    result = benchmark(manifest, Model(), {"batch_size": 2}, tmp_path / "metrics.json")
    assert result["occurrence_recall"] == 1
    assert result["character_error_rate"] == 0
    assert result["false_candidates_per_query_video_hour"] is None
    records[1]["split"] = "train"
    manifest.write_text("\n".join(json.dumps(r) for r in records))
    with pytest.raises(ValueError, match="leaks"):
        benchmark(manifest, Model(), {"batch_size": 2}, tmp_path / "metrics.json")
