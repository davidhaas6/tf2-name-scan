# Refinement implementation

## Current milestone: search, evidence and reports complete

R01–R11 core are implemented. R04/R05/R08 provide the local detection scanner;
R06/R07 add bounded local and remote chunks, incremental commits and resume.
`uv sync --extra paddle` installs the verified CPU runtime. `scan --local` loads
the specified PP-OCRv6-small models, filters proposals deterministically,
recognizes useful Unicode text, and persists geometry/motion clusters and actual
observation evidence. Completed chunks are skipped on resume, and a failed
reprocess preserves the previous completed scan. The R01–R03 notes below describe
their historical boundaries; their references to missing Paddle construction,
singleton clustering and gated detection CLI scans no longer describe current code.

Current validation: 85 tests pass and Ruff passes for `src` and `tests`. A real Paddle scan
completed two six-second local chunks of `gameplay-smoke.mp4` during R06/R07 validation. The earlier local
scanner validation recorded 69 tests and real Paddle inference on two 12-second
excerpts; see [local scanner validation](local-scanner-validation.md) for model
identities and inspected examples. R09–R11 add three-second query promotion,
sparse evidence retention/cleanup, and lineage-aware reports/exports. R12–R14
(labeled video evaluation, end-to-end metrics, and remaining CLI/documentation)
follow. R15 model comparisons are deferred. Live remote/model integration remains
environment-dependent; the remote streaming path has deterministic mocked coverage.

## 2026-09-28 performance follow-up

Routine evidence compaction now considers only clusters changed by a batch and paths whose references were removed. Crash-orphan recovery walks the corpus once per open store before its first detected scan. The recognizer defaults to oneDNN on CPU; detector oneDNN remains disabled after failing on the installed runtime. A four-frame pooling option preserves frame/result order but stays at one frame by default because its single trial did not improve recognition time and changed some OCR strings. A four-thread trial was also slower than the default setting.

On `input/uw1.mp4` at 1 sampled frame/second in the top-right region, the short local profile processed 11 frames and 107 crops in 16.031 scan-wall seconds for 11.486 seconds of video (RTF 1.396). That excludes 4.880 seconds of model setup and includes 4.200 seconds of startup orphan recovery. Without startup recovery, the measured scan work was 11.831 seconds (about 1.03× real time). Batch compaction took 0.104 seconds across three commits, down from 11.965 seconds in the original detailed profile.

