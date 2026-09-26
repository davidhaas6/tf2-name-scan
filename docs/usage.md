# Running the scanner

The default pipeline scans local files and bounded remote VOD sections with PP-OCRv6-small detection and
recognition. No HUD profile or calibration is needed. Query/report/index commands
need no inference setup. See [implementation status](refinement-progress.md).

## Local Paddle scanner

Verified on Windows x64 / Python 3.10.11 / CPU. Install FFmpeg and FFprobe on PATH:

```powershell
uv sync --extra paddle --extra dev
Copy-Item config.example.yaml config.yaml
uv run tf2scan scan --local "C:\videos\game.mp4"
uv run tf2scan query --name AnotherPlayer --alias HistoricalName
uv run tf2scan report --rows
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
and then runs the configured query. `report --rows` exports all clusters to
`rows.jsonl`. Each cluster keeps a crop for its highest-confidence representative;
other observations may have no crop. Set `evidence.full_frames: true` to also
retain sampled frames with accepted text. There is no evidence compaction yet.

Detection scans use bounded sections and commit frames at configured video-time,
row, or memory limits. Interrupted scans resume their matching run and skip durable
frames and completed chunks. A failed reprocess leaves the previous completed scan
and its evidence available.
The matching/report transition follows in R09-R11: query promotion still uses its
legacy eight-second rule, while detection clustering uses three seconds. The
evidence compaction and final report transition remain in R10/R11.

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
uv run tf2scan report --rows
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
Observations record all useful OCR strings, timestamps, row indices, confidence,
and normalization/model/HUD provenance. Row clusters deduplicate similar text in
neighboring rows within eight seconds, allowing notices to move down the feed.
Separate notices in the same frame cannot merge. Similar repeated events can
still merge; this is a retrieval heuristic, not exact death-event reconstruction.

Only the highest-confidence representative row crop and full frame per cluster
are saved. Only the actual representative observation has a `crop_path`; other
observations have null paths. Migrated ambiguous evidence remains explicitly marked
in `legacy_evidence_path`. Immutable `scan_runs` contain effective configuration
and model provenance; `video_scans`, chunks, frames and support links trace each
text cluster to its source. Text variants remain available
to subsequent queries. Report generation cleans up scanner-owned orphan images
left by abruptly terminated scans.
Blank OCR output is discarded; nonmatching and low-confidence nonempty text stays.
Query scores of at least 0.65 are retained in `query_matches` for borderline review.
Hits require a score of 0.95, or two distinct frames within eight seconds at 0.82.

`report/index.html` has sortable columns, crop/full-frame evidence and timestamp
links. All displayed OCR and metadata is HTML-escaped. Review locally:

```powershell
uv run tf2scan review 42 confirmed --note "Name clearly visible"
uv run tf2scan review 43 rejected
```

Rerunning an unchanged query preserves reviews. `hits.jsonl` exports the selected
query (or all queries with `report`). `report --rows` exports target-independent
clusters to `rows.jsonl`. These files can be regenerated from SQLite without OCR.
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
