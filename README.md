# TF2 name scan

A local CLI that builds a reusable TF2 text OCR corpus, then searches
it for a username and aliases. Changing targets reruns matching only—no downloads,
frame decoding, or OCR.

Start with [setup and CLI usage](docs/usage.md), copy `config.example.yaml`, and
see [implementation status](docs/refinement-progress.md). The example config uses
full-frame Paddle detection and recognition for local files and bounded remote VODs. The verified runtime
uses Python 3.10.11; FFmpeg and FFprobe are required. YouTube scans also need
Node.js 22 or newer on `PATH`. SQLite stores corpus data and derived query hits; a static HTML
report provides timestamp links, evidence and review state.

```powershell
uv sync --extra paddle --extra dev
Copy-Item config.example.yaml config.yaml
uv run tf2scan scan --local game.mp4 --name HumanWorm
uv run tf2scan query --name AnotherPlayer
uv run tf2scan report --text-clusters
```

See the [high-level design](docs/high-level-design.md) and
[evaluation guide](docs/evaluation.md). No public search service is included.
