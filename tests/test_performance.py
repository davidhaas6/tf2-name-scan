from test_detection import Recognizer, box, frame

from tf2scan.config import Config
from tf2scan.detection import FakeDetector
from tf2scan.ingestion import ingest
from tf2scan.performance import ScanProfile
from tf2scan.storage import Store


def test_profile_reports_stage_totals_and_realtime_factor():
    profile = ScanProfile()
    with profile.measure("decoding"):
        pass
    profile.frames = 12
    profile.crops = 100
    profile.source_resolution = [640, 360]
    profile.working_resolution = [640, 360]
    result = profile.result({"id": "clip", "duration_s": 12}, {"sampling": {"fps": 1}})
    assert result["wall_s"] >= result["stages_s"]["decoding"]
    assert result["realtime_factor"] == round(result["wall_s"] / 12, 3)
    assert result["frames"] == 12
    assert result["source_resolution"] == [640, 360]
    assert result["settings"]["sampling"]["fps"] == 1


def test_persistence_detail_counts_batch_and_orphan_sweep(tmp_path):
    config = Config(tmp_path / "config.yaml", {})
    profile = ScanProfile()
    with Store(config.root) as store:
        store.upsert_video({"id": "clip", "title": "clip", "source_url": "fixture",
                            "duration_s": 1})
        assert ingest(store, config, store.video("clip"), Recognizer(),
                      detector=FakeDetector({"0": [box()]}), frame_source=[frame()],
                      profile=profile)
        result = profile.result(store.video("clip"), {})
    counts = result["persistence_counts"]
    assert counts["batch_commits"] == 1
    assert counts["compaction_calls"] == 1
    assert counts["orphan_sweep_calls"] == 1
    assert counts["images_saved"] == 1
    assert counts["asset_files_checked"] >= 1
    detail = result["persistence_detail_s"]
    assert detail["transaction_total"] >= detail["image_save"]
    assert detail["compaction_total"] >= detail["reference_lookup"]
    assert detail["compaction_total"] >= detail["file_walk"]
    assert result["stages_s"]["persistence_evidence"] >= detail["transaction_total"]
    assert result["persistence_batches"][0]["frames"] == 1
    assert result["persistence_batches"][0]["accepted_observations"] == 1
    assert result["persistence_batches"][0]["asset_files_checked"] >= 1
