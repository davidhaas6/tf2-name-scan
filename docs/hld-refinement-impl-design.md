# HLD refinement: implementation design and scope

Status: R01–R11 core implemented through lineage-aware search, sparse evidence and reports; R12–R13 and remaining R14 work are next. Performance follow-up updated 2026-09-28.
Audit date: 2026-09-22.  
Implementation baseline: `90f842e`; repository HEAD at audit: `605a562`.  
Design changes: `666bc0a` (adaptive detection and lineage) and `605a562` (streaming MVP refinements).  
Authority: [high-level-design.md](high-level-design.md), as of `605a562`.

The audit, proposed work packages and milestone counts below preserve their original dates. For current performance and validation status, see [refinement progress](refinement-progress.md#2026-09-28-performance-follow-up) and [scan profiling findings](scan-profiling-findings.md#longer-online-scans-in-the-saved-corpus-2026-09-28). The latest local check has 85 passing tests and Ruff passing. The 11-second top-right clip measured RTF 1.396 with model setup excluded; four later online scans recorded RTF 0.682–0.836, with one source-duration discrepancy still open.

## 1. Purpose and original audit conclusions

This document scopes the work needed to bring the implementation into agreement with the current HLD. The audit findings in this section and the ?Current implementation? column in ?2 describe the original `605a562` baseline, not the current code. See ?11 for completed work and the remaining delivery sequence, and [refinement-progress.md](refinement-progress.md) for implementation details and validation results.

The original audit follows. It is based on inspection of `src/tf2scan/`, all five test modules, configuration, packaging, and the usage/evaluation documentation, alongside the three design revisions. It is a source audit, not a certification of the original implementation or a real-model benchmark. Tests and model inference were not run during that original audit; subsequent implementation validation is recorded in ?11.

At the audit baseline, the implementation was a whole-video, fixed-HUD-row pipeline. `cli.execute()` instantiates `OpenOCRRecognizer`, downloads a complete video, and calls `ingest()`. Ingestion slices profile-defined rows, recognizes them, and commits one transaction for the entire video. Reprocessing deletes and replaces the previous corpus. The benchmark accepts already-cropped images and cannot measure detector or production clustering failures.

The two HLD updates require a coordinated change across acquisition, geometry, inference, persistence, clustering, query/report joins, evaluation, and CLI configuration. Changing only the model adapter and default time gap would leave most of the design unimplemented.

Several foundations should be retained:

- Target-independent ingestion and query-only rematching already exist.
- Unicode normalization, substring/sliding-window matching, short-alias protection, and borderline `query_matches` already exist.
- `Store` already enables WAL and foreign keys and applies versioned migrations. WAL itself is not a missing feature.
- FFmpeg already pipes decoded frames, detects truncated frames, and checks completion; it currently reads a local file and forces 720-pixel height.
- Representative crop/full-frame evidence, atomic report-file replacement, HTML escaping, stable reviews on unchanged queries, and owned-file cleanup already exist.
- Indexing, filters, local inputs, queue failure isolation, source-split validation, and recognition adapter test doubles are reusable.

Items described below as **HLD requirements** come from the current design. **Implementation decisions** resolve details the HLD does not specify. They are proposed defaults for implementing agents, not claims that those details were in either commit. File names for new modules are suggestions; public behavior and invariants are the acceptance criteria.

## 2. Change inventory and work packages

Size expresses relative complexity, not elapsed-time estimates: S = localized change; M = several related components; L = subsystem change; XL = cross-cutting migration or state-machine change. Dependencies describe the integrated implementation; adapters and fixtures can be developed before storage integration.

| ID | Change and HLD origin | Current implementation | Scope / principal files | Size | Depends on |
|---|---|---|---|---|---|
| R01 | Effective configuration and model contracts; §§3.3–3.5, 5 (`666`, `605`) | HUD profiles required; OpenOCR settings and 8-second defaults | `config.py`, example YAML, adapter metadata contracts | M | — |
| R02 | Immutable run/video/chunk/frame lineage; §4 (`666`, `605`) | Mutable video state, two migrations, `row_clusters`; replacement deletes old data | `storage.py`, new migrations, selection helpers | XL | R01 |
| R03 | Full-frame detection and rectified crops; §§3.2–3.4 (`666`) | `hud.boxes/useful/prepare`; no detector | New `detection.py`, `crops.py`; `frames.py`, `ingestion.py` | L | R01 |
| R04 | Paddle detector/recognizer V1; §§3.3, 3.5 (`605`) | OpenOCR/PyTorch only | `recognize.py`, detector backend, model factory, `pyproject.toml` | M | R01, R03 contracts |
| R05 | Deterministic text retention and counters; §3.4 (`666`) | Contrast gate plus nonempty normalized OCR | `crops.py`, `ingestion.py`, frame records | M | R02–R04 |
| R06 | Bounded streaming acquisition; §§3.1–3.2 (`605`) | Full download then local FFmpeg | `download.py`, `frames.py`, `indexing.py`, source abstraction | L | R01, R02 |
| R07 | Batched persistence, recovery, boundary deduplication; §§3.1, 4.1 (`605`) | One transaction and retry per complete video | `ingestion.py`, `storage.py`, chunk orchestration | XL | R02, R05, R06, R08 |
| R08 | Geometry/motion text clustering; §3.7 (`666`, `605`) | `RowTracker`: text, row-index distance, 8 seconds | `clustering.py`, cluster persistence and restoration | L | R02, R03 |
| R09 | 3-second promotion and lineage-aware queries; §3.6 (`605`), §4 (`666`) | `promoted(gap=8)` and row-cluster SQL | `matching.py`, `query.py` | M | R02, R08 |
| R10 | Accurate sparse evidence and cleanup; §§3.4, 4 (`666`, `605`) | All observations point at a mutable cluster representative | `ingestion.py`, `storage.py`, evidence lifecycle | L | R02, R07, R08 |
| R11 | Text-cluster reports and exports; §§4–5 (`666`) | `rows.jsonl`, row/HUD lineage and evidence joins | `report.py`, CLI export options | M | R09, R10 |
| R12 | Manual real-video dataset and manifest; §§6.1–6.3 (`605`) | Empty manifest; docs prioritize demo crops | `eval/`, `docs/evaluation.md`, manifest validation | M + annotation | R03 contracts |
| R13 | Detector/end-to-end evaluation, uncertainty, telemetry; §6.4 (`666`, `605`) | Crop-only point estimates and Python-heap timing | `benchmark.py`, `eval/run_benchmark.py`, instrumentation | L | R05, R07–R09, R12 |
| R14 | CLI, user documentation, regression transition; §§5, 10 (`666`, `605`) | Calibration/profile commands and replacement semantics | `cli.py`, README, usage/evaluation docs, tests | M | All V1 packages |
| R15 | Benchmark-gated model/runtime comparisons; §§3.5, 6.4, 10 (`605`) | OpenOCR adapter available, no detector comparisons or ONNX backend | Optional adapters and benchmark configurations | M–L | R13 |

R01–R14 are the V1 implementation/data scope. R15 must be tracked as the HLD's subsequent comparison/deployment phase; installing every alternative backend is not a prerequisite for the Paddle vertical slice.

## 3. Shared contracts and configuration (R01)

Replace the default dependence on `Config.profile()` and `DEFAULT_PROFILE`. New scans do not require channel-to-HUD mappings, row count, ROI calibration, contrast/edge thresholds, or universal 2x row upscaling. Preserve old configuration only through an explicit compatibility path; do not silently interpret a user's old HUD configuration as the new detection pipeline.

Introduce validated settings for:

| Area | Required settings / initial policy |
|---|---|
| Sampling | `fps=1`, configurable; bounded local/remote chunks, `chunk_seconds=600` |
| Detection | PP-OCRv6-small-det identity, weights, device, batch bound, detector confidence, native processing versus `limit_type=min, limit_side_len=960` experiment |
| Crops/retention | Minimum source-text height 6 px; polygon IoU duplicate threshold 0.85; containment threshold 0.90; recognition cap 64 per frame; configurable padding and low OCR confidence floor |
| Recognition | PP-OCRv6-small-rec identity, weights, device and batch size; optional explicit OpenOCR comparison backend |
| Clustering | Missing-observation gap 3 seconds; text similarity, normalized-distance and motion tolerances |
| Matching | Strong 0.95; weak 0.82 supported at distinct timestamps within 3 seconds; preserve short-name rule |
| Acquisition | Bounded attempts/backoff; one active stream per host; overlap duration; debug/failed-chunk cache disabled by default or explicitly short-lived |
| Persistence | Video-time commit interval within 10–30 seconds, row/byte memory bounds, WAL checkpoint and size settings |
| Evidence | Representative crop policy, optional full frames, bounded candidate retention and explicit compaction |

The HLD does not choose every confidence/motion/cache threshold. Pick documented provisional values, expose them to evaluation, and record effective values in run metadata. Validate finite values, integer counts, threshold ranges and interdependent limits before loading models or opening streams.

Use explicit records across subsystem boundaries:

- Frame: source timestamp/sample identity, source width/height, image, owning chunk.
- Detection: source-coordinate quadrilateral and detector confidence.
- Prepared crop: original detection identity, rectified/padded image, normalization transform metadata as needed.
- Recognition: text and nullable confidence, preserving input/result order and cardinality.
- Adapter metadata: model name/version, weights hash, runtime/dependency versions and effective preprocessing options.

Build a canonical effective scan configuration after CLI overrides. Hash it and store its JSON snapshot. Include detector/recognizer configuration, weights, preprocessing, sampling, chunk policy, geometry and clustering versions. Exclude aliases and report presentation from the reusable OCR identity. Record query/matcher settings separately. Once created, a run's reproducibility metadata is immutable; status and completion timestamps may change.

**Acceptance:** A new minimal config needs no HUD or OpenOCR checkout. Invalid settings fail before acquisition. Changed weights/settings produce distinguishable provenance. Query/report commands still work without inference dependencies loaded.

## 4. Storage model, legacy migration and scan selection (R02)

Implement the table relationships in HLD §4: `videos -> video_scans -> scan_chunks -> sampled_frames -> observations`, with `scan_runs -> video_scans`, `text_clusters -> cluster_observations -> observations`, and queries/hits referencing text clusters. Preserve `query_matches` as an extension: the HLD still requires borderline results, even though its illustrative schema omits that table.

### Required schema changes

- Keep `videos` as stable source metadata, including available resolution and an optional owned cache reference. Move execution status, failures, dimensions and timing to the appropriate scan/chunk records. A summary status may remain for compatibility, but it must not decide history or resume on its own.
- Add run configuration, detector/recognizer identities and hashes, dependency versions, preprocessing/normalization/geometry versions, start/end times and status.
- Add chunk ranges, sequence numbers, status, error, download mode and last committed processed timestamp. Add retry/attempt diagnostics rather than overwriting the only error record.
- Record sampled frames even when there are no detections or all detections are rejected. Persist all five HLD counters and optional full-frame path.
- Replace row-index-only observations with polygon, coarse region, normalized dimensions, separate detector/OCR confidence and a nullable path to that observation's actual crop.
- Replace row clusters with text clusters containing representative observation, representative geometry, motion summary and close reason. Persist each support link and score explicitly.
- Preserve conservative and compact normalization, either in stored columns or a versioned derived representation. The HLD's abbreviated `normalized_text` field is not a reason to discard the existing compact form.
- Adapt hits to text-cluster references, add creation timestamps, preserve query review state and uniqueness. Keep query settings/version identity sufficient to reproduce promotion behavior.
- Add the six indexes listed in HLD §4.1 and uniqueness constraints for run/video, chunk sequence, canonical sampled-frame identity, support links and query/cluster hits. Enforce that linked observations/representatives belong to the same video scan.

Geometry encoding: use an explicitly versioned codec for eight little-endian float32 values (four x/y pairs) in source pixels; validate shape, finiteness, winding and nondegeneracy. JSON exports decode the blob to readable coordinates. Never serialize raw bytes through `json.dumps`, assume native endianness, or require SQLite JSONB. Use a separate deterministic comparison/key representation for replay deduplication; float32 byte equality is not a geometric-overlap test.

### Legacy database migration decision

Append migrations after the existing two; do not edit applied migration definitions or require users to delete their databases. Prefer migration into a marked legacy lineage with explicit nullable/unknown historical fields, so one query/report layer can serve old and new data. New detector scans must satisfy the stronger geometry/provenance invariants even if imported legacy records cannot.

1. Exercise the migration on populated v1 and v2 fixtures, not only the current migration test's almost-empty database. Preserve a recoverable database snapshot before operational migration.
2. Create synthetic legacy run/video-scan records for the corpus actually present, preserving `scan_config_json`, `scan_signature`, model/HUD metadata and known dates. Mark missing detector, dependency, geometry and chunk details as unknown/legacy. Do not invent model hashes, source dimensions, polygons, or discarded historical scans.
3. Map `row_clusters` to `text_clusters`; map observations grouped by source and timestamp to legacy sampled-frame records; translate `row_cluster_id` into support links. Legacy frames' detector counters are unknown, not measured zero. A legacy whole-video chunk is a migration container, not evidence that chunked acquisition occurred.
4. Translate hit/query-match references and preserve reviews, notes, IDs where practical, and all existing evidence. Retain old row/HUD fields as legacy metadata if needed for interpretation.
5. Old `observations.crop_path` values often all reference the same representative file. Assign that path only to the actual representative if `evidence_timestamp_s`/`evidence_row_index` identify it unambiguously. If they do not, retain a marked legacy evidence reference; never claim every observation has its own image.
6. Validate counts, foreign keys, text, review records, exports and file references before completing migration. Migration failure must leave the previous database recoverable. Do not remove legacy tables until all necessary mappings have been preserved and validated.

### Reprocess/resume/query selection decision

Reprocessing creates a new video scan under a new run; it never deletes the previous completed scan or its files. Resume attaches to the same unfinished lineage only if its effective configuration and model identities match. A changed configuration requires a new run rather than silently continuing mixed inference.

Default query/report selection should choose the latest successfully completed scan for each video, including a migrated legacy scan when it is the only completed one. An incomplete or failed reprocess must not displace the previous successful scan. Historical scans remain addressable by explicit scan/run selection through shared selection helpers; whether this is initially a CLI selector or an internal export option should be documented. Do not query all historical scans by default and multiply candidate counts.

Preserve historical hits/reviews in their lineage. Reviews do not transfer automatically to a newly created cluster in a reprocessed scan. Rerunning an unchanged query against the same selected clusters continues to preserve reviews.

**Acceptance:** Populated migration preserves search and evidence; new reprocessing leaves old data intact; failed reprocessing leaves the old default corpus usable; selection never merges observations from different runs. Local user-owned input paths retain their ownership distinction.

## 5. Detection, crops and recognition (R03–R05)

### Detection and crop geometry

Add the HLD `TextDetector` adapter and a fake detector for deterministic tests. Run it on each full sampled frame. Return source-pixel quadrilaterals even when detector inference internally resizes to a 960-pixel minimum side. Keep source-frame coordinates distinct from detector-tensor coordinates and rectified-crop coordinates.

Stop unconditionally scaling every frame to 720 pixels high in `sample_frames()`. Preserve selected stream dimensions for inputs at or below 720p. For larger local test inputs, define an explicit bounded working-resolution policy and record any transform so source-coordinate geometry remains truthful. Record actual dimensions, rather than deriving them from a HUD profile.

Move general crop preparation out of `hud.py` into `crops.py`: canonicalize quadrilaterals, reject invalid/tiny boxes, rectify perspective, add configurable padding without clipping strokes, and preserve mappings back to source geometry. Fixed row slicing, blank-row contrast/edge filtering and mandatory 2x scaling must not remain hidden prerequisites of the new default path. HUD semantic classification is deferred; the 3x3 screen region is only geometry metadata.

### Paddle V1 adapters

Add a PP-OCRv6-small-rec adapter compatible with the existing `Recognition` result abstraction. Instantiate detector and recognizer once per worker in the same Paddle runtime/device context. Preserve batch order, validate result counts, and avoid loading PyTorch in the default process. Keep the existing OpenOCR adapter as an explicitly selected comparison implementation.

Implementing agents must verify the exact supported PaddleOCR/Paddle package APIs, model identifiers, weight artifacts and installation matrix on the target environment before pinning dependencies. The HLD model names are requirements, not a tested installer recipe. Record verified package versions and hashes; do not silently substitute a different model if weights or device support are unavailable. Prefer optional inference dependencies with clear setup errors so SQLite-only commands and unit tests remain lightweight. ONNX is a later, regression-tested backend rather than the initial implementation boundary.

### Retention pipeline and accounting

For each frame, apply this order deterministically:

1. Count raw proposals; validate polygons and minimum source-text height (initially 6 px).
2. Deduplicate proposals when polygon IoU is at least 0.85 or at least 90% of the smaller proposal is contained in another. Keep the higher detector confidence; use a stable geometry tie-break for equal scores.
3. Sort remaining detections by confidence and stable tie-break, then recognize at most 64. Count proposals dropped by the cap separately from geometric duplicates and invalid boxes.
4. Reject empty OCR, confidence below the configured floor, and strings with fewer than two Unicode letters/numbers. Count letters/numbers using Unicode categories, not ASCII-only matching. Preserve raw text plus both normalized forms for accepted observations.
5. Persist source polygons, normalized width/height and 3x3 screen region, then feed accepted observations into target-independent clustering.

Define missing-confidence behavior explicitly. Proposed policy: retain `None` unless the adapter contract requires a score; do not turn missing confidence into a fabricated high confidence. Standard Paddle results should supply real scores. Record rejection reasons or extra counters where needed to explain differences between raw and accepted counts. `recognition_count` counts attempted/returned crop recognitions, and `accepted_observation_count` counts unique stored observations, with retry accounting kept separate.

Retention must not inspect aliases. A query change must not change the text corpus or per-frame counters.

**Acceptance:** Tilted and edge-of-frame crops rectify correctly; resize round trips preserve geometry; overlapping proposals deduplicate before the cap; ties are deterministic; Unicode, blank, low-confidence and missing-confidence cases are covered. Names outside the old killfeed ROI enter the corpus. Zero-detection frames remain visible in accounting.

## 6. Streaming acquisition, chunk ownership and write recovery (R06–R07)

### Source and process lifecycle

Replace the normal `download() -> local_path -> sample_frames()` route with a context-managed source that opens one bounded video-only section through yt-dlp and FFmpeg. Keep `download.py` as the acquisition module if useful; a filename alone no longer represents the source contract. Local files use the same chunk/sample pipeline without a network download and are never treated as managed disposable files.

Use sequential 600-second chunks initially, shortening the last range to duration. Refresh metadata when a finite duration is missing; if no valid bounded VOD range can be established, record an actionable failure/skip rather than treating a live/unknown-duration stream as an unbounded V1 scan. Indexing's existing filtering behavior otherwise remains reusable.

The HLD's shell pipe is conceptual. Implement subprocesses with argument arrays and explicit pipe ownership. Resolve actual selected dimensions before consuming raw RGB bytes, without consuming the same media stream twice or issuing speculative concurrent downloads. Verify both yt-dlp and FFmpeg exit statuses. Drain/bound diagnostic output, close both processes on errors/cancellation, and use bounded termination followed by kill if necessary. Cover truncated frames and an upstream error that appears downstream as EOF.

Keep one active source stream per host, including retry/debug paths. Bound retries/backoff and record attempt reason, requested range, elapsed time and bytes when measurable. An archive may help indexing but never marks a chunk complete. Remove complete-file download retention from the default scan path; an optional diagnostic/failed-chunk cache has explicit ownership, size/lifetime bounds and cleanup.

### Timestamp and boundary contract

Do not assume a section request starts at an exact keyframe or that enumerating decoded frames alone recovers its source offset. Verify trimming/PTS handling on non-keyframe starts. Choose a global sample grid based on video time and FPS; add the actual chunk/source offset exactly once, and trim range overhang explicitly. Returned timestamps and polygons must refer to the original video, including retries and later chunks.

Implementation decision: each sample belongs to exactly one half-open core chunk interval `[start, end)`. Additional overlap frames supply continuity context and/or are mapped to their canonical core owner. A retry or a neighbor's overlap cannot create a second persisted sample with a different chunk ID for the same logical frame. Use integer sample indices or an explicitly defined timestamp quantization to avoid float-key drift.

Within the canonical sampled frame, deduplicate geometry deterministically. Use a stable detection key/quantization plus a geometric duplicate check where necessary; do not identify observations solely by OCR text or raw float bytes. Identical text in different boxes must survive. Keep detector settings fixed within a run, and test benign detector-coordinate jitter during replay.

### Commit and checkpoint semantics

`Store` already has WAL and foreign keys. Add `synchronous=NORMAL` and configurable WAL checkpoint/size handling, and verify the effective settings. Document that WAL size targets/checkpoints depend on readers and are not an unconditional hard cap while a reader pins WAL pages.

Replace the video-long write transaction with bounded batches, initially 20 seconds of video time, committed sooner at configured row/byte limits. Decode and inference must not hold a SQLite write transaction open. Commit frame rows, observations, cluster/support updates, counters and the processed watermark together. Advance a chunk's watermark only after all scheduled frames through that point are resolved, including empty frames and recognition batches spanning frame boundaries.

On failure, previously committed batches and chunks remain. Replay uncommitted work in the current chunk, optionally restarting its bounded range as the HLD allows. Idempotency must prevent duplicate observations, counters, support counts and evidence references. Do not rerun completed chunks. Mark a chunk complete only after checked normal source completion for its intended range and all results are durable; intentional bounded termination needs its own success rule.

Persist or deterministically reconstruct active cluster state from committed observations and cluster metadata. Reconstruction must include last compatible timestamp/geometry, representative text/confidence and enough recent motion context, without loading the full video corpus. Merge compatible clusters across completed neighboring chunks before publishing the video scan as complete. Reconciliation must itself be transactional and replayable, including support-link reassignment and representative selection.

The completed `video_scan` is the publication boundary for default queries; batches/chunks can be durable but not yet published. A crash between chunk completion and video publication is resumed/finalized without re-OCRing completed ranges. Recovery of stale `scanning` states must be safe under the documented single-writer model.

**Acceptance:** Crash injection before/after batch commit, during OCR, during boundary merge, and before publication produces the same finalized corpus as uninterrupted processing. Completed chunks survive; counters and support counts do not inflate; a moving notice across a boundary becomes one cluster. A failed source does not block the next queued video. Memory, temporary media and transaction size remain bounded over long inputs.

## 7. Geometry tracking and query behavior (R08–R09)

Replace `RowTracker`/`Cluster.row` with a tracker driven by source-normalized geometry, compatible text, timestamps and local motion. Preserve the existing target-independent behavior and the rule that two separate observations in one frame cannot support the same cluster twice.

The default missing-observation gap is 3 seconds: exactly 3 seconds is still eligible; more than 3 closes the cluster. Use overlap or normalized center distance for spatial compatibility. Estimate local text/line height from nearby detections and score approximately integer-line-height vertical shifts. Treat region tags as supporting metadata; crossing a 3x3 cell boundary alone should not split a plausible continuation.

Use frame-level deterministic association so detector result ordering cannot steal a match from a better candidate. Avoid a simple text-only greedy merge when simultaneous lookalike notices exist. A similarly named detection returning to a likely insertion location after a gap or incompatible motion must start a new cluster. There is no fixed killfeed coordinate or semantic killfeed classifier in this scope.

Store distinct-frame support count, canonical/best text, observed variants via support links, representative observation, motion summary and close reason. Use `time_gap`, `incompatible_text`, and `incompatible_motion` where supported; additionally distinguish end-of-scan finalization. A chunk boundary alone is not a semantic close reason. Do not close an old cluster merely because unrelated text appears nearby while the old notice is still visible.

Change `matching.promoted()`'s default gap from 8 to 3 seconds and update all configuration, callers, examples and benchmark expectations. Keep strong/weak thresholds and distinct-timestamp support. Increment `MATCHER_VERSION` for changed semantics, or incorporate a canonical matcher-settings hash into query identity. Otherwise existing queries appear unchanged even though promotion changed.

Refactor `run_query()` to join selected completed video scans, `text_clusters`, `cluster_observations` and `sampled_frames`. Keep substring/near-length fuzzy matching, short-name protection, borderline matches and review-preserving upserts. Match every supported raw OCR variant, not only canonical cluster text. Do not use queries to drive cluster creation. Keep query code free of model loading and media access.

**Acceptance:** Cover 3-second equality and greater-than-3 gaps; row shifts by one/multiple estimated heights; insertion of a new similar name; unrelated same-frame detections; static chat/scoreboard text; sparse frames; cross-region continuity; chunk-boundary equivalence; and deterministic detector-order permutations. Two weak matches eight seconds apart no longer promote. Existing normalization behavior remains intact.

## 8. Evidence, reports and lifecycle (R10–R11)

Current ingestion saves one best row/frame per cluster and assigns its mutable crop path to every observation. Replace this with immutable evidence identity: an observation crop depicts that observation; a sampled frame path depicts that frame. The cluster references its representative observation and derives the optional full frame through that observation. Most paths become null after compaction.

Save representative crop candidates while frames are available, using bounded memory or bounded temporary evidence. Write files atomically under scan-owned identities. Publish references only after files exist, and delete superseded files only after the replacement reference commits. Cleanup must account for every preserved historical run and resumable chunk, not just the currently selected corpus. Report generation must not delete files still used by an incomplete scan.

The HLD also requests evidence for borderline matches and confirmed hits. Sparse retention makes a limitation unavoidable: an arbitrary later query cannot recover a discarded crop from SQLite alone. Implementation decision: retain representative evidence for every useful cluster and compact superseded representatives independently of configured query names and aliases. If a later match's own crop is absent, show the cluster representative with its actual timestamp/text and label unavailable observation-specific evidence. Any future media re-fetch is an explicit separate operation; `query` must remain SQLite-only. Do not claim an unrelated representative image is the best matching observation.

Update `export_report()` and corpus export joins to include scan/run IDs, effective model/preprocessing/normalization provenance, source polygons, screen region, motion/close reason, support and representative timestamp. Keep HTML concise and review-oriented; detailed metadata belongs in JSONL/debug artifacts. Change image descriptions from fixed killfeed rows to detected text. Handle crop-only, frame-only and no-evidence cases without broken links.

Export `text_clusters.jsonl` as the current corpus format. Proposed compatibility: accept `report --rows`/`export_rows` temporarily as deprecated aliases, but document the new filename and a schema version. Provide a clear equivalent such as `--text-clusters`/`export_text_clusters`. Historical export requires explicit selection; default exports use the same completed-scan selection as queries. Regenerate obsolete exports during transition so stale `rows.jsonl` is not mistaken for current data.

Update `delete_video()` to cascade across all runs/chunks/frames/observations/clusters/hits/query matches for that source and remove all its owned evidence/cache files. Shared runs covering other videos must remain. Preserve ownership/path-containment protections and user-supplied local inputs. Orphan cleanup must use the new references and file naming, be restart-safe, and avoid active scan files.

Compaction primarily reduces evidence duplication and searchable cluster entries. Accepted observation metadata still grows linearly with detections and samples; do not delete support history needed for rematching or claim that clustering makes database storage constant.

**Acceptance:** Exact representative provenance survives replacement/retry; historical evidence remains valid; report/query performs no OCR; nullable images render cleanly; binary polygons export as coordinates; unchanged-query reviews persist; deleting a video removes all its generations without touching another video or local user media.

## 9. Evaluation data and measurement (R12–R13)

### Manual dataset and manifest

Create the first evaluation set from three roughly ten-minute real VODs with varied HUDs, resolutions, compression and backgrounds, manually annotating about 100 visible killfeed username occurrences plus negative regions. Record visible intervals, polygons, exact transcription, source and stable occurrence IDs. Include shifted/expired notices, dense activity and lookalike names. Supplemental full-frame examples outside the killfeed should verify the expanded detection scope.

The checked-in manifest is currently empty. Data selection, media availability and manual verification are real deliverables with effort separate from writing the harness. The old `~/Documents/Projects/killfeed-search` test data can supply candidates; neither filenames nor old OCR output are ground truth. Do not fabricate labels or declare accuracy gates met from fake-detector tests. No demo parser/render automation is needed for the first dataset.

Version the manifest and validate frame timestamps, dimensions, polygons, source grouping, exact text when CER is requested, and annotated-window duration. A single frame can contain several regions and several name occurrences: define explicit region/occurrence associations rather than assuming the HLD example's one `occurrence_id` applies to every box. Support zero-positive frames and distinguish unlabelled text from verified negatives. Reject source leakage across splits and inconsistent source durations. Adjacent frames of one occurrence stay grouped.

Keep a crop-only recognition mode for controlled model comparisons, but introduce frame/sequence and video-window modes that exercise the production detector, retention, tracker and matcher. Legacy crop manifests may be explicitly recognized as a different schema/mode; they cannot certify end-to-end performance.

### Metrics and experimental correctness

- Detection: region recall, under-20-pixel name recall, false boxes/frame and polygon coverage. Define matching/coverage thresholds and treatment of one box spanning multiple names. Evaluate proposal quality before retention and retained-name recall after dedup/cap so filtering losses are visible.
- Recognition: normalized exact match, CER and target-name recall on correctly associated crops. Name-only labels do not provide full-line CER.
- Matching: threshold precision/recall and positive/negative score distributions, including alias-length groups.
- End to end: evaluate actual predicted temporal clusters against labelled occurrences using time/geometry/name association. Ground-truth occurrence IDs group labels; they must not supply production prediction clusters. Count unmatched predicted hits and duplicate predicted events as appropriate rather than grouping predictions by ground-truth ID as the existing crop benchmark does.
- Reviewer workload: candidates and false candidates per query-video-hour, plus occurrence and video recall. Deduplicate source duration across frames/regions. Only exhaustively labelled query windows supply false-positive denominators; sparse positive frames do not establish a per-hour rate. Preserve null rates when coverage is insufficient.
- Breakdowns: source resolution, HUD, compression, text height, alias length and scene crowding/scenario tags.
- Uncertainty: 95% Wilson intervals for proportions, with numerators/denominators; source-group bootstrap for rates and other corpus-level metrics, with reproducible seed/resample count. Recompute ratio denominators when resampling sources. Document dependence between adjacent frames and the limited source coverage of only three VODs; grouped uncertainty should accompany end-to-end conclusions.
- Performance/operations: detection/recognition crop throughput, decode/scan video-hours per wall-hour, batch latency, process/native RAM and VRAM, download bytes per video-hour, temporary media/evidence footprint, and retry/failure counts. Mark unavailable counters explicitly. `tracemalloc`'s existing Python heap metric is not peak RAM/VRAM.

Measure the 10x compute-throughput target separately from download time using controlled local/cached input and the same pipeline, then also report real streaming wall time. In a pipelined run acquisition and inference overlap; subtracting arbitrary download duration from total wall time is not a valid compute benchmark. Record hardware, software, model hashes and effective config in every benchmark artifact.

Keep provisional gates visible: 95% occurrence recall, 97% detection recall, at most 0.5 false candidates per scanned query-video-hour, 10x compute scanning, and correct resume. Distinguish target, measured estimate, interval, and insufficient evidence. Passing deterministic tests or obtaining a point estimate above a gate is not proof of production accuracy.

**Acceptance:** Known synthetic misses, duplicate events, false positives, zero denominators and uneven source durations yield expected metrics. Frame-based evaluation catches a detector miss that crop-only evaluation cannot. Bootstrap never splits a source. Saved reports disclose coverage and uncertainty; the manually verified VOD dataset can run end to end through the same logic as scanning.

## 10. CLI, documentation and tests (R14)

### CLI and documentation changes

- Wire the new model factory and chunk coordinator into `scan`; add `--chunk-seconds`, preserve `--fps`, `--local`, `--pending`, `--video`, and explicit `--reprocess` with append-only semantics.
- Retry unfinished matching-config scans and failed chunks; skip completed scans unless reprocessing is requested. Update help/errors that currently say reprocess replaces rows.
- Add `detect FRAME_PATH --visualize` to save annotated source polygons and useful detector diagnostics without a video/HUD calibration step. Expose detection settings through the same config and adapter as ingestion.
- Remove `--profile` and `calibrate` from the default workflow. If temporarily retained, label them legacy tooling and avoid routing normal scans through them.
- Remove profile resolution from both `index_sources()` and `add_local()`. Preserve indexing/filter behavior and local ownership.
- Wire benchmark modes/model selection, new export naming, run selection and review/report behavior consistently. The existing `eval/run_benchmark.py` wrapper should continue forwarding to the revised CLI.
- Update `README.md`, `docs/usage.md`, `docs/evaluation.md`, package description and `config.example.yaml`: Paddle setup, full-frame detection, chunk/batch resume, append-only history, 3-second rules, retention limits, nullable evidence, manual-first evaluation and current exports.
- Remove statements that all low-confidence nonempty text is retained, that every observation points to a cluster crop, that reprocess invalidates/deletes the old corpus, or that one whole video is the resume unit. Explain legacy config/database conversion and which model comparisons remain optional.

### Existing tests that require intentional changes

| Current test / area | Required transition |
|---|---|
| `test_reprocess_replaces_hits_and_delete_removes_evidence` | Replace deletion assertions with preserved historical corpus/hits/evidence and newest-completed selection; retain explicit source-deletion coverage |
| `test_failed_reprocess_rolls_back_corpus_and_evidence` | Preserve old completed scan; new failed scan may retain committed resumable work. Do not require global rollback of the new generation |
| `test_consensus_requires_distinct_nearby_frames` | Change the eight-second accepted pair; test exact 3-second and greater-than-3 boundaries |
| `test_target_independent_tracker_moves_rows_but_not_same_frame` | Replace row-index fixtures with polygon/motion and simultaneous-notice fixtures |
| Corpus config/ingestion fixtures | Replace mandatory HUD profiles and fake row-only recognition with deterministic frame detector/recognizer doubles |
| `test_real_ffmpeg_streaming_and_failure` | Existing 320x180 -> 1280x720 assertion is obsolete; assert explicit source/working dimensions, source timestamps and chunk trimming |
| Archive/download adapter test | Retain for optional legacy/cache path only; add bounded section/retry/process cleanup tests for default acquisition |
| `test_export_has_provenance` | Assert run/scan lineage, decoded geometry, support links and actual representative evidence in `text_clusters.jsonl` |
| Migration and orphan recovery tests | Use fully populated old schemas and both historical/unfinished new scans; test new file ownership/reference rules |
| Benchmark occurrence test | Keep crop-mode coverage; add actual detector/tracker/end-to-end assignment, split, duration and interval tests |

Preserve the intent of query-without-OCR, rematching without corpus mutation, review persistence, queue continuation, normalization/short aliases, source filtering and owned local-file protection. Extend mocks to prohibit both detector and recognizer creation in query/report commands.

Add focused tests for geometry encoding/crop transforms, retention counters, configuration hashing, legacy migration, duplicate/reordered detection association, crash recovery, boundary merging and evidence compaction. Use real FFmpeg local fixtures for media timing; gate model/network integration tests explicitly on their required environment. Test source failure, early EOF, cancellation and subprocess cleanup rather than relying only on success-path mocks.

When implementing, run `uv run pytest` and `uv run ruff check src tests` plus the relevant real-model/media smoke tests available in the environment. Record skipped integration checks and benchmark evidence separately. Run these checks for each remaining delivery commit. Completed milestone results are recorded in ?11; they are separate from real-model evaluation.

## 11. Delivery sequence and completion boundaries

### Completed milestones: R01–R08 core

| Requirement | Commit | Delivered |
|---|---|---|
| R01 | `a5452e4` | Validated effective configuration, reproducibility identity and inference contracts; explicit legacy configuration path |
| R02 | `cdcd500` | Immutable scan lineage, recoverable populated legacy migration, preserved history/reviews/evidence, latest-completed selection |
| R03 | `d1c508c` | Full-frame detector interface and fake adapter, source-aware sampling, perspective rectification and local ingestion integration |
| R04, R05, R08 core | `6a65872`, `c403e2f` | Specified Paddle detector/recognizer, deterministic filtering and retention, geometry/motion clustering and local CLI scanning |
| R06, R07 integration | `b35d698` | Bounded local/remote chunks, incremental persistence, retry diagnostics, tracker restoration and completed-chunk resume |

At the R11 milestone, **80 tests passed** and Ruff passed for `src` and `tests`. A real Paddle scan completed two six-second local chunks of `gameplay-smoke.mp4` (12 sampled frames, 86 observations, 67 clusters) during R06/R07 validation; R09–R11 have deterministic tests but no new real-video accuracy measurement. Node-backed indexing of two configured YouTube URLs and video-only stream resolution for `ZiZmodw-yRc` succeeded, but a full remote OCR scan has not been measured. The earlier R01–R03 foundation passed 52 tests, including real FFmpeg fixtures. Original v1/v2 migration definitions and `high-level-design.md` remain unchanged. These results establish implementation behavior, not measured production accuracy or throughput. See [refinement-progress.md](refinement-progress.md) for details.

The current scanner runs the Paddle models, filters and clusters detected text, commits bounded chunks incrementally, and resumes completed chunks. R09–R11 use three-second query promotion, compact evidence, and export lineage-aware hits and text clusters. Labeled video evaluation, end-to-end metrics, and remaining CLI/documentation work remain R12–R14. R15 model comparisons are deferred.

### Remaining delivery sequence

The first three delivery groups below are complete at their stated core scope. The detailed acceptance criteria in §§3–10 still apply to the remaining work.

| Delivery group | Scope | Completion check |
|---|---|---|
| 1. Working local scanner — complete | **R04 + R05 + R08 core**, plus local CLI wiring from R14: Paddle detector/recognizer, deterministic proposal filtering/deduplication/caps, useful-text retention and counters, geometry/motion temporal clustering | Real local clips produced clustered text and evidence with the specified models. Deterministic tests cover filtering, accounting and temporal/motion boundaries; model/runtime versions and weight hashes are recorded. |
| 2. Streaming and recovery — complete | **R06 + R07**, R08 persistence/restoration and evidence consistency needed for safe recovery: bounded acquisition, chunk/sample ownership, incremental commits, retries/backoff, overlap deduplication and tracker restoration | A real Paddle scan completed two bounded local chunks; deterministic tests cover resume and remote acquisition. Live remote/model integration remains to be validated in an environment with network access. |
| 3. Complete search and review — core complete | **R09 + R10 + R11**, with remaining R14 CLI/documentation polish: three-second promotion, truthful representative evidence, retention/cleanup, lineage-aware reports and exports | Scan → query → report → review works. Historical reviews survive reprocessing, new clusters do not inherit reviews, nullable evidence is truthful, and SQLite-only rematching needs no inference dependencies. |
| 4. Evaluation and release readiness | **R12 + R13 + remaining R14**: annotated real-video fixtures, production-pipeline evaluation, uncertainty/performance reporting, CLI and documentation completion, regression coverage | Run the actual pipeline against labeled videos and report recall, false positives, reviewer workload, speed and remaining failures. Complete the CLI/migration documentation and all remaining V1 acceptance checks. |

Each remaining delivery group includes supporting documentation and passes `uv run pytest`, `uv run ruff check src tests`, and applicable real-model/media smoke tests before committing. These groups do not prescribe a fixed commit count or omit acceptance checks.

### Evaluation work for the next phase

- Select the three roughly ten-minute VODs, establish source-separated splits, and annotate about 100 visible username occurrences plus negative regions for R12.
- Use failures from the local smoke test and early labeled samples to drive fixes. Keep inference behavior, geometry and provenance consistent between scanning and evaluation.

### V1 completion and excluded follow-up work

Completion means all **R01?R14** deliverables and acceptance checks are implemented, old databases remain usable, new scans follow the HLD pipeline, interrupted scans recover correctly, and a real-data evaluation report exists. Accuracy/performance targets may remain unproven or unmet; report the measurements and uncertainty explicitly. Architecture completion and passing product gates are separate claims.

**R15 is outside this completion push.** Defer tiny-detector/SVTRv2 comparisons and ONNX export until the Paddle V1 is reliable and evaluated. Fine-tuning, VLM recovery, demo parsing/rendering and other ?12 backlog items also remain deferred. None is a prerequisite for finishing this V1 effort.

## 12. Deferred work and decisions to verify during implementation

The following remain deferred by the HLD: demo parsing/SourceDemoRender automation until useful for controlled variation; OCR VLM recovery; detector/recognizer fine-tuning; full HUD semantic classification; text-aware change detection; public search/API/server; distributed scanning; general YouTube crawling; multilingual expansion and large-scale policy/bandwidth planning. Preserve extensibility and provenance without implementing these systems as part of this refinement.

Specific uncertainties to resolve with evidence, without blocking unrelated work:

| Question | Owner / required evidence |
|---|---|
| Exact Paddle packages, model artifacts and CPU/GPU support | R04 resolved for Windows x64 CPU: packages, model hashes and real-model smoke tests are recorded in [local scanner validation](local-scanner-validation.md). GPU support remains unvalidated. |
| yt-dlp section behavior, source PTS and raw frame dimensions | R06/R07 implemented with local media and deterministic mocked remote tests; live remote/model integration remains environment-dependent. |
| Replay identity under detector jitter and range overhang | R07 has deterministic sample ownership and retry/recovery coverage; retain geometry tolerance checks during end-to-end evaluation. |
| Motion, confidence floor, padding and detector thresholds | R05/R08/R13: initial documented defaults, tune on source-separated labelled data |
| Ambiguous legacy representative evidence | R02/R10: preserve marked unknowns and legacy paths; no fabricated per-observation provenance |
| Evidence requested by arbitrary later aliases | R10: retain/show truthful cluster representatives; observation-specific recapture is separate from SQLite-only query |
| Statistical confidence from three VODs | R12/R13: publish source coverage and intervals, expand labels before claiming gates established |

The HLD's proposed ONNX boundary and alternative models are later measured deliverables. The V1 default remains PP-OCRv6-small detection and recognition in Paddle even though the existing OpenOCR adapter can be reused for comparisons.
