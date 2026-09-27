# Using TF2 name scan

The app reads text from sampled TF2 video frames, groups repeated sightings of the same text into **text clusters**, and searches those clusters for a player name. You can search the saved text for more names without scanning the video again.

## Set up

Install Python 3.10.11, [uv](https://docs.astral.sh/uv/), FFmpeg, and FFprobe. Make sure `ffmpeg` and `ffprobe` are on your `PATH`. From the project directory, run:

```powershell
uv sync --extra paddle
Copy-Item config.example.yaml config.yaml
```

The first scan downloads the OCR models. The example config puts results in `output/` and has an example search name; replace it in `config.yaml` or supply `--name` when scanning.

## Scan a local video

```powershell
uv run tf2scan scan --local "C:\videos\game.mp4" --name AnotherPlayer
```

The scan saves recognized text and searches for `AnotherPlayer`. It prints candidate matches and a summary of saved clusters. A candidate is a text cluster worth checking, not a confirmed kill or a unique event. `0 failures` means processing completed; zero candidates means this name had no promoted matches, even if other text was found.

To scan without searching for a name, remove the `query` section from `config.yaml` and omit `--name`.

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
