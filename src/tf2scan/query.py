import hashlib
import json

from .matching import MATCHER_VERSION, alias_score, compact, promoted
from .settings import query_settings


def run_query(store, target, aliases=(), *, scan_id=None, run_id=None, matching=None):
    aliases = list(dict.fromkeys([target, *aliases]))
    if not target.strip() or any(not compact(alias) for alias in aliases):
        raise ValueError("Target and aliases must contain non-separator characters")
    encoded = json.dumps(sorted(aliases), ensure_ascii=False)
    settings = query_settings({"matching": matching or {}})
    settings_json = json.dumps(settings, sort_keys=True, separators=(",", ":"))
    version = MATCHER_VERSION + ":" + hashlib.sha256(settings_json.encode()).hexdigest()[:16]
    with store.transaction() as db:
        db.execute(
            "INSERT OR IGNORE INTO queries(target_name,aliases_json,matcher_version,settings_json) VALUES (?,?,?,?)",
            (
                target,
                encoded,
                version,
                settings_json,
            ),
        )
        query_id = db.execute(
            "SELECT id FROM queries WHERE target_name=? AND aliases_json=? AND matcher_version=?",
            (target, encoded, version),
        ).fetchone()[0]
        selected = store.selected_clusters(scan_id=scan_id, run_id=run_id)
        selected_ids = {cluster["id"] for cluster in selected}
        retained = set()
        for cluster in selected:
            observations = store.rows(
                "SELECT o.*,f.timestamp_s FROM observations o JOIN sampled_frames f ON f.id=o.sampled_frame_id "
                "JOIN cluster_observations co ON co.observation_id=o.id WHERE co.cluster_id=? ORDER BY f.timestamp_s",
                (cluster["id"],),
            )
            candidates = []
            for alias in aliases:
                scored = [(obs, alias_score(obs["raw_text"], alias,
                           settings["short_name_length"])) for obs in observations]
                for obs, score in scored:
                    if score >= 0.65:
                        db.execute(
                            "INSERT OR REPLACE INTO query_matches VALUES (?,?,?,?)",
                            (query_id, obs["id"], alias, score),
                        )
                if not promoted([(obs["timestamp_s"], score) for obs, score in scored],
                                settings["strong"], settings["weak"], settings["gap_s"]):
                    continue
                best, score = max(scored, key=lambda pair: pair[1])
                support = len({obs["timestamp_s"] for obs, value in scored
                               if value >= settings["weak"]})
                candidates.append((score, support, alias, best))
            if not candidates:
                continue
            score, support, alias, best = max(candidates, key=lambda item: item[:2])
            retained.add(cluster["id"])
            # Stable identities preserve reviews when an unchanged query is rerun.
            db.execute(
                """INSERT INTO hits
                (text_cluster_id,query_id,matched_alias,best_text,best_score,support_count,evidence_path)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(text_cluster_id,query_id) DO UPDATE SET
                matched_alias=excluded.matched_alias,best_text=excluded.best_text,
                best_score=excluded.best_score,support_count=excluded.support_count,
                evidence_path=excluded.evidence_path""",
                (
                    cluster["id"],
                    query_id,
                    alias,
                    best["raw_text"],
                    score,
                    support,
                    best["crop_path"] or cluster["evidence_path"],
                ),
            )
        for hit in store.rows("SELECT id,text_cluster_id FROM hits WHERE query_id=?", (query_id,)):
            if hit["text_cluster_id"] in selected_ids and hit["text_cluster_id"] not in retained:
                db.execute("DELETE FROM hits WHERE id=?", (hit["id"],))
    return query_id
