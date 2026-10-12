# Production scan benchmark

**Acquisition follow-up (2026-10-03):** [The streaming investigation](acquisition-attribution.md)
isolates bounded versus continuous HTTP requests as the dominant cause of the
local/remote gap. A fresh equivalent production pair takes 12.258/80.650 seconds;
repeated same-URL transfers take about 2.7/76.3 seconds depending only on the
request policy. Bounded requests became the production default in ticket 7
(2026-10-12), which requires FFmpeg 9+; see
[bounded streaming](bounded-streaming.md).

Run one fresh-corpus scan using the same detector, recognizer, ingestion path,
acquisition policy and `ScanProfile` as `tf2scan scan --reprocess --perf-output`:

```powershell
uv run python eval/benchmark_scan.py benchmark
uv run python eval/benchmark_scan.py benchmark --source stream
uv run python eval/benchmark_scan.py benchmark --source local
uv run python eval/benchmark_scan.py compare
uv run python eval/benchmark_scan.py benchmark --video GCN-CzALyq0 --config config.yaml --label baseline
```

The defaults are `GCN-CzALyq0`, `config.yaml`, stream input, and the config's
effective scan settings. `--video` takes a YouTube ID. The detection pipeline,
Paddle environment, FFmpeg/FFprobe and Node requirements are the same as the
normal scanner. Config-relative model paths retain their original meaning.
The benchmark does not index configured sources, apply source filters, search,
or export reports. No fps, model, device or acquisition settings are overridden.

Each measured run launches a new Python process and creates a new empty corpus
under `output/benchmarks/runs/<run-id>/corpus/`. Production `output/results.sqlite3`
and the config's normal corpus are untouched. Model files can be reused, but
model instances and their in-process state are fresh. This measures a fresh-corpus
baseline, not growing-corpus scaling or a cold model-download baseline.

Local mode downloads the production selection
`bestvideo[height<=720]/bestvideo` into a temporary cache directory, then probes
and hashes it before publication. Published files and their JSON metadata are
retained under `output/benchmarks/cache/`. Later invocations verify file size,
SHA-256 and FFprobe metadata before reuse. Partial or invalid downloads cannot
replace published metadata. Invalid cache entries are downloaded again. Old
published files remain available; automatic cache eviction is out of scope.

Comparison runs local first, then streams exactly the cached format ID using
a freshly resolved remote URL. Both processes use the same frozen configuration.
Single stream runs also pin a valid cached format when available, but always
decode the remote source. With no cache, stream mode uses the production format
selection and records the resolved format. If an exact pinned format is no
longer available, the run fails rather than selecting different media.
Stored media metadata includes format ID, extension, dimensions, fps, codecs,
and catalog duration; cache metadata adds probed duration, frame rate, file size
and SHA-256. Signed remote URLs and request headers are excluded from metadata.
Decoder errors in `command.log` may contain upstream diagnostic URLs.

## History and artifacts

Run IDs combine a UTC timestamp, input mode and random suffix. Nothing is
overwritten on repeated invocations. Each run directory retains:

- `request.json`: frozen config, original config path, input choice and pinned media.
- `command.log`: child-process logs and diagnostics.
- `profile.json`: the existing scan profile, including partial timings if ingestion fails.
- `result.json`: child status, effective settings with loaded adapter versions and
  weight hashes, media selection, timing summary and sample-grid check.
- `run.json`: the complete history record, including preparation or launch failures.
- `corpus/`: the isolated SQLite database and representative evidence.

`output/benchmarks/history.jsonl` appends one JSON object per attempted run,
including failed attempts. Its `schema_version: 1` records contain `run_id`,
UTC `timestamp`, optional `label`, `video_id`, `source`, `effective_scan`, Git
`commit` and `dirty`, `media`, `profile_path`, `status`, `error`, `warnings`,
`sample_grid`, `timing`, `command_s`, `worker_command_s`, `cache_download_s`,
`cache_reused`, child `exit_code`, and cache metadata where applicable. Fields that cannot be
measured before a failure are absent or null. Model setup failures have no
scan profile. Git dirtiness reflects the working tree, including untracked
files; unknown Git state is null. Paths are absolute local paths.

## Timing and comparison boundaries

