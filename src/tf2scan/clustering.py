"""Online, target-independent tracking of similar rows across nearby frames."""

import math
from dataclasses import dataclass, field

from rapidfuzz.distance import Levenshtein

from .matching import normalize


@dataclass
class Cluster:
    id: int
    last_time: float
    row: int
    text: str
    confidence: float
    timestamps: set = field(default_factory=set)


class RowTracker:
    def __init__(self, gap=8, similarity=0.82, neighborhood=1):
        self.gap = gap
        self.similarity = similarity
        self.neighborhood = neighborhood
        self.active = []

    def find(self, timestamp, row, text):
        self.active = [c for c in self.active if timestamp - c.last_time <= self.gap]
        candidates = []
        for cluster in self.active:
            # Two separate notices in the same frame must never collapse together.
            if timestamp in cluster.timestamps or abs(cluster.row - row) > self.neighborhood:
                continue
            score = Levenshtein.normalized_similarity(normalize(text), normalize(cluster.text))
            if score >= self.similarity:
                candidates.append((score, -abs(cluster.row - row), cluster))
        return max(candidates, key=lambda c: c[:2])[2] if candidates else None


@dataclass
class TextTrack:
    id: int
    last_time: float
    center: tuple
    size: tuple
    text: str
    velocity: tuple = (0.0, 0.0)
    count: int = 1

    def motion(self):
        return {
            "last_timestamp_s": self.last_time,
            "center": self.center,
            "size": self.size,
            "velocity_per_s": self.velocity,
        }


class TextTracker:
    """Frame-wide best-edge association in normalized source coordinates.

    Scores all candidates before assigning any; ties use persistent cluster IDs
    and canonical geometry. A track accepts at most one observation per frame.
    """

    def __init__(self, settings):
        self.settings = settings
        self.active = {}
        self.last_time = -1

    @classmethod
    def restore(cls, settings, store, scan_id):
        """Rebuild only tracks still eligible at the committed watermark."""
        import json

        tracker = cls(settings)
        watermark = store.db.execute(
            "SELECT max(last_processed_timestamp_s) FROM scan_chunks WHERE video_scan_id=?",
            (scan_id,),
        ).fetchone()[0]
        if watermark is None:
            return tracker
        tracker.last_time = watermark
        rows = store.rows(
            """SELECT c.id,c.end_s,c.support_count,c.motion_summary_json,o.raw_text
            FROM text_clusters c JOIN cluster_observations link ON link.cluster_id=c.id
            JOIN observations o ON o.id=link.observation_id
            JOIN sampled_frames f ON f.id=o.sampled_frame_id
            WHERE c.video_scan_id=? AND c.end_s>=? AND c.close_reason IS NULL
            AND f.timestamp_s=c.end_s ORDER BY c.id,o.id""",
            (scan_id, watermark - settings["gap_s"]),
        )
        for row in rows:
            motion = json.loads(row["motion_summary_json"])
            tracker.active[row["id"]] = TextTrack(
                row["id"],
                row["end_s"],
                tuple(motion["center"]),
                tuple(motion["size"]),
                row["raw_text"],
                tuple(motion["velocity_per_s"]),
                row["support_count"],
            )
        return tracker

    @staticmethod
    def geometry(frame, crop):
        points = crop.detection.polygon
        xs = [p[0] / frame.source_width for p in points]
        ys = [p[1] / frame.source_height for p in points]
        return ((sum(xs) / 4, sum(ys) / 4), (max(xs) - min(xs), max(ys) - min(ys)))

    def associate(self, frame, accepted):
        timestamp = frame.timestamp_s
        if timestamp <= self.last_time:
            raise ValueError("Tracker frames must have strictly increasing timestamps")
        self.last_time = timestamp
        closed = {}
        for identity, track in list(self.active.items()):
            if timestamp - track.last_time > self.settings["gap_s"]:
                closed[identity] = "time_gap"
                del self.active[identity]
        edges, rejected = [], {}
        for index, (crop, result) in enumerate(accepted):
            center, size = self.geometry(frame, crop)
            for track in self.active.values():
                dt = timestamp - track.last_time
                distance = math.dist(center, track.center)
                if distance > self.settings["center_distance"]:
                    continue
                score = Levenshtein.normalized_similarity(
                    normalize(result.text), normalize(track.text)
                )
                if score < self.settings["similarity"]:
                    rejected.setdefault(track.id, "incompatible_text")
                    continue
                predicted = tuple(c + v * dt for c, v in zip(track.center, track.velocity))
                residual = math.dist(center, predicted)
                tolerance = max(
                    0.005, self.settings["motion_tolerance"] * max(size[1], track.size[1])
                )
                size_change = max(abs(a - b) / max(a, b) for a, b in zip(size, track.size))
                if (track.count > 1 and residual > tolerance) or size_change > self.settings[
                    "motion_tolerance"
                ]:
                    rejected[track.id] = "incompatible_motion"
                    continue
                quality = score / (1 + distance + residual)
                edges.append(
                    (
                        -quality,
                        track.id,
                        crop.detection.polygon,
                        crop.detection.identity,
                        index,
                        score,
                    )
                )
        matches, used = {}, set()
        for _, identity, _, _, index, score in sorted(edges):
            if index not in matches and identity not in used:
                matches[index] = (identity, score)
                used.add(identity)
        # Nearby unrelated text must not close a track that is still observed.
        for identity, reason in rejected.items():
            if identity not in used:
                closed[identity] = reason
                del self.active[identity]
        return matches, closed

    def observe(self, identity, frame, crop, result):
        center, size = self.geometry(frame, crop)
        previous = self.active.get(identity)
        track = TextTrack(identity, frame.timestamp_s, center, size, result.text)
        if previous:
            dt = frame.timestamp_s - previous.last_time
            track.velocity = tuple((c - p) / dt for c, p in zip(center, previous.center))
            track.count = previous.count + 1
        self.active[identity] = track
        return track
