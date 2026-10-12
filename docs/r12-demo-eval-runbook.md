# R12 demo-window eval runbook

How to reproduce the demo-window evaluation. Design and results are in
[demo-window validation](demo-window-validation.md); this page is just the
command sequence.

The pipeline is: parse the demo → pick a window → render it with SVR → scan the
render → run the query evaluation. Only the SVR render is manual.

## Prerequisites

- Vendored `lib/tf-demo-parser` with the `parse_demo` binary on `PATH` or at
  `~/.cargo/bin/parse_demo.exe` (no build needed).
- `lib/svr` extracted with `svr_launcher64.exe` and a D3D11 GPU.
- The demo in `input/` (e.g. `input/test_9v9_demo.dem`).
- `uv sync --extra paddle` done; `ffmpeg`/`ffprobe` on `PATH`.
- Dedicated config `eval/r12_demo_validation/config.yaml` (production sampling
  defaults, its own `output_dir`), and profile `lib/svr/data/profiles/r12_eval.ini`.

Run everything from the project root unless noted.

## 1. Parse the demo and pick a window

Pick a window using the documented criteria (no pause, no name changes, ~8+
distinct names spread across the window, inside one active round). Inspect the
cached parse (`output/r12-demo/parse_full.json`) for `deaths` and `users`.
Window times are seconds; `tick = seconds / intervalPerTick`.

Extract the ground-truth name set G for the chosen window:

```powershell
uv run python eval/r12_demo_validation/extract_ground_truth.py `
  --demo input/test_9v9_demo.dem `
  --window-start 1320 --window-length 180 `
  --output output/r12-demo/ground_truth.json
```

G = distinct names appearing as killer or victim of any `PlayerDeath` in the
window (world/suicide victims included and flagged; assisters excluded).

## 2. Render the window with SVR (manual)

Render a **superset** `[T0 − 10 s, T1 + 10 s]` so boundary kills and HUD lag are
covered (200 s here). See [demo-window validation §3](demo-window-validation.md).

1. Copy the demo where TF2 can find it (game profile uses `demo_path=tf`), e.g.
   into the TF2 `tf/` directory.
2. Force 1920x1080 (add to TF2 launch parameters if the profile does not honor
   it), then start SVR from `lib/svr`:

   ```powershell
   cd lib\svr
   .\svr_launcher64.exe tf2_steam.ini
   ```

3. In the game console:

   ```
   playdemo test_9v9_demo
   demo_gototick 87333
   startmovie r12_window timeout=200 profile=r12_eval
   ```

   `demo_gototick` is `tick(T0 − 10 s) = (1320 − 10) / 0.015 ≈ 87333`.
   `timeout=200` matches the superset length. `endmovie` is not needed with
   `timeout`.

4. Output lands in **`lib/svr/movies/`** (`lib/svr/movies/r12_window.mp4`), not
   `data/movies/` as the SVR README suggests. Verify it:

   ```powershell
   ffprobe -v error -select_streams v:0 `
     -show_entries stream=width,height,r_frame_rate -show_entries format=duration `
     -of default=noprint_wrappers=1 "lib\svr\movies\r12_window.mp4"
   ```

   Expect 1920x1080, 60 fps, 200.000 s. (`r12_eval.ini`: libx264 CRF 15, no
   audio.)

## 3. Scan the render

Uses the dedicated config; `output_dir` resolves to `output/r12-demo`. Add
`--reprocess` if the render was already scanned.

```powershell
uv run tf2scan scan --local "lib\svr\movies\r12_window.mp4" `
  --config eval/r12_demo_validation/config.yaml `
  --perf-output output/r12-demo/scan-profile.json
```

Confirm a complete 1 fps sample grid (`frames` = window length in the profile).

## 4. Presence evaluation

Queries every name in G through the production query path and classifies
matches. Without `--manual-review` it emits bounds only; supply the previous
`manual_review.json` once `needs_review` names are decided by hand.

```powershell
uv run python eval/r12_demo_validation/evaluate_presence.py `
  --ground-truth output/r12-demo/ground_truth.json `
  --config eval/r12_demo_validation/config.yaml `
  --db output/r12-demo/results.sqlite3 `
  --scan-profile output/r12-demo/scan-profile.json `
  --output output/r12-demo/presence_results.json `
  --manual-review output/r12-demo/manual_review.json
```

`manual_review.json` is a small map such as
`{"[MOM$] Rizz HuzzHouse": true}`.

## 5. Optional: negative battery and positive audit

Uses the demo's exhaustive `users` roster as free labeled negatives.

```powershell
uv run python eval/r12_demo_validation/negative_queries.py `
  --parse-cache output/r12-demo/parse_full.json `
  --ground-truth output/r12-demo/ground_truth.json `
  --config eval/r12_demo_validation/config.yaml `
  --db output/r12-demo/results.sqlite3 `
  --output output/r12-demo/negative_results.json
```

## Outputs

All under `output/r12-demo/` (gitignored):

| File | Meaning |
|---|---|
| `parse_full.json` | cached full `parse_demo` JSON |
| `ground_truth.json` | window, per-kill table, G, counts |
| `scan-profile.json` | coverage, RTF, stage timings |
| `presence_results.json` | per-name candidates, coverage, metrics |
| `manual_review.json` | hand decisions for `needs_review` names |
| `negative_results.json` | negative battery + positive audit |
| `results.sqlite3` | the dedicated corpus |

## Notes

- Render hygiene: the SourceTV HUD drew verbose killfeed rows and an FPS/CPU/GPU
  overlay, making this an easier-than-production input. Disable overlays in
  future renders so cluster stats reflect gameplay text only.
- Recall ceiling: 1 fps + `top_right` + 720p cap can each miss transient,
  crowded or off-region rows; misses are pipeline-wide, not recognition-only.
- One window and ~18 names carry almost no statistical weight; counts are the
  result, and no HLD gate is established.
- For a second window, re-run steps 1–4 with a new `--window-start` and a
  separate output directory/corpus.