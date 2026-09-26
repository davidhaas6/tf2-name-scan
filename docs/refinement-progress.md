# Refinement implementation

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
