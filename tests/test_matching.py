from tf2scan.clustering import Cluster, RowTracker
from tf2scan.matching import alias_score, compact, normalize, promoted


def test_unicode_and_separators_without_character_substitution():
    assert normalize("  ＰＬＡＹＥＲ   Straße ") == "player strasse"
    assert compact("Human_Worm-1") == "humanworm1"
    assert normalize("B0B") != normalize("Bob")
    assert alias_score("Killer + HUMAN_WORM victim", "HumanWorm") == 1
    assert alias_score("Killer HumanWorrn Victim", "HumanWorm") >= 0.82
    assert alias_score("Killer B0b victim", "Bob") == 0
    assert alias_score("Killer player123 victim", "player123") == 1
    assert alias_score("", "anything") == 0


def test_consensus_requires_distinct_nearby_frames():
    assert promoted([(1, 0.96)])
    assert promoted([(1, 0.85), (9, 0.84)])
    assert not promoted([(1, 0.85), (1, 0.85)])
    assert not promoted([(1, 0.85), (10, 0.85)])
    assert not promoted([(1, 0.81), (2, 0.81)])


def test_target_independent_tracker_moves_rows_but_not_same_frame():
    tracker = RowTracker()
    cluster = Cluster(1, 0, 0, "Alpha killed Beta", 0.9, {0})
    tracker.active.append(cluster)
    assert tracker.find(0, 1, cluster.text) is None
    assert tracker.find(1, 1, "Alpha killed Beto") is cluster
    assert tracker.find(1, 3, cluster.text) is None
    assert tracker.find(9, 0, cluster.text) is None
