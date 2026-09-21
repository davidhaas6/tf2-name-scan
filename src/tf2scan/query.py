import json

from .matching import MATCHER_VERSION, alias_score, compact, promoted


def run_query(store, target, aliases=()):
    aliases = list(dict.fromkeys([target, *aliases]))
    if not target.strip() or any(not compact(alias) for alias in aliases):
        raise ValueError("Target and aliases must contain non-separator characters")
    encoded = json.dumps(sorted(aliases), ensure_ascii=False)
    with store.transaction() as db:
        db.execute(
            "INSERT OR IGNORE INTO queries(target_name,aliases_json,matcher_version) VALUES (?,?,?)",
            (target, encoded, MATCHER_VERSION),
        )
        query_id = db.execute(
            "SELECT id FROM queries WHERE target_name=? AND aliases_json=? AND matcher_version=?",
            (target, encoded, MATCHER_VERSION),
        ).fetchone()[0]
        db.execute("DELETE FROM query_matches WHERE query_id=?", (query_id,))
        retained = set()
        for cluster in db.execute("SELECT * FROM row_clusters ORDER BY video_id,start_s"):
            observations = store.rows(
                "SELECT * FROM observations WHERE row_cluster_id=? ORDER BY timestamp_s",
                (cluster["id"],),
            )
            candidates = []
            for alias in aliases:
                scored = [(obs, alias_score(obs["raw_text"], alias)) for obs in observations]
                for obs, score in scored:
                    if score >= 0.65:
                        db.execute(
                            "INSERT INTO query_matches VALUES (?,?,?,?)",
                            (query_id, obs["id"], alias, score),
                        )
                if not promoted([(obs["timestamp_s"], score) for obs, score in scored]):
                    continue
                best, score = max(scored, key=lambda pair: pair[1])
                support = len({obs["timestamp_s"] for obs, value in scored if value >= 0.82})
                candidates.append((score, support, alias, best))
            if not candidates:
                continue
            score, support, alias, best = max(candidates, key=lambda item: item[:2])
            retained.add(cluster["id"])
            # Stable identities preserve reviews when an unchanged query is rerun.
            db.execute(
                """INSERT INTO hits
                (row_cluster_id,query_id,matched_alias,best_text,best_score,support_count,evidence_path)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(row_cluster_id,query_id) DO UPDATE SET
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
                    cluster["evidence_path"],
                ),
            )
        for hit in store.rows("SELECT id,row_cluster_id FROM hits WHERE query_id=?", (query_id,)):
            if hit["row_cluster_id"] not in retained:
                db.execute("DELETE FROM hits WHERE id=?", (hit["id"],))
    return query_id