`timing.wall_s` starts immediately before production ingestion and ends as soon
as ingestion returns or raises. It includes remote resolution and acquisition
retries, startup evidence cleanup and the existing scan stages. It excludes model
construction, corpus opening, cache preparation, sample-grid validation,
profile serialization and model teardown. `timing.realtime_factor` is scan wall
time divided by the selected VOD's catalog duration, matching the normal profile.
Both local and stream scans use that duration for their bounded sample grid.

`model_setup_s` records model construction separately. `worker_command_s` is the
child-process wall time including interpreter startup, model setup and teardown,
scanning and validation. `command_s` is the entire per-run wrapper time through
child completion, including cache preparation, excluding history writes and
summary printing. `cache_download_s` covers the download's fresh resolution,
transfer, probing, hashing and publication; the initial format selection is
included in command time. A reused cache has zero download time. Download time
never enters scan RTF or the scan comparison.

The summary prints scan wall time, RTF, model setup, command time and all existing
stages. Comparison also prints stream-minus-local scan wall/RTF and stage deltas,
and retains `comparison.json` in the local run directory. It checks effective
settings and loaded model metadata, selected media metadata, and a timestamp
grid fingerprint. Every expected sample at `i / fps` before the catalog duration
must be present; even the production decoder's tolerated one-frame EOF shortfall
is flagged. Failed runs, differing formats/settings, or incomplete grids are
explicitly marked as non-equivalent work. Numerical deltas in that case are only
diagnostic, not a speed comparison. Compare returns nonzero when equivalence
cannot be established. One pair is a measurement, not a statistical speed claim.

Offline tests in `tests/test_scan_benchmark.py` cover cache publication/reuse,
corruption and failed downloads, exact format selection, real production ingestion
with fake models and decoded frames, local/remote routing, fresh corpus isolation,
failed-run history, config freezing, sample checks and timing boundaries.

## First default baseline (2026-10-02)

Ran the default stream benchmark exactly once with the installed environment:

```powershell
uv --cache-dir .uv-cache run --no-sync python eval/benchmark_scan.py benchmark
```

The cache-directory override avoids the sandbox's inaccessible user-level uv
cache; `--no-sync` uses the already installed Paddle/dev environment. Benchmark
arguments and scan settings are the defaults. Run ID:
`20261002T224752.166232Z-stream-e40ff366` (18:47:52 America/New_York).
The Git commit was `20cf108f7a9565b81b29d1bca29ede3dc9c8bb0b`, with a dirty working
tree including this implementation and the pre-existing evidence-index work.

The input was video `GCN-CzALyq0`, format `298`, video-only H.264 MP4,
1280×720 at 60 fps, catalog duration 148 seconds. Configured scanning used 1 fps,
the top-right detector region, 720-pixel working height, GPU detector/recognizer,
one-frame recognition pooling and representative evidence compaction.

| Measurement | Seconds |
|---|---:|
| Scan wall | 80.522 |
| Model setup (outside scan wall) | 6.495 |
| Worker command | 88.272 |
| Whole benchmark command | 88.361 |
| Cache download | 0.000 |
| Decoding / frame waiting | 63.984 |
| Detection | 4.246 |
| Crop preparation | 0.989 |
| Recognition | 8.542 |
| Persistence/evidence | 1.192 |
| Startup cleanup | 0.001 |
| Other scan work | 1.567 |

Scan RTF was **0.544**. The isolated corpus has exactly one scan and one
`remote-section` chunk, all 148 unique timestamps from 0 through 147 seconds,
627 observations and 394 text clusters. SQLite integrity is `ok`, foreign-key
checks return no violations, and all representative evidence files exist.
The production `output/results.sqlite3` SHA-256 was identical before and after:
`394e8136db8c6e8a6d8494ac7aec0f95c261ac538e52c63ce164f51cd959a416`.

Artifacts are retained under `output/benchmarks/runs/<run-id>/`, with one record
in `output/benchmarks/history.jsonl`. All 125 tests and Ruff checks passed.
Local/cache and comparison behavior were verified offline; a real local/stream
pair was later attributed by [ticket 1](../tickets/01-acquisition-decode-attribution.md)
(see the acquisition follow-up note above). This single fresh-corpus result
does not establish speed changes relative to older growing-corpus scans.

## Default local/stream comparison (2026-10-02)

