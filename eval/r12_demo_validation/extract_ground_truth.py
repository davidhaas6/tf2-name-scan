"""Extract the ground-truth name set G for the R12 demo-window validation.

Runs the vendored tf-demo-parser's `parse_demo` (already installed on PATH or
at ~/.cargo/bin) on a TF2 demo and emits, for one window:

- ``ground_truth.json``: the window definition, the per-kill table (tick,
  seconds, killer/victim names, weapon, assister for context only), the
  distinct-name set G with per-name roles and flags, and counts.

G is the set of distinct player names appearing as killer (attacker UserId
!= 0) or victim of any PlayerDeath whose tick falls in the window. Victims of
world/suicide kills are always included and flagged ``world_kill_only`` when
that is their only route into G. Assisters are never part of G because the
TF2 killfeed does not display assisters.

See docs/demo-window-validation.md for the plan this implements.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_PARSE_DEMO = Path.home() / ".cargo" / "bin" / "parse_demo.exe"


def find_parse_demo(explicit: str | None) -> str:
    if explicit:
        path = Path(explicit)
        if not path.exists():
            sys.exit(f"parse_demo not found at {path}")
        return str(path)
    on_path = shutil.which("parse_demo")
    if on_path:
        return on_path
    if DEFAULT_PARSE_DEMO.exists():
        return str(DEFAULT_PARSE_DEMO)
    sys.exit("parse_demo binary not found (not on PATH, not at ~/.cargo/bin)")


def parse_demo(demo: Path, binary: str, cache: Path) -> dict:
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    proc = subprocess.run(
        [binary, str(demo)], capture_output=True, text=True, check=True
    )
    data = json.loads(proc.stdout)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(proc.stdout, encoding="utf-8")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", type=Path, default=Path("input/test_9v9_demo.dem"))
    parser.add_argument("--window-start", type=float, required=True,
                        help="window start in demo playback seconds (tick * interval_per_tick)")
    parser.add_argument("--window-length", type=float, default=180.0,
                        help="window length in seconds (default 180, customizable)")
    parser.add_argument("--parse-demo", default=None,
                        help="path to the parse_demo binary (default: PATH, then ~/.cargo/bin)")
    parser.add_argument("--parse-cache", type=Path,
                        default=Path("output/r12-demo/parse_full.json"),
                        help="cached full parse_demo JSON (reused if present)")
    parser.add_argument("--output", type=Path,
                        default=Path("output/r12-demo/ground_truth.json"))
    args = parser.parse_args()

    data = parse_demo(args.demo, find_parse_demo(args.parse_demo), args.parse_cache)
    header = data["header"]
    ipt = data["intervalPerTick"]
    if not ipt:
        sys.exit("parser output has no intervalPerTick; refusing to guess tick->seconds")
    users = {int(uid): info for uid, info in data["users"].items()}

    t0, t1 = args.window_start, args.window_start + args.window_length
    tick0, tick1 = t0 / ipt, t1 / ipt

    kills = []
    roles: dict[str, dict[str, bool]] = {}
    for death in data["deaths"]:
        s = death["tick"] * ipt
        if not (t0 <= s < t1):
            continue
        killer_id, victim_id = death["killer"], death["victim"]
        if killer_id != 0 and killer_id not in users:
            sys.exit(f"kill at tick {death['tick']}: killer id {killer_id} missing from users")
        if victim_id not in users:
            sys.exit(f"kill at tick {death['tick']}: victim id {victim_id} missing from users")
        killer_name = users[killer_id]["name"] if killer_id != 0 else None
        victim_name = users[victim_id]["name"]
        kills.append({
            "tick": death["tick"],
            "s": round(s, 3),
            "killer": killer_name,
            "victim": victim_name,
            # assister is context only; never part of G (killfeed hides assisters)
            "assister": users[death["assister"]]["name"] if death["assister"] in users else None,
            "weapon": death["weapon"],
            "world_kill": killer_id == 0,
        })
        for name, is_killer in ((killer_name, True), (victim_name, False)):
            if name is None:
                continue
            entry = roles.setdefault(name, {"killer": False, "victim": False})
            if is_killer:
                entry["killer"] = True
            else:
                entry["victim"] = True

    names = {}
    for name, entry in sorted(roles.items()):
        entry = dict(entry)
        entry["world_kill_only"] = (
            not entry["killer"] and entry["victim"]
            and all(k["victim"] == name and k["world_kill"] for k in kills
                    if k["victim"] == name or k["killer"] == name)
        )
        names[name] = entry

    ground_truth = {
        "demo": {
            "path": str(args.demo),
            "map": header["map"],
            "duration_s": header["duration"],
            "ticks": header["ticks"],
            "interval_per_tick_s": ipt,
        },
        "window": {
            "start_s": t0,
            "end_s": t1,
            "length_s": args.window_length,
            "start_tick": tick0,
            "end_tick": tick1,
        },
        "kills": kills,
        "names": names,
        "counts": {
            "kills_in_window": len(kills),
            "world_kills_in_window": sum(k["world_kill"] for k in kills),
            "distinct_names": len(names),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(ground_truth, indent=2), encoding="utf-8")

    print(f"window [{t0} s, {t1} s) = ticks [{tick0:.1f}, {tick1:.1f})")
    print(f"kills in window: {ground_truth['counts']['kills_in_window']} "
          f"(world kills: {ground_truth['counts']['world_kills_in_window']})")
    print(f"|G| = {len(names)} distinct names")
    for name, entry in names.items():
        tags = [r for r in ("killer", "victim") if entry[r]]
        if entry["world_kill_only"]:
            tags.append("world_kill_only")
        print(f"  {name!r}: {', '.join(tags)}")
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
