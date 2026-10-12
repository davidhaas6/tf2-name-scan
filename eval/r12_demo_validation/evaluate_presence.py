"""Presence/retrieval evaluation for the R12 demo-window validation.

For every ground-truth name in G (from extract_ground_truth.py), runs the
production query path (`tf2scan query --name NAME`) against the dedicated
corpus and classifies each queried name:

- ``found``        : >=1 promoted candidate whose best_text matches the name
                     after normalization (casefold, trim, collapse whitespace).
- ``not_found``    : no promoted candidates at all (FN).
- ``needs_review`` : >=1 candidate but none normalized-equal; the candidates'
                     texts/evidence are emitted for manual review before a
                     final TP/FP decision.

Metrics (per-name set comparison): |G|, |P|, TP, FN, FP, recall, precision, F1,
reported twice as bounds — needs_review counted as true (upper) or as false
(lower) — plus the final numbers once manual review decisions are supplied
as a JSON file.

See docs/demo-window-validation.md §4-5 for the definitions this implements.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
from pathlib import Path


def normalize(text: str) -> str:
    """Documented match rule: casefold, trim, collapse internal whitespace."""
    return re.sub(r"\s+", " ", text.strip()).casefold()


def run_query(name: str, config: Path) -> int:
    proc = subprocess.run(
        ["uv", "run", "tf2scan", "query", "--name", name, "--config", str(config)],
        capture_output=True,
        text=True,
        check=True,
    )
    print(f"  queried {name!r}: {proc.stdout.strip().splitlines()[-1]}")
    return 0


def fetch_hits(db: sqlite3.Connection, name: str) -> list[dict]:
    row = db.execute(
        "SELECT id FROM queries WHERE target_name = ? ORDER BY id DESC LIMIT 1",
        (name,),
    ).fetchone()
    if row is None:
        return []
    return [
        dict(r)
        for r in db.execute(
            "SELECT text_cluster_id, matched_alias, best_text, best_score,"
            " support_count, evidence_path, review_status"
            " FROM hits WHERE query_id = ?",
            (row["id"],),
        )
    ]


def classify(name: str, hits: list[dict]) -> dict:
    if not hits:
        return {"status": "not_found", "verified_hits": 0, "candidates": hits}
    verified = [h for h in hits if normalize(h["best_text"]) == normalize(name)]
    status = "found" if verified else "needs_review"
    return {"status": status, "verified_hits": len(verified), "candidates": hits}


def counts(names: dict, needs_review_as: str) -> dict:
    tp = fn = fp = 0
    for result in names.values():
        if result["status"] == "found":
            tp += 1
        elif result["status"] == "not_found":
            fn += 1
        else:  # needs_review
            if needs_review_as == "true":
                tp += 1
            else:
                fp += 1
    g = len(names)
    p = tp + fp
    precision = tp / p if p else None
    recall = tp / g if g else None
    f1 = (2 * precision * recall / (precision + recall)) if precision and recall else None
    return {
        "|G|": g,
        "|P|": p,
        "TP": tp,
        "FN": fn,
        "FP": fp,
        "recall": round(recall, 4) if recall is not None else None,
        "precision": round(precision, 4) if precision is not None else None,
        "F1": round(f1, 4) if f1 is not None else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ground-truth", type=Path,
                        default=Path("output/r12-demo/ground_truth.json"))
    parser.add_argument("--config", type=Path,
                        default=Path("eval/r12_demo_validation/config.yaml"))
    parser.add_argument("--db", type=Path,
                        default=Path("output/r12-demo/results.sqlite3"))
    parser.add_argument("--scan-profile", type=Path,
                        default=Path("output/r12-demo/scan-profile.json"))
    parser.add_argument("--output", type=Path,
                        default=Path("output/r12-demo/presence_results.json"))
    parser.add_argument("--manual-review", type=Path, default=None,
                        help="JSON dict name -> true/false for needs_review names"
                             " (from a previous run's output); enables final metrics")
    args = parser.parse_args()

    gt = json.loads(args.ground_truth.read_text(encoding="utf-8"))
    g_names = list(gt["names"])

    print(f"Querying {len(g_names)} ground-truth names against the corpus...")
    for name in g_names:
        run_query(name, args.config)

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    results = {name: classify(name, fetch_hits(db, name)) for name in g_names}
    cluster_count = db.execute("SELECT COUNT(*) FROM text_clusters").fetchone()[0]
    db.close()

    review = {}
    if args.manual_review:
        review = json.loads(args.manual_review.read_text(encoding="utf-8"))
    unreviewed = False
    for name, result in results.items():
        if result["status"] == "needs_review":
            if name in review:
                result["status"] = "found" if review[name] else "false_candidates"
                result["manual_decision"] = review[name]
            else:
                unreviewed = True

    summary = {
        "upper_bound_needs_review_true": counts(results, "true"),
        "lower_bound_needs_review_false": counts(results, "false"),
    }
    if args.manual_review and not unreviewed:
        summary["final"] = counts(results, "false")

    profile = json.loads(args.scan_profile.read_text(encoding="utf-8"))
    out = {
        "window": gt["window"],
        "ground_truth_counts": gt["counts"],
        "coverage": {
            "samples_scanned": profile.get("frames"),
            "video_duration_s": profile.get("duration_s"),
            "scan_wall_time_s": profile.get("wall_s"),
            "rtf": profile.get("realtime_factor"),
            "model_setup_s": profile.get("model_setup_s"),
            "crops": profile.get("crops"),
            "text_clusters": cluster_count,
            "largest_stage": profile.get("largest_stage"),
            "stages_s": profile.get("stages_s"),
            "source_resolution": profile.get("source_resolution"),
            "working_resolution": profile.get("working_resolution"),
            "detector_resolution": profile.get("detector_resolution"),
            "hardware": profile.get("hardware"),
        },
        "per_name": results,
        "missed_names": [n for n, r in results.items() if r["status"] == "not_found"],
        "needs_review": [
            {
                "name": n,
                "candidates": [
                    {"best_text": h["best_text"], "best_score": h["best_score"],
                     "support_count": h["support_count"], "evidence_path": h["evidence_path"]}
                    for h in r["candidates"]
                ],
            }
            for n, r in results.items() if r["status"] == "needs_review"
        ],
        "false_candidates": [
            {
                "name": n,
                "candidates": [
                    {"best_text": h["best_text"], "best_score": h["best_score"]}
                    for h in r["candidates"]
                ],
            }
            for n, r in results.items() if r["status"] == "false_candidates"
        ],
        "metrics": summary,
    }
    args.output.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(summary, indent=2))
    print("missed (FN):", out["missed_names"])
    print("needs manual review:", [n["name"] for n in out["needs_review"]])
    print("false candidates:", [n["name"] for n in out["false_candidates"]])
    if unreviewed:
        print("NOTE: unresolved needs_review names; bounds only. Re-run with"
              " --manual-review for final metrics.")
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
