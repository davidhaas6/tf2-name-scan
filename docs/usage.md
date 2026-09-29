# Using TF2 name scan

The app reads text from sampled TF2 video frames, groups repeated sightings of the same text into **text clusters**, and searches those clusters for a player name. You can search the saved text for more names without scanning the video again.

## Set up

Install Python 3.10.11, [uv](https://docs.astral.sh/uv/), FFmpeg, and FFprobe. Make sure `ffmpeg` and `ffprobe` are on your `PATH`. From the project directory, run:

```powershell
uv sync --extra paddle
Copy-Item config.example.yaml config.yaml
```

The first scan downloads the OCR models. The example config puts the corpus in `output/`. Scanning needs no target; optional `query.target_name` and `query.aliases` defaults apply only to `query`.

The supplied configuration uses `sampling.region: top_right`: only the top-right
quarter of each sampled frame is sent to the detector. The scanner keeps source
coordinates for observations and uses the original sampled frame for evidence.
This can reduce detection and recognition work, but names elsewhere in the frame
will be missed. Set `sampling.region: full` to scan the whole frame. Changing the
region requires `scan --reprocess` for videos already scanned.

The CPU example enables `recognizer.enable_mkldnn: true`. On `input/uw1.mp4`,
recognition took 19.91 seconds without it and about 5–6 seconds in oneDNN trials
with the same 107 crops. Keep `detector.enable_mkldnn: false`: that setting failed
on the installed Paddle runtime. The example keeps `recognizer.pool_frames: 1`
because four-frame pooling did not improve recognition time and changed some OCR
strings. The default `cpu_threads: 10` matches the runtime's existing setting;
the four-thread trial was slower. These short, single-clip measurements are not
an accuracy evaluation.

## Scan a local video

To profile one cached local video with the detection pipeline, run:

```powershell
uv run tf2scan scan --local "C:\path\to\cached-video.mp4" --perf-output "output\scan-profile.json"
```

The flag is optional. Add `--reprocess` if that video was already scanned, and
`--fps N` to override the configured sampling rate. The JSON records scan wall
time, video duration, real-time factor (`wall time / video duration`), source
working and detector-input resolution, CPU/platform, frame and crop counts, effective scan
settings, model setup time, and stage times. Stage times cover decoding,
detection, crop preparation, recognition, persistence/evidence, and startup
orphan recovery. `other` includes scan bookkeeping and time outside those
measured sections. `largest_stage` names the largest measured category. Model setup is
reported separately and is excluded from the scan real-time factor. Profiling
currently applies to the detection pipeline and one video per command.

`persistence_detail_s` breaks the batch persistence timer into the SQLite/tracking
transaction (including image saves), image saves, evidence compaction, and the
orphan sweep's database-reference lookup and file walk in older profiles. The `*_other` values
subtract nested timings so they do not double-count. `persistence_counts`
records batch commits, compaction calls, images saved, and asset files
checked. Routine compaction now visits changed clusters and removed evidence
paths; a full orphan sweep runs once at startup per open corpus. Startup time
appears as `startup_cleanup` in new profiles.
`persistence_batches` gives the same timers and file count for each batch, so
you can see whether routine cleanup cost grows as the corpus grows.

```powershell
uv run tf2scan scan --local "C:\videos\game.mp4"
```

The scan saves recognized text and prints completed, skipped and failed video counts plus the saved corpus totals. It does not run a search or generate exports. Existing reports remain unchanged until `query` or `report` runs. `--name` and `--alias` are query options only.

Evidence retention is independent of configured names and aliases: each cluster keeps its highest-confidence representative crop. With `evidence.compact: true`, superseded representatives are removed; disabling compaction keeps earlier representative crops too. The former `evidence.max_candidates` option is ignored and can be removed from existing configs. All accepted OCR observations remain searchable, even when their crops are not retained. Previously retained crops remain available in existing completed scans.

Search the corpus and generate a report:

```powershell
uv run tf2scan query --name AnotherPlayer
```

A candidate is a text cluster worth checking, not a confirmed kill or a unique event. Zero candidates means this name had no promoted matches, even if other text was found.

## See and review search results

Open the generated report in a browser:

```powershell
Invoke-Item .\output\report\index.html
```

The report shows each candidate's video, time, matched OCR text, score, supporting frames, and available image evidence. Click a column heading to sort. For an online video, click its time to open the source at that point. Image evidence may show the cluster's representative text if the matching observation's crop was not retained; the report labels which image it shows.

The **Hit** column gives the ID for recording a review:

```powershell
uv run tf2scan review 42 confirmed --note "Name clearly visible"
uv run tf2scan review 43 rejected
```

The same candidates are also written to `output/hits.jsonl`, one JSON object per line. The HTML report is the easiest way to inspect them. You can regenerate it with `uv run tf2scan report`; without `--query-id`, that command includes hits from all saved queries for the selected scans.

## Search for another name

```powershell
uv run tf2scan query --name AnotherPlayer --alias HistoricalName
Invoke-Item .\output\report\index.html
```

Repeat `--alias` for additional spellings. This searches saved OCR text and performs no video decoding or OCR. The command prints a query ID and candidate count. Immediately after a query, `output/report/index.html` and `output/hits.jsonl` contain that query's candidates. To regenerate a particular query's report later, use `uv run tf2scan report --query-id ID` with the printed ID. Reviews of unchanged query hits are preserved.

## Export all recognized text clusters

```powershell
uv run tf2scan report --text-clusters
```

This writes `output/text_clusters.jsonl`. Each line is one cluster of related text sightings across nearby frames, whether or not it matched a searched name. A record includes the video and time range, representative text and evidence path, plus its individual OCR observations with timestamps, recognized strings, confidence values, and crop paths when retained. It is useful for inspecting what the scanner read or doing your own analysis. It is **not** a list of query matches; use the HTML report or `hits.jsonl` for those. A cluster can contain several observations, and clusters do not necessarily correspond one-to-one with kill events.

The authoritative saved data is `output/results.sqlite3`. The JSONL files and HTML report can be regenerated from it. Running `query` or `report` without `--text-clusters` removes the previous `text_clusters.jsonl` export, so rerun the export when you need it.

## Scan indexed online videos

Add sources in `config.yaml`, then run:

```powershell
uv run tf2scan index config.yaml
uv run tf2scan scan --pending
```

YouTube scanning also needs Node.js 22 or newer on `PATH`. Use `uv run tf2scan scan --video YOUTUBE_ID` for one indexed video. Completed videos are skipped on later scans; use `--reprocess` when you intentionally want a new scan with changed scan settings. Interrupted scans can resume, and failed videos can be retried with `--pending`.

All commands accept `--config PATH` if your config file has another name or location. Paths inside the config are relative to that file; `--local` paths are relative to your current directory.

For configuration and implementation details, see the [high-level design](high-level-design.md) and [implementation status](refinement-progress.md).
