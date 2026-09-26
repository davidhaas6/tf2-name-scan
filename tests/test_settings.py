import json

import pytest

from tf2scan.config import load_config
from tf2scan.contracts import AdapterMetadata


def test_minimal_config_and_identity(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("{}")
    config = load_config(path)
    encoded, digest = config.effective_scan()
    assert "profiles" not in config.data
    assert json.loads(encoded)["settings"]["clustering"]["gap_s"] == 3
    config.data["query"] = {"target_name": "different"}
    config.data["matching"] = {"strong": 0.99}
    assert config.effective_scan()[1] == digest
    assert config.effective_scan({"sampling": {"fps": 2}})[1] != digest
    weights = tmp_path / "weights"
    weights.write_bytes(b"first")
    config.data["detector"] = {"weights": "weights"}
    first = config.effective_scan()[1]
    weights.write_bytes(b"second")
    assert config.effective_scan()[1] != first
    meta = AdapterMetadata("fake", "v1", "hash", "python", {"Pillow": "test"})
    assert config.effective_scan(adapters={"detector": meta})[1] != first


@pytest.mark.parametrize(
    "setting",
    [
        "sampling: {fps: .nan}",
        "detector: {batch_size: 1.5}",
        "crops: {padding: true}",
        "clustering: {similarity: 1.1}",
        "persistence: {commit_interval_s: 9}",
        "acquisition: {overlap_s: 600}",
        "acquisition: {streams_per_host: 2}",
        "matching: {weak: 0.99}",
        "recognizer: {device: false}",
        "profiles: {}",
        "scan: {fps: 2}",
    ],
)
def test_invalid_config(tmp_path, setting):
    path = tmp_path / "config.yaml"
    path.write_text(setting)
    with pytest.raises((ValueError, TypeError)):
        load_config(path)


def test_explicit_legacy(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("pipeline: legacy_hud")
    assert load_config(path).profile()[0] == "default_tf2_720p"
