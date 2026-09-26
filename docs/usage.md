# Running the scanner

The default pipeline scans local files and bounded remote VOD sections with PP-OCRv6-small detection and
recognition. No HUD profile or calibration is needed. Query/report/index commands
need no inference setup. See [implementation status](refinement-progress.md).

## Local Paddle scanner

Verified on Windows x64 / Python 3.10.11 / CPU. Install FFmpeg and FFprobe on PATH:

```powershell
uv sync --extra paddle --extra dev
Copy-Item config.example.yaml config.yaml
uv run tf2scan scan --local "C:\videos\game.mp4" --name AnotherPlayer
uv run tf2scan query --name AnotherPlayer --alias HistoricalName
uv run tf2scan report --text-clusters
```

The optional `paddle` extra pins PaddleOCR 3.7.0, PaddleX 3.7.2 and
PaddlePaddle 3.3.1. Other platforms/Python versions need compatible upstream
wheels; GPU installation has not been validated here. Both adapters must use the
same device. The default runtime does not load PyTorch.

On first use, Paddle downloads the two official models under `models/paddlex/`
beside the config (or `PADDLE_PDX_CACHE_HOME` if set). For offline use, set
`detector.weights` and `recognizer.weights` to the extracted inference directories
containing `inference.json`, `inference.pdiparams` and `inference.yml`. To skip
Paddle's host connectivity probe for cached/local models, set
`PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True`. If Hugging Face is unavailable,
`PADDLE_PDX_MODEL_SOURCE=BOS` selects Paddle's official mirror.

The adapter explicitly maps design names `PP-OCRv6-small-det/rec` to official
`PP-OCRv6_small_det/rec` IDs. See the upstream
[detection](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/module_usage/text_detection.en.md)
and [recognition](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/module_usage/text_recognition.en.md)
APIs. Run metadata records actual artifact hashes, runtime versions and preprocessing.

`scan` retains useful text across the whole frame, clusters nearby observations,
and then searches the configured name (the example config uses `HumanWorm`).
`scan --name NAME` overrides that name for this run; `query --name NAME` searches
the saved OCR text later without scanning again. A zero-candidate scan can still
have many recognized text observations: it only means the selected name was not
promoted as a hit. `0 failures` means the scan completed successfully. `Query 2`
means database query ID 2, and the reported candidates are text clusters for
review, not necessarily distinct kill events. Each query/scan regenerates
`hits.jsonl` and `report/index.html` for its selected target; the underlying
observations and earlier query records remain in SQLite.

`report --text-clusters` exports the selected
corpus to `text_clusters.jsonl` (schema version 2). `--rows` remains a deprecated
alias that also writes `rows.jsonl`. Each cluster retains its representative crop;
configured query candidates can retain up to `evidence.max_candidates` additional
crops. Other observations have null crop paths after compaction. Set
`evidence.full_frames: true` to retain representative sampled frames.

Detection scans use bounded sections and commit frames at configured video-time,
row, or memory limits. Interrupted scans resume their matching run and skip durable
frames and completed chunks. A failed reprocess leaves the previous completed scan
and its evidence available.
Query promotion and detection clustering both use a three-second gap by default.
Query settings are stored separately from the immutable scan configuration. Later
SQLite-only queries can match observations whose own crops were compacted; the
report labels the representative image with its actual timestamp and text.

All commands accept `--config PATH`. Configured paths resolve relative to that YAML
file; command-line local video paths resolve relative to the working directory.

## Legacy configuration

For the commands below, use a separate legacy configuration (paths relative to it):

```yaml
pipeline: legacy_hud
output_dir: output
sources: []
query: {target_name: HumanWorm}
scan: {fps: 1, batch_size: 32}
ocr:
  repository: vendor/OpenOCR
  config: models/svtrv2-s/inference.yml
  checkpoint: models/svtrv2-s/best.pth
  use_gpu: auto
```

Legacy HUD defaults are supplied only in this explicit mode. The new detection
example uses `sampling`, `detector`, `recognizer` and `crops` settings; mixing old
`scan`/HUD/OpenOCR sections into a detection config is rejected.

## OpenOCR setup