Ran `uv --cache-dir .uv-cache run --no-sync python eval/benchmark_scan.py compare`
once, starting at 22:30:37 America/New_York. The local run is
`20261003T023037.502273Z-local-760079b9`; the stream run is
`20261003T023113.131936Z-stream-e47d6bed` (run IDs use UTC).
The first local invocation downloaded and retained a validated 44.14 MiB cache
file. Both runs used default video `GCN-CzALyq0`, format `298`, the same config,
loaded model metadata and fresh corpora/processes. The comparison reports
`equivalent: true`, with no warnings.

| Measurement | Local | Stream |
|---|---:|---:|
| Scan wall, s | 12.022 | 80.451 |
| Scan RTF | 0.081 | 0.544 |
| Model setup, s (outside scan) | 14.546 | 5.417 |
| Whole per-run command, s | 35.628 | 86.954 |
| Cache download/validation/publication, s (outside scan) | 5.582 | 0.000 |
| Decoding / frame waiting, s | 1.233 | 64.829 |
| Detection, s | 3.856 | 4.398 |
| Crop preparation, s | 0.940 | 0.871 |
| Recognition, s | 4.834 | 7.803 |
| Persistence/evidence, s | 1.111 | 1.009 |
| Startup cleanup, s | 0.000 | 0.000 |
| Other scan work, s | 0.046 | 1.542 |

Both sample grids contain every timestamp from 0 through 147 seconds and have
the same fingerprint. Each corpus contains 734 recognition crops, 627 observations
and 394 text clusters. Both databases pass integrity and foreign-key checks;
all representative evidence files exist.

The streamed scan took 68.429 seconds longer, with 63.596 seconds of that gap in
the current decoding/frame-wait stage. This stage combines acquisition and
decoding; [ticket 1](../tickets/01-acquisition-decode-attribution.md) later
separated those costs and attributed the gap to bounded HTTP requests (see the
acquisition follow-up note above). This is one ordered pair, not a repeated
throughput estimate. The profiles,
corpora and logs are retained in each run directory; `comparison.json` is in
the local run directory, and both runs are appended to `history.jsonl`.

## Longer comparison: tFwLwEcSTwc (2026-10-02)

The latest user-run comparison covers 736 seconds (12:16), using video-only AV1
format `398`, 1280×720 at 30 fps. Local run:
`20261003T023610.248133Z-local-1f8b7e1e`; stream run:
`20261003T023819.971831Z-stream-bf37396c`. Both completed with matching
settings/models/media and complete, identical 736-sample grids.

| Measurement | Local | Stream |
|---|---:|---:|
| Scan wall, s | 80.523 | 375.457 |
| Scan RTF | 0.109 | 0.510 |
| Whole per-run command, s | 129.720 | 382.729 |
| Cache download/validation/publication, s | 40.359 | 0.000 |
| Model setup, s | 5.558 | 5.867 |
| Decoding / frame waiting, s | 6.021 | 312.181 |
| Detection, s | 17.672 | 15.266 |
| Recognition, s | 38.974 | 31.540 |
| Crop preparation, s | 7.842 | 6.170 |
| Persistence/evidence, s | 9.716 | 7.904 |

The first download retained 142,187,883 bytes (about 135.6 MiB). Including the
download and all wrapper/model overhead, the local command was about 2.95 times
faster, saving 253.009 seconds. Scan-only time was about 4.66 times faster.
The 306.160-second frame-wait gap exceeds the 294.934-second scan-wall gap
because the other stages, including recognition, were faster in the stream run.
Frame waiting accounts for about 83% of streaming scan time. Locally, detection
plus recognition instead accounts for about 70% of scan time.

Both runs have 5,113 recognition crops, 4,583 observations and 2,262 clusters.
They crossed the 600-second chunk boundary, with exactly two successful attempts
per run and no retries/errors. A sorted observation digest over timestamp,
geometry key, raw text and OCR confidence matches exactly:
`5b402f034b2f05694c5fa753d487be1efa4f302f7d41fc011be2e9fe45feef8b`.

This longer AV1 example reinforces the earlier H.264 result: download-first
wins even including initial cache preparation. These pairs did not isolate HTTP
request behavior, but [ticket 1](../tickets/01-acquisition-decode-attribution.md)
subsequently proved bounded versus continuous requests as the cause (see the
acquisition follow-up note above). These are two different videos/formats, not
repeated trials of one workload.
