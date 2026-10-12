"""Negative-query battery + positive-candidate audit for the R12 window.

Two cheap extensions of the presence evaluation, both fully labeled by the
demo's own exhaustiveness (the `users` table records everyone who ever
connected during recording, so any name outside it is provably absent):

1. Negative battery: query ~30 names that are not on the roster, in three
   documented categories. Any promoted candidate for such a query is a false
   candidate by definition of the query semantics ("find player X", X absent).
2. Positive-candidate audit: for each of the 18 ground-truth names, classify
   every candidate of its latest query as verified (compact name is a
   substring of compact best_text — mirrors the matcher's own 1.0 rule) or
   unverified (garbled variants and junk alike; a human must look at them).

This measures the matcher's false-promotion behavior and reviewer workload
on this one corpus; counts are not per-hour rates. See
docs/demo-window-validation.md.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
from pathlib import Path


def compact(text: str) -> str:
    return re.sub(r"[\s_\-|.·:]+", "", " ".join(text.casefold().split()))


BATTERY: dict[str, list[str]] = {
    # Plausible invented names, designed to avoid substring collisions.
    "realistic_invented": [
        "definitely not a spy",
        "Medic Enjoyer",
        "gibus vision enjoyer",
        "trade offer declined",
        "sandvich connoisseur",
        "Gary the Gambler",
        "pootis bird",
        "heavy main btw",
        "unusual hat collector",
        "backburner enjoyer",
    ],
    # Short names (<= 4 compact chars): substring-collision stress.
    # First five deliberately collide with real text (Star.zip, sell me this
    # pen, Jo3o, Norwood, Rizz HuzzHouse); last five are clean.
    "short": ["zip", "pen", "jo3", "nor", "rizz", "kaz", "orb", "vex", "meep", "tofu"],
    # Near-duplicates of real G names: the "reviewer must squint" cases.
    "near_duplicates": [
        "chimneyy",
        "konner",
        "eto1",
        "Yorr",
        "JoJo2",
        "Chinney",
        "sunpoisonlng",
        "Don T Beakunt",
        "Rizz HuzzHouse",
        "500lbs Norwood 5 Janitor",
    ],
}


def run_query(name: str, config: Path) -> None:
    subprocess.run(
        ["uv", "run", "tf2scan", "query", "--name", name, "--config", str(config)],
        capture_output=True,
        text=True,
        check=True,
    )


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
            "SELECT text_cluster_id, best_text, best_score, support_count"
            " FROM hits WHERE query_id = ?",
            (row["id"],),
        )
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parse-cache", type=Path,
                        default=Path("output/r12-demo/parse_full.json"))
    parser.add_argument("--ground-truth", type=Path,
                        default=Path("output/r12-demo/ground_truth.json"))
    parser.add_argument("--config", type=Path,
                        default=Path("eval/r12_demo_validation/config.yaml"))
    parser.add_argument("--db", type=Path,
                        default=Path("output/r12-demo/results.sqlite3"))
    parser.add_argument("--output", type=Path,
                        default=Path("output/r12-demo/negative_results.json"))
    args = parser.parse_args()

    roster = {u["name"] for u in json.loads(
        args.parse_cache.read_text(encoding="utf-8"))["users"].values()}
    g_names = list(json.loads(
        args.ground_truth.read_text(encoding="utf-8"))["names"])

    battery_names = [n for names in BATTERY.values() for n in names]
    overlap = [n for n in battery_names if n in roster]
    if overlap:
        raise SystemExit(f"battery names on the roster (negativity broken): {overlap}")

    print(f"Running {len(battery_names)} negative queries...")
    for name in battery_names:
        run_query(name, args.config)

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row

    negatives = {}
    for category, names in BATTERY.items():
        negatives[category] = {}
        for name in names:
            hits = fetch_hits(db, name)
            negatives[category][name] = {
                "candidates": len(hits),
                "hits": [
                    {"best_text": h["best_text"], "best_score": h["best_score"],
                     "support_count": h["support_count"]}
                    for h in hits
                ],
            }

    positives = {}
    for name in g_names:
        hits = fetch_hits(db, name)
        needle = compact(name)
        verified = [h for h in hits if needle in compact(h["best_text"])]
        unverified = [h for h in hits if needle not in compact(h["best_text"])]
        positives[name] = {
            "candidates": len(hits),
            "verified": len(verified),
            "unverified": len(unverified),
            "unverified_texts": [h["best_text"] for h in unverified],
        }
    db.close()

    neg_with_hits = sum(
        1 for names in negatives.values() for r in names.values() if r["candidates"]
    )
    neg_candidates = sum(
        r["candidates"] for names in negatives.values() for r in names.values()
    )
    pos_candidates = sum(p["candidates"] for p in positives.values())
    pos_verified = sum(p["verified"] for p in positives.values())
    pos_unverified = sum(p["unverified"] for p in positives.values())

    out = {
        "battery": BATTERY,
        "negatives": negatives,
        "positive_audit": positives,
        "summary": {
            "negative_queries": len(battery_names),
            "negative_queries_with_candidates": neg_with_hits,
            "negative_false_candidates_total": neg_candidates,
            "positive_queries": len(g_names),
            "positive_candidates_total": pos_candidates,
            "positive_verified": pos_verified,
            "positive_unverified": pos_unverified,
        },
    }
    args.output.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(out["summary"], indent=2))
    print("\nnegatives with candidates, by category:")
    for category, names in negatives.items():
        hits = {n: r["candidates"] for n, r in names.items() if r["candidates"]}
        print(f"  {category}: {len(hits)}/{len(names)} queries -> {hits}")
    print("\npositive audit (candidates/verified/unverified):")
    for name, p in positives.items():
        print(f"  {p['candidates']:>3}/{p['verified']:>3}/{p['unverified']:>3}  {name!r}")
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
