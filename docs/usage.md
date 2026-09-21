# Running the MVP

Install Python 3.10.11 or newer, FFmpeg and FFprobe on PATH, then run:

```powershell
uv sync --extra dev
Copy-Item config.example.yaml config.yaml
uv run tf2scan --help
```

All commands accept `--config PATH`. Configured paths resolve relative to that YAML
file; command-line local video paths resolve relative to the working directory.

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

## Local vertical slice and calibration

```powershell
uv run tf2scan calibrate "C:\videos\game.mp4" --timestamp 60
uv run tf2scan scan --local "C:\videos\game.mp4"
uv run tf2scan query --name AnotherPlayer --alias HistoricalName
uv run tf2scan report --rows
```

Calibration saves the original frame and annotated ROI/row boxes. Adjust the
profile in YAML until rows contain complete notices. Calibration requires FFmpeg,
but does not load OCR. Sampling scales to 720 pixels high and disables automatic
rotation because TF2 gameplay is expected to be landscape footage.

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
Downloads use video-only streams at up to 720p, partial-file continuation and an
archive. A removed download can be downloaded again for explicit reprocessing.

Completed videos are skipped even if the current query or model configuration
changes. Use `--reprocess` explicitly to replace a video's corpus for a new HUD,
model, preprocessing, or sampling rate. This invalidates that video's derived hits.
The old corpus and evidence remain valid if replacement ingestion fails.
Resume granularity is one video: interruption retries the unfinished video from
the beginning, while completed videos are never re-OCRed automatically.
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
are saved. Observation `crop_path` refers to that representative, not necessarily
the observation's own frame. The cluster's `evidence_timestamp_s` and
`evidence_row_index` identify that exact representative; `videos.scan_config_json`
records the sampling and preprocessing settings. Text variants remain available
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
