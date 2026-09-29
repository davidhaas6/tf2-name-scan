import pytest

from tf2scan.scan_estimate import ScanEstimate, format_time


def test_estimate_learns_within_video_and_decays_between_videos():
    estimate = ScanEstimate(
        [
            {"id": "one", "duration_s": 100},
            {"id": "two", "duration_s": 200},
        ],
        {"two": 50},
    )
    estimate.begin("one", 100, 0, now=0)
    assert estimate.remaining(0) == pytest.approx((60, 150, 0))

    estimate.observe(30, now=30)
    learned = estimate.rate
    assert 0.6 < learned < 1.0
    assert estimate.queue_rate == pytest.approx((0.6 * 300 + 30) / 330)
    current, queue, unknown = estimate.remaining(30)
    assert current == pytest.approx(70 * learned)
    assert queue == pytest.approx(70 * learned + 150 * estimate.queue_rate)
    assert unknown == 0

    estimate.begin("two", 200, 0, now=40)
    assert estimate.rate == pytest.approx(0.6 + 0.75 * (learned - 0.6))
    assert estimate.queue_rate == pytest.approx((0.6 * 300 + 30 * 0.75) / (300 + 30 * 0.75))


def test_short_fast_section_does_not_swing_queue_estimate():
    estimate = ScanEstimate([{"id": "one", "duration_s": 200}, {"id": "two", "duration_s": 3600}])
    estimate.begin("one", 200, 0, now=0)
    estimate.observe(20, now=20)
    before = estimate.remaining(20)[1]
    estimate.observe(40, now=21)
    after = estimate.remaining(40)[1]
    assert estimate.rate > 0.5
    assert estimate.queue_rate > 0.58
    assert abs(after - before) < 180


def test_resume_does_not_count_old_video_time_and_unknown_duration_is_visible():
    estimate = ScanEstimate(
        [
            {"id": "one", "duration_s": 100},
            {"id": "unknown", "duration_s": None},
        ]
    )
    estimate.begin("one", 100, 50, now=10)
    estimate.observe(50, now=20)
    assert estimate.rate == 0.6
    estimate.observe(60, now=30)
    assert estimate.rate > 0.6
    current, queue, unknown = estimate.remaining(60)
    assert current == pytest.approx(queue)
    assert unknown == 1
    assert format_time(3661) == "1:01:01"
