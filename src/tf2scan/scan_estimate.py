"""Lightweight scan-time estimates from committed video progress."""

import math
import time


def _duration(value):
    return (
        float(value)
        if isinstance(value, (int, float)) and math.isfinite(value) and value > 0
        else None
    )


def format_time(seconds):
    seconds = math.ceil(max(0, seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


class ScanEstimate:
    """Estimate wall seconds per video second, retaining decayed knowledge across videos."""

    INITIAL_RATE = 0.6
    VIDEO_MEMORY = 0.75
    ADAPTATION_SECONDS = 30.0

    def __init__(self, queue, resume_positions=None):
        self.queue = [(video["id"], _duration(video["duration_s"])) for video in queue]
        self.resume_positions = resume_positions or {}
        self.rate = self.INITIAL_RATE
        self.started_videos = 0
        self.index = None
        self.position = 0.0
        self.observed_at = None

    def begin(self, video_id, duration, position, *, now=None):
        self.index = next(i for i, (identity, _) in enumerate(self.queue) if identity == video_id)
        self.queue[self.index] = (video_id, _duration(duration))
        if self.started_videos:
            self.rate = self.INITIAL_RATE + self.VIDEO_MEMORY * (self.rate - self.INITIAL_RATE)
        self.started_videos += 1
        self.position = max(0.0, position)
        self.observed_at = time.monotonic() if now is None else now

    def observe(self, position, *, now=None):
        """Learn only from newly committed video time; ignore resume and skipped gaps."""
        now = time.monotonic() if now is None else now
        advanced = position - self.position
        if advanced <= 0:
            return
        elapsed = now - self.observed_at
        if elapsed > 0:
            measured = elapsed / advanced
            weight = -math.expm1(-advanced / self.ADAPTATION_SECONDS)
            self.rate += weight * (measured - self.rate)
        self.position = position
        self.observed_at = now

    def remaining(self, progress):
        """Return current and known queue seconds, plus missing-duration count."""
        duration = self.queue[self.index][1]
        current = max(0.0, duration - progress)
        later = [
            max(0.0, value - self.resume_positions.get(video_id, 0)) if value else None
            for video_id, value in self.queue[self.index + 1 :]
        ]
        return (
            current * self.rate,
            (current + sum(value for value in later if value)) * self.rate,
            sum(value is None for value in later),
        )