Four later online scans of 148–477 catalog seconds each recorded RTF 0.682–0.836 using per-video database timestamps. Three had the expected 1-fps frame count; the 477-second entry had only 455 frames and requires source-duration verification. See [scan profiling findings](scan-profiling-findings.md#longer-online-scans-in-the-saved-corpus-2026-09-28) for the measurements and limits. These runs demonstrate faster-than-real-time recorded processing for the longer clips, while repeatability, complete source coverage for one clip, and player-name accuracy remain unverified.

## R09–R11 — query, evidence and report transition

The matcher uses three-second weak-score consensus and an identity containing its
effective settings, so old reviews remain with their original query version.
Queries score every supported observation in selected completed scans. Scanner
batches retain the actual representative crop and bounded crops relevant to the
configured query. Optional full-frame evidence is compacted to representative
frames. Later queries can still rematch all OCR text; reports identify the matched
observation and label representative fallback evidence with its own text and time.

`text_clusters.jsonl` schema version 2 includes support observations, decoded
polygons, scan/run IDs and run provenance. `hits.jsonl` includes matched and shown
evidence provenance. `report --text-clusters` is the main export; `--rows` remains
a compatibility alias. Reports can select historical scans/runs explicitly. Video
deletion cascades through generations, preserves shared runs, and removes owned
evidence only after the database delete commits.

Validation: 80 tests pass and Ruff passes. Real-model accuracy remains an R12/R13
evaluation task.

YouTube indexing and stream resolution now enable Node.js through yt-dlp's Python
API and install the matching EJS scripts via the `yt-dlp[default]` dependency.
Node 24.14.0 indexed both configured videos without the missing-runtime warning;
the video-only stream for `ZiZmodw-yRc` resolved at 1280×720 and 25 seconds.
Python 3.10 deprecation warnings remain separate from this runtime change.

## Historical milestone record: R01–R03

The following sections record what was delivered and validated at each earlier
milestone, including the later streaming/recovery entry. Forward-looking statements
in those entries are historical; use the current milestone above for present status.

## R01 — effective configuration and contracts

`config.example.yaml` describes the detection pipeline without a HUD or OpenOCR
checkout. `settings.py` owns validated defaults and canonical JSON/SHA-256 scan
identity. CLI sampling overrides are validated before model/media work. Local
weight files are hashed by content; adapters supply actual runtime versions,
dependencies, preprocessing and weights identities when instantiated. Unloaded
model versions and absent hashes remain explicitly unknown, never fabricated.
Aliases, matching settings and report presentation do not enter the OCR identity.

`contracts.py` defines source frames, detections, prepared crops and adapter
metadata; `recognize.Recognition` retains ordered text/nullable-confidence results.
The provisional confidence floor is 0.1, detector confidence 0.3, padding 2 pixels,
center-distance tolerance 0.05 and motion tolerance 0.5. These require evaluation.
All effective settings, including versions, belong in immutable run metadata.

Old configurations must explicitly set `pipeline: legacy_hud`. Only that path
uses `scan`, `profiles` and `ocr`; its eight-second legacy tracker is unchanged.
The new settings contract uses three seconds. Paddle runtime construction is
R04, retention enforcement R05, bounded acquisition R06, and batching R07;
settings for those packages are contracts, not claims they already execute.
Until R04/CLI cutover, normal detection CLI scans fail clearly before downloading;
query/report remain available without inference packages.

Validation: baseline 19 tests passed. R01 adds minimal/legacy configuration,
invalid values/interdependent limits, weight/override identity and query exclusion
coverage; full pytest and Ruff results are recorded in the requirement commit.

R01 validation result: 31 tests passed; Ruff passed. Real model setup is deferred to R04.


## R02 ? immutable lineage and migration

Migration v3 leaves migrations v1/v2 unchanged and retains their corpus tables as
`legacy_*` audit records. Before upgrading an existing database, SQLite's backup
API saves `results.pre-vN-migration.sqlite3` including committed WAL contents.
An existing backup is never overwritten: rename/archive it before retrying a
failed operational migration. Import and schema creation are transactional;
count and foreign-key checks run before v3 commits. Keep the backup for recovery.

The new tables are `scan_runs`, `video_scans`, `scan_chunks`, `chunk_attempts`,
`sampled_frames`, `observations`, `text_clusters`, and `cluster_observations`.
Hits and borderline matches refer to the new corpus. Run reproducibility metadata
and scan ownership are immutable. Compound foreign keys prohibit cross-scan
frame/support/representative links. Geometry uses validated clockwise convex
source-pixel quadrilaterals, packed as eight little-endian float32 values
(`quad-f32le-v1`). JSON exports decode geometry; the separate 1/16-pixel lattice
key is deterministic identity only, not an overlap/deduplication algorithm.

Legacy imports preserve IDs, both normalized forms, model/HUD metadata, reviews,
notes, config snapshots/signatures and evidence paths. Missing dimensions,
detector counts, model hashes and start times remain unknown. A synthetic chunk
is explicitly `legacy-container`. Only an unambiguous timestamp/row representative
gets an actual observation crop; ambiguous paths remain marked legacy references.
Legacy support scores of 1 mean direct original row membership, not measured
geometric similarity. Retained legacy tables are audit-only, never queried as an
additional corpus.

Reprocessing appends a new run/scan and preserves old evidence and hits.
`Store.selected_scan_ids` / `selected_clusters` choose the latest successfully
completed scan per video, with explicit `scan_id` or `run_id` selection available
in Python `run_query` and `export_report`. CLI selectors are deferred to R14.
Reviews remain in their original lineage and do not transfer to new clusters.
`start_scan(..., resume=True)` only resumes an unfinished matching config/model
snapshot; changed settings require a new run. Chunk replay integration is R07.
The legacy scanner still commits a whole video; its failed attempt is recorded
without displacing the prior completed scan. Query promotion remains the old
versioned eight-second rule until R09, and records these settings separately.

Validation includes populated v1/v2 migration, backup contents, injected import
failure, historical export/search/review preservation, failed/latest selection,
resume mismatch, geometry encoding, duplicate identities and cross-scan rejection.

R02 validation result: 37 tests passed; Ruff passed, including the real FFmpeg fixture.


## R03 ? full-frame detection and rectified crops

`TextDetector.detect(list[Frame])` returns one ordered detection list per input
frame, with unique detection identities within each frame and source-coordinate
quadrilaterals. `FakeDetector` supplies deterministic scripted proposals. The
`detector_input` / `to_source_detection` helpers implement native and 960 minimum
side experiments with exact per-axis tensor-to-source mappings (including rounded
resize dimensions). Both detector and recognizer result cardinality are checked.

`sample_frames` now yields source-aware `Frame` records with source timestamp,
sample identity, dimensions, working image and optional chunk owner. At or below
720p the image stays native; larger inputs use an aspect-preserving 720-high
working image by default. `max_height` is explicit and enters run configuration.
No unconditional upscale remains. Transitional tuple unpacking/indexing supports
legacy calibration and sampling callers. Decoder generators close on failure or
cancellation, and ordinary iterable fixtures work too. Exact section ownership
and PTS/retry reconciliation remain R06/R07.

`crops.py` canonicalizes unordered convex quads, rejects nonfinite, degenerate,
out-of-frame or undersized polygons, rectifies perspective at working-image
pixel density, and samples configurable padding from surrounding source content.
Outside-frame padding is white. Minimum text height is tested in source pixels,
not the detector tensor or reduced working image. Every crop preserves its
original detection identity and a homogeneous crop-to-source mapping. Region
labels (`top_left` through `bottom_right`) and normalized bounding dimensions are
geometry only. There are no ROI, contrast, edge, row-count or mandatory 2x gates.

Call `ingest(..., detector=adapter)` with detection configuration and adapters
that expose `AdapterMetadata` to exercise the local full-frame integration. It
stores every sampled frame, even with zero/invalid proposals, and writes ordered
recognitions, source geometry, transform metadata and support links into R02
lineage. Migration v4 adds detection identity and crop transform columns without
changing applied migrations. OpenOCR now exposes structured metadata when used in
its explicit compatibility/comparison role.

This is an integration foundation, not the complete V1 pipeline: detected
observations receive singleton clusters with `close_reason=untracked-r03` and
`clustering=singleton-r03` in run provenance. R05 implements proposal deduplication,
recognition caps, confidence/Unicode retention and full accounting. Current
counters accurately record this foundation's work (no dedup/cap drops); valid
nonempty OCR is retained. R08 supplies temporal/motion tracking. R04 supplies
Paddle factories, R06 bounded acquisition, R07 incremental persistence and R10
evidence compaction. CLI detection inference remains gated before acquisition
until the real factory exists. No real-model accuracy or performance claim is
made by the deterministic tests.

Tests cover skewed/rotated and reordered polygons, invalid/tiny boxes, source
versus working height, padding at frame edges, transform corner round trips,
960 resize mapping, spatial tags, batch ordering/cardinality, empty/rejected
frames, persisted decoded geometry, scan failure isolation, and actual FFmpeg
native 320x180 plus reduced 1920x1080 media.

R03 validation result: 52 tests passed; Ruff and git diff --check passed. Both real FFmpeg fixtures ran. Original v1/v2 migration definitions and high-level-design.md were verified unchanged. Paddle model integration/smoke testing remains R04.

## Streaming and recovery (R06/R07 integration)

Detection scans now resolve remote VOD media URLs with yt-dlp, require finite
duration, and decode bounded FFmpeg sections. Local files use the same chunk
schedule. The default core interval is 600 seconds with a three-second acquisition
overlap. Each global-grid sample belongs to one half-open core interval, and its
sample key is unique within the scan. Completed chunks are skipped on resume.

Frames are prepared and recognized before a SQLite write transaction. Frames,
observations, support links, representative evidence references and the chunk
watermark commit together at the configured video-time, row or frame-byte bound.
The scan becomes visible to default queries only after all chunks complete. A
failed attempt retains prior committed work and evidence. Retry diagnostics include
the requested range, elapsed time and error. V5 migration adds these diagnostics.

The tracker reconstructs recent active tracks from committed motion summaries and
last observations, allowing a notice to continue across a chunk or process
restart. Representative crops are written atomically before database references;
rejected transactions leave unreferenced files eligible for orphan cleanup.
Reprocessing appends a new scan, so a failed reprocess preserves the previous
completed corpus and its evidence.

The remote path relies on yt-dlp's selected direct media URL and FFmpeg's normal
HTTP handling. It has deterministic mocked source coverage; live network/model
integration remains environment-dependent. Evidence candidate compaction and the
query/report transition remain in R10/R11.

Validation: 75 tests pass, Ruff passes, and a real Paddle scan of the 12-second
`gameplay-smoke.mp4` completed two six-second local chunks. It stored 12 sampled
frames, 86 observations and 67 clusters; visible killfeed name `Seojer` has two
supporting observations. The separate `local-smoke.mp4` has no visible killfeed
and was used only for mechanical scan validation.
