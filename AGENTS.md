The high level design is in `docs/`. Add new documentation files as necessary.

Use `docs/todo.md` as the lightweight tracker for near-term evaluation and profiling work.

For scan speed/performance measurements, use `uv run python eval/benchmark_scan.py benchmark`
(default video/config, stream input). Use `--source local` for cached input or `compare` instead
of `benchmark` for a matched local/stream pair. The script retains profiles/history and uses
fresh processes/corpora; consult `docs/scan-benchmark.md` for timing boundaries and coverage
checks. Record findings in `docs/`; do not claim equivalent work when mismatch or incomplete-grid
warnings are present.

A 2022 early-stage prototype with test data is available in `~\Documents\Projects\killfeed-search`