Clone the [official OpenOCR repository](https://github.com/Topdu/OpenOCR) into
`vendor/OpenOCR` and install its PyTorch requirements into the same environment.
Follow its platform-specific PyTorch installation instructions for your GPU.
Download the **English SVTRv2-S config and checkpoint together** from the
[official model table](https://github.com/Topdu/OpenOCR/blob/main/configs/rec/svtrv2/readme.md).
Place the inference YAML and checkpoint under `models/svtrv2-s/` and set
`ocr.repository`, `ocr.config`, and `ocr.checkpoint` accordingly. Configs may
reference character dictionaries; relative dictionary paths resolve against the
OpenOCR checkout. Do not use a training-only config with a different decoder.

The adapter calls upstream `tools.infer_rec.OpenRecognizer` with an explicit config,
checkpoint, and PyTorch backend. It does not silently choose OpenOCR's default
mobile/Chinese model. The model version records the repository commit (or source
hash), config hash, checkpoint hash, and declared variant. For T/B comparisons,
use separate configs with each model's matching inference YAML and weights.

## Legacy local scanning and calibration

```powershell
uv run tf2scan calibrate "C:\videos\game.mp4" --timestamp 60
uv run tf2scan scan --local "C:\videos\game.mp4"
uv run tf2scan query --name AnotherPlayer --alias HistoricalName
uv run tf2scan report --text-clusters
```

Calibration saves the original frame and annotated ROI/row boxes. Adjust the
profile in YAML until rows contain complete notices. Calibration requires FFmpeg,
but does not load OCR. Sampling preserves dimensions up to 720 pixels high; larger
inputs are reduced to a configurable working height while frame records retain
source dimensions and the working-to-source scale. Automatic rotation is disabled
because TF2 gameplay is expected to be landscape footage.

`scan` ingests **all nonblank OCR rows**, then runs the configured query if present.
`query` uses only SQLite and never instantiates OCR or opens video files. You can
remove the `query` section to build a corpus without a target. Short aliases (up to
four compact characters) require an exact normalized substring; this can still
match inside a longer name, so review short-name hits carefully.

## Acquisition and resume

```powershell
uv run tf2scan index config.yaml
uv run tf2scan scan --pending
uv run tf2scan scan --video YOUTUBE_ID
uv run tf2scan scan --video YOUTUBE_ID --fps 2 --reprocess
```

Indexing expands each configured source and applies inclusive dates, regex title,
and duration filters. Missing metadata needed by a filter causes a skip. Indexing
again preserves completed scan state. Failed videos are retried by `--pending`.
Detection scans resolve a video-only stream with yt-dlp and pass bounded sections
to FFmpeg. They do not keep a full media download. A finite VOD duration is required.
YouTube indexing and stream resolution enable Node.js through yt-dlp's Python
equivalent of `--js-runtimes node`. Install Node.js 22 or newer on `PATH` and run
`uv sync --extra paddle --extra dev`; the project's yt-dlp default extra installs
the matching EJS challenge scripts. `tf2scan` does not accept yt-dlp's CLI flag.
The separate Python 3.10 deprecation warning is unrelated to the JavaScript runtime.
Legacy HUD scans still use managed whole-file downloads.

Completed videos are skipped even if the current query or model configuration
changes. Use `--reprocess` to create a new scan/run for changed HUD, model,
preprocessing or sampling settings. Historical observations, reviews, hits and
evidence remain intact. Default queries/reports select the latest completed scan
per video; failed or incomplete reprocessing does not replace the prior selection.
Resume keeps completed chunks and committed batches. The current chunk is requested
again with overlap, but persisted frames have one canonical core chunk owner.
`--chunk-seconds` overrides the default 600-second section length; `--fps` sets
the global sample grid. Failed chunks use bounded exponential backoff.
Only one writer/scanner should run against an output directory at a time.

With `retain_downloads: false`, managed downloads are removed only after ingestion,
matching, and report generation succeed. User-supplied local videos are never
removed by retention cleanup. `delete-video ID` explicitly removes a source's
corpus, hits, owned evidence, and managed download, then refreshes exports.

## Corpus and review

`output/results.sqlite3` is authoritative, with transactional schema migrations.
Detection observations retain useful OCR text, source polygons, frame timestamps,
screen region and separate detector/OCR confidence. Motion-aware `text_clusters`
link supporting observations across frames within a three-second gap; distinct
detections in one frame cannot support the same cluster twice. Immutable
`scan_runs` record effective model and preprocessing provenance, while
`video_scans`, chunks and sampled frames trace each cluster to its source.

Each detection cluster retains its actual representative observation crop.
Configured query candidates may retain a bounded number of additional crops;
other accepted observations keep their text and geometry with null crop paths.
Optional full frames are retained for representative frames. Reports show a
matched observation crop when available; otherwise they identify the cluster
representative by its own text and timestamp and mark the matched crop unavailable.
Scanner-owned orphan files are cleaned up after committed batches and reports.

The explicit `legacy_hud` pipeline still uses its eight-second row tracker and
row-index metadata. Migrated ambiguous evidence remains marked in
`legacy_evidence_path` instead of being assigned to an observation without proof.
Both pipelines remain queryable through the text-cluster lineage.

Blank OCR output is discarded. Detection retention also applies the configured
confidence floor and useful-text rule before storage.
Query scores of at least 0.65 are retained in `query_matches` for borderline review.
Hits require a score of 0.95, or two distinct frames within three seconds at 0.82.

`report/index.html` has sortable columns, crop/full-frame evidence and timestamp
links. All displayed OCR and metadata is HTML-escaped. Review locally:

```powershell
uv run tf2scan review 42 confirmed --note "Name clearly visible"
uv run tf2scan review 43 rejected
```

Rerunning an unchanged query preserves reviews. `hits.jsonl` exports the selected
query (or all queries with `report`). `report --text-clusters` exports target-independent
clusters to `text_clusters.jsonl`. Use `--scan-id` or `--run-id` for a historical
corpus in `report`; Python `run_query` also accepts `scan_id` or `run_id` for
historical rematching. These files can be regenerated from SQLite without OCR.
No server, public API, VLM fallback, fine-tuning, or search-engine service is included.

## Validation and evaluation

```powershell
uv run pytest
uv run ruff check src tests
uv run tf2scan benchmark eval/manifest.jsonl --output benchmark-s.json
```

The checked-in manifest is intentionally empty rather than pretending unverified
prototype data is ground truth. See [evaluation.md](evaluation.md) to label samples.
The test suite uses controlled recognizer doubles to test corpus/search behavior
without downloading model weights, and exercises real FFmpeg when installed.
The 95% recall, 0.5 false hits/hour and 10x throughput goals require a labeled
dataset and actual model/hardware runs; unit tests do not establish those gates.
