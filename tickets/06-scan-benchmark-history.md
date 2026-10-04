# 6. Add a simple scan benchmark with history and video caching

Status: complete (2026-10-02). Implementation, the default stream baseline, and
real local/stream comparisons for the default and longer videos are documented
in [scan benchmark](../docs/scan-benchmark.md). Offline tests verify local/cache
and comparison behavior. Complete before [ticket 1](01-acquisition-decode-attribution.md).

## Goal

Add `eval/benchmark_scan.py` as a small wrapper around the production scan path,
equivalent to `tf2scan scan --reprocess --perf-output ...`, with repeatable
settings, retained run history, and an optional local-versus-stream comparison.
Default to video `GCN-CzALyq0`, `config.yaml`, and the config's scan settings.
This is not a full evaluation suite.

## Interface

```powershell
# One benchmark; choose local or stream input explicitly.
uv run python eval/benchmark_scan.py benchmark --source stream
uv run python eval/benchmark_scan.py benchmark --source local
# Two benchmarks of the same video/settings, plus a comparison summary.
uv run python eval/benchmark_scan.py compare
```

Support `--video`, `--config`, and an optional `--label`. Single benchmarks
default to stream input. One invocation performs one run, or one pair in compare
mode; repetitions, manifests, parameter sweeps, and dashboards are out of scope.

## Work

- Reuse the production scanner and `ScanProfile`; do not duplicate OCR logic or
  silently change sampling, model, device, or acquisition defaults.
- For local benchmarks and comparison mode, download the selected video-only
  format if absent and retain it under `output/benchmarks/cache/`. Reuse a valid
  cached file on later runs; record its format ID and basic media metadata.
  Publish cache files only after a successful download.
- Compare the cached format with the same format streamed remotely. Resolve a
  fresh media URL; fail clearly if that format is unavailable. Stream mode must
  actually use the remote source even when a cached local file exists.
- Give each measured run a fresh benchmark corpus and fresh model process, using
  the same effective settings. Keep production results untouched. This defines
  a simple, consistent fresh-corpus baseline; existing-corpus scaling remains
  separate work.
- Save each profile under a unique run directory in `output/benchmarks/runs/`
  and append a small record to `output/benchmarks/history.jsonl`. Include run ID,
  timestamp, label, video/input mode, effective settings, Git commit/dirty flag,
  selected format, profile path, completion status, and timing summary.
- Report scan wall time/RTF and existing stages, with model setup and command
  time separately. Record cache-download time separately and exclude it from
  scan RTF and the local-versus-stream scan comparison.
- Print a compact summary; compare mode prints both runs' wall time, RTF, and
  stage differences. Flag settings/media mismatches and incomplete sample grids
  instead of presenting them as equivalent work. Preserve failed-run history.

## Acceptance

Both single-run input modes and compare mode work with the default video.
Repeated invocations preserve earlier profiles, reuse the local cache, and start
from equivalent benchmark state. Local and stream runs cover the same sample
grid; production corpus and normal scan behavior are unchanged. Document the
commands and history format, and verify cache reuse, input routing, failure
recording, and timing boundaries without requiring network access in unit tests.
