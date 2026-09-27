from tf2scan.performance import ScanProfile


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
