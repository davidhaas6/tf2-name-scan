"""Online, target-independent tracking of similar rows across nearby frames."""

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
