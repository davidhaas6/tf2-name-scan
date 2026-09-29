# TF2 name scan

A local CLI that builds a reusable TF2 text OCR corpus, then searches
it for a username and aliases. Changing targets reruns matching only—no downloads,
frame decoding, or OCR.

See [CLI usage](docs/usage.md) and [implementation status](docs/refinement-progress.md).
SQLite stores corpus data and derived query hits; a static HTML
report provides timestamp links, evidence and review state.

## Install

The current dependency configuration targets Windows x64 and Python 3.10.
Install Git, uv, and FFmpeg (including FFprobe), with their commands on `PATH`.
YouTube scans also require Node.js 22 or newer on `PATH`. In PowerShell:

```powershell
git clone https://github.com/davidhaas6/tf2-name-scan.git
cd tf2-name-scan
uv python install 3.10.11
uv sync --extra paddle --extra dev
Copy-Item config.example.yaml config.yaml
```

`uv sync` installs the CLI, OCR dependencies, and development tools into `.venv`.
The first scan downloads the OCR models. Edit `config.yaml` to choose sources
and scan settings; results default to `output/`.

## Run tests

From the repository directory after installation:

```powershell
uv run pytest
uv run ruff check src tests
# Run one test file while working on scanning:
uv run pytest tests/test_local_scanner.py
```

FFmpeg-dependent tests are skipped if FFmpeg is unavailable.

See the [high-level design](docs/high-level-design.md) and
[evaluation guide](docs/evaluation.md). No public search service is included.

## Usage

```bash
# Build a reusable OCR corpus from a local recording.
uv run tf2scan scan --local game.mp4

# Index online sources from config.yaml, then scan or resume pending videos.
uv run tf2scan index config.yaml
uv run tf2scan scan --pending

# Search names and aliases; each query generates a report without rerunning OCR.
uv run tf2scan query --name HumanWorm --alias OldName --alias AlternateSpelling
uv run tf2scan query --name AnotherPlayer

# Review a candidate and export all recognized text for further analysis.
# Replace 42 with a hit ID from output/report/index.html.
uv run tf2scan review 42 confirmed --note "Name clearly visible"
uv run tf2scan report --text-clusters

# 5. Reprocess a recording at a chosen sampling rate and capture stage timings.
uv run tf2scan scan --local game.mp4 --reprocess --fps 2 --perf-output output/scan-profile.json

# Scan one YouTube video (YouTube scanning requires Node.js 22+ on PATH).
# Set sources: ["https://www.youtube.com/watch?v=YOUTUBE_ID"] in config.yaml.
# Replace YOUTUBE_ID in the URL and command with the video's ID.
uv run tf2scan index config.yaml
uv run tf2scan scan --video YOUTUBE_ID
uv run tf2scan query --name HumanWorm

# Scan a YouTube playlist, then resume interrupted scans or retry failures.
# Set sources: ["https://www.youtube.com/playlist?list=PLAYLIST_ID"] in config.yaml.
# Replace PLAYLIST_ID with the playlist's ID before indexing.
uv run tf2scan index config.yaml
uv run tf2scan scan --pending
# After an interruption or failure, rerun; completed videos are skipped.
uv run tf2scan scan --pending

# 8. Reprocess one indexed YouTube video at 2 fps and profile its scan.
uv run tf2scan scan --video YOUTUBE_ID --reprocess --fps 2 --perf-output output/youtube-profile.json
uv run tf2scan report --text-clusters
```
