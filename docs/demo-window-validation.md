# Demo-window ground-truth validation (R12 demo-derived first slice)

Status: **implemented and measured (2026-10-12).** Plan below; results in the
[Results](#results) section. For the command sequence, see the
[R12 demo-window eval runbook](r12-demo-eval-runbook.md).

This is a deliberately minimal slice of R12: compare an unordered set of
names the local pipeline reports against an unordered set of ground-truth names
from a demo, for one short window. It answers one question only: *does the
pipeline find every player who got a kill or died on the server during this
window?* R12 records it as the demo-derived accuracy evidence. R13 adds occurrence-level alignment of detections to kill timestamps and a little more demo coverage before the ship check; the manually labeled real-video set is deferred post-MVP.

## 1. Demo and time window

- Demo: `input/test_9v9_demo.dem` — SourceTV demo (`na.serveme.tf #634602`),
  map `pl_eruption_b16`, game `tf`, playback 1535.640 s (25:36), 102,376 ticks
  at 66.67 tick (0.015 s per tick).
- Window: **180 seconds by default** (a parameter of the extraction script,
  not a hardcoded constant) of continuous active play, selected in
  implementation step 0 (after parsing) by these documented criteria, in
  priority order:
  1. No round pause overlapping the window (parser reports pauses).
  2. No player name changes among window participants (parser applies
     `change_name`; we check the affected names are absent or unchanged).
  3. At least ~8 distinct killer/victim names, with kills spread across the
     window rather than bunched in one fight. Shrink the default 180 s only if
     the shorter window still yields a comparable name count.
  4. Window lies strictly inside one round of active play (avoids setup gates
     and round-end screens with no killfeed).
  If criteria cannot be met, relax in this order: shorten to 120 s, then accept
  a name change and flag it. The selected window (start/end ticks, seconds,
  kill count) is recorded in the results section before anything is measured.

## 2. Ground-truth set G

- Source of truth: the demo's own event stream, read with the vendored
  `lib/tf-demo-parser` `parse_demo` binary (default analyzer, JSON output:
  `users` as UserId → name, and `deaths` with killer/victim/assister/weapon/
  tick). The binary is already installed at
  `C:\Users\David\.cargo\bin\parse_demo.exe` (verified against this demo);
  no build or download is required.
- Rule: **G = the set of distinct player names appearing as killer (attacker
  UserId != 0) or victim of any `PlayerDeath` whose tick falls in the
  window.** Victims are always included, including world/suicide kills
  (attacker 0), with a per-name `world_kill_only` flag: TF2 killfeed display
  of such deaths is uncertain, so any miss on such a name is reported with
  that caveat. Assists are excluded: the TF2 killfeed does not name assisters,
  so an assister-only name would make recall look artificially low.
- Why complete for the window: a demo is a complete recording of the server's
  game-event stream; every kill in the window raised exactly one
  `PlayerDeath` event, and every killer/victim UserId resolves to an entry in
  the `users` table. So G is *exactly* the set of kill-event participants —
  not necessarily the set of killfeed-*visible* names. Two known divergences
  are recorded, not assumed away: (a) world/suicide kills (attacker 0) name
  only the victim, and TF2 killfeed display of such deaths is uncertain
  (flagged per name); (b) killfeed notices appear with a small HUD lag
  relative to the event tick. Both are listed per-name in the results.

## 3. Video for the pipeline

- Rendered with SVR (`lib/svr`, third-party; rendering only) into
  `lib/svr/movies/` (SVR wrote next to its launcher, not `data/movies/` as its
  README suggests), using a documented superset window:
  **[T0 − 10 s, T1 + 10 s]** (200 s for a 180 s window). The ±10 s margin
  absorbs seek imprecision and keeps killfeed notices for late-boundary kills
  on screen inside the window; set comparison is unaffected by the margin.
- Manual procedure (SVR cannot be driven headless): start TF2 via
  `svr_launcher64.exe tf2_steam.ini` (the profile already passes fixed
  launch args; resolution is forced to 1920x1080 — added to the game's launch
  parameters for this session if the game does not otherwise honor them),
  console `playdemo test_9v9_demo`, then
  `demo_gototick <tick(T0−10s)>`, then
  `startmovie r12_window timeout=200 profile=r12_eval`.
- Custom profile `lib/svr/data/profiles/r12_eval.ini` overriding the default:
  `video_encoder=libx264`, `video_x264_crf=15`, `video_x264_preset=faster`,
  `video_fps=60`, `audio_enabled=0`, `lagcomp_override=0.1` (default;
  0.1 s is irrelevant to a name-set comparison). Target resolution
  **1920x1080 at 60 fps**, confirmed via `ffprobe` on the rendered file and
  recorded in the results.
- Scanner config for this evaluation: production sampling defaults
  (`sampling.fps: 1`, `sampling.region: top_right`, `max_height: 720`) in a
  dedicated config with its own `output_dir` (fresh corpus, production corpus
  untouched). `scan --reprocess` if rescanning.
- **Recall ceiling, stated up front:** 1 fps sampling, the top-right region
  crop, and the 720p downscale can each independently miss transient,
  crowded, or off-region killfeed rows. Any miss found here is therefore
  attributable to the *combined* pipeline, not to recognition alone.

## 4. Predicted set P — presence/retrieval form (chosen)

The pipeline emits text clusters, not player names, so P is defined as:

- For each name in G, run the production query path:
  `uv run tf2scan query --name <name>` against the dedicated corpus.
  P = the subset of G with **at least one promoted candidate** (production
  promotion: any score ≥ 0.95, or ≥ 0.82 scores at distinct times within a
  3 s gap — unchanged production behavior).
- Why presence/retrieval over discovery: it exercises exactly the production
  search path including matching thresholds and temporal consensus; it avoids
  inventing a name-shapedness heuristic (a second, unevaluated classifier);
  and it sidesteps the ambiguity that pipeline text outside the killfeed is
  not necessarily a false positive. A discovery form over
  `text_clusters.jsonl` is deferred; the export is still generated for
  qualitative review, but contributes no metric.
- Match verification / FP rule: a candidate is a **true match** if its matched
  cluster text equals the queried name after normalization (casefold, trim,
  collapse internal whitespace). Non-exact matches go on a short manual
  review list (representative crop + text checked by hand); manual decisions
  are recorded in the results. A **false candidate** is a promoted candidate
  that fails this rule; a queried name whose candidates are all false counts
  as FP per-name.

## 5. Metrics and reporting

- Per-name set comparison (unit = distinct name): |G|, |P|, TP, FN, FP,
  recall = TP/|G|, precision = TP/|P|, F1, plus absolute counts, the list of
  missed names (FN) and the list of false candidates (per candidate and
  per-name).
- Coverage: samples scanned vs window and superset duration, scan wall time
  and RTF from `--perf-output`.
- Uncertainty: one window and on the order of 10–20 names; counts are the
  primary result, ratios are reported but interpreted qualitatively. No
  interval estimates, and **no claim that any HLD gate (95 % occurrence
  recall, ≤ 0.5 false candidates/query-hour, …) is established**.
- R12 is complete on this demo-derived set. R13 still needs occurrence-level
  alignment of detections to kill timestamps and a little more demo coverage
  before the ship check; full-row transcripts/CER, threshold tuning, model
  comparisons (R15), and the manually labeled real-video set (post-MVP) remain
  deferred. `docs/todo.md` records the current scope.

## 6. What this design can and cannot detect

Can detect: names in G the production query path fails to retrieve
(pipeline-wide recall failures — detection, retention, clustering,
recognition, or matching), and false candidates among retrieved names.

Cannot detect (set comparison is blind to):

- **Wrong-time detections** — a name retrieved from outside its kill's
  visibility period still counts as found.
- **Duplicates and over/under-merging** — one cluster or five for the same
  notice makes no difference to the name set.
- **Names absent from the killfeed** — a player who connected but never
  killed/died is invisible to G by construction.
- **Text outside the killfeed** — a pipeline hit on such text is not
  evidence of a false positive; presence form does not count it either way.
- **Sampling/crop misses** as distinct causes — 1 fps, top-right crop, and
  downscale losses are confounded with pipeline losses (see recall ceiling).
- **Recognition quality under real-video compression** — a clean demo render
  is the *easiest* input the pipeline will ever face; numbers here are an
  upper bound relative to real VODs, and say nothing about YouTube-compressed
  sources.

## 7. Implementation steps (after approval)

1. Parse the demo with the installed `parse_demo`, select and record the
   window (criteria in §1); emit the per-kill table and G via a small script
   (`eval/r12_demo_validation/extract_ground_truth.py`, window length a
   parameter, default 180 s).
2. Render the superset window via SVR (manual, commands in §3); record
   `ffprobe` metadata (resolution, fps, duration, size).
3. Scan the rendered file with the dedicated config and `--perf-output`.
4. Run the query loop for every name in G; apply the verification rule
   (§4), manual review for non-exact matches; emit counts JSON, missed-name
   and false-candidate lists.
5. Append measurements to this doc; update the R12/R13 tickets in
   `docs/todo.md`.

## Results

Measured 2026-10-12 on Windows 10 (AMD64, 12 logical CPUs), PaddleOCR 3.7.0
models PP-OCRv6-small-det/-rec on CPU, production sampling defaults
(1 fps, `top_right`, 720p cap) in a dedicated fresh corpus.

### Window and ground truth

- Demo `input/test_9v9_demo.dem` (SourceTV, `pl_eruption_b16`, 1535.640 s,
  66.67 tick). Window **[1320 s, 1500 s)** = ticks [88000, 100000), inside
  round 2 active play (round ends at 1520.6 s; no pauses in the demo).
- Selected from a documented criteria scan over candidate windows: 46 kills,
  6/6 thirty-second sub-bins occupied, **0 world kills**, and **18 distinct
  killer/victim names — every player on the server** — so no
  display-uncertainty caveat was triggered.
- `|G| = 18`. Kill table and per-name roles:
  `output/r12-demo/ground_truth.json` (from
  `eval/r12_demo_validation/extract_ground_truth.py`).

### Video

- SVR render via `startmovie r12_window.mp4 timeout=200 profile=r12_eval`
  after `demo_gototick 87333` (superset [1310 s, 1510 s]).
  `lib/svr/data/profiles/r12_eval.ini`: 1080p60, libx264 CRF 15, no audio.
- `ffprobe`: 1920x1080, 60 fps, h264, exactly **200.000 s, 12,000 frames**,
  36.2 MB (`lib/svr/movies/r12_window.mp4`).
- Observation: this SourceTV playback draws **verbose killfeed text** —
  `koner killed [MOM$] Rizz HuzzHouse with scattergun.` — instead of the
  stock icon row (weapon-icon-font fallback in the GOTV HUD). Rows are
  therefore longer and more legible than a stock HUD killfeed, which makes
  this an **easier-than-production input**; the numbers below are an upper
  bound on real-VOD behavior.

### Scan (coverage)

| Measure | Value |
|---|---|
| Samples scanned | 200 of 200 expected (1 fps over 200.0 s — complete grid) |
| Crops / clusters | 2,925 crops, 1,158 text clusters |
| Scan wall time | 219.365 s (RTF 1.097); model setup 26.291 s excluded |
| Largest stage | recognition 134.472 s (detection 76.094 s, decoding 1.779 s) |
| Resolutions | source 1920x1080 → working 1280x720 → detector 640x360 |

Profile: `output/r12-demo/scan-profile.json`; corpus
`output/r12-demo/results.sqlite3`.

### Presence metrics (final, after one manual review)

- All 18 G names were queried through the production `query` path; **every
  name had ≥ 1 promoted candidate**: `|P| = 18`.
- 17 names verified by the normalized-exact rule with no manual review.
  One name, `[MOM$] Rizz HuzzHouse`, needed manual review because every
  matched cluster text was a full verbose row rather than the bare name;
  decided **true**: the name appears verbatim as the victim in
  `koner killed [MOM$] Rizz HuzzHouse with scattergun.` (matcher score
  1.000, 6 supporting frames, OCR confidence 0.98), with the expected
  `$`→`s` OCR confusion (`[MOMs]`) in other clusters of the same player.
- **|G| = 18, |P| = 18, TP = 18, FN = 0, FP = 0. Recall 1.0, precision 1.0,
  F1 1.0.** Missed names: none. False candidates (per name): none.
- Interpretation: on this one window, the pipeline retrieved every player
  who got a kill or died. With 18 names from one window of one clean-source
  demo, these ratios carry almost no statistical weight — the counts are
  the result. A single different window (crowded fights, stock icon HUD,
  real-video compression) could look different. **No HLD gate is established
  by this run.**

Artifacts: `output/r12-demo/presence_results.json` (per-name candidates,
  coverage, metrics), `output/r12-demo/manual_review.json`, evaluation
  script `eval/r12_demo_validation/evaluate_presence.py`.

### Post-hoc descriptive pass over detected text (not a deferred metric)

After the measured run, a one-off inspection of all 1,158 cluster texts was
made to answer "what did the pipeline detect that is not a G name?"
This is descriptive context only — the discovery-form P remains deferred.
Of 673 distinct texts, 166 contained a G name (normalized substring); the
other 507 contained none, and every one of those falls into four buckets:

1. Pure numbers (225 distinct texts): HUD health/ammo/damage/timer readouts.
2. A `FPS/CPU/GPU` performance overlay from the render (123 of 1,158
   clusters) — demo playback drew an FPS/CPU/GPU HUD readout that production
   VODs may not have.
3. Server console messages (`unknown particle system 'water_splash_02…'`,
   `This server is running config: rg1_HL_stopwatch`,
   `teams have been switched`, capture/defense notices).
4. OCR-garbled or truncated variants of G names themselves
   (`soolbs Norwood 4 Janitor`, `Starzip`, `sureeisoning`, `Jo3o`, …),
   including killfeed rows whose name was misread enough to fail the
   substring check.

So on this window the pipeline detected **no name that is not one of the 18
G names** (the 19th roster entry, the `SourceTV` bot, never appears). A
`…suicided.` row for `You Get That Thang Oiled Down?` sits at video 0–5 s —
inside the superset margin before the window, consistent with the window
having zero world kills. The overlay text (bucket 2) is a render-hygiene
observation: future renders could disable it so cluster statistics reflect
gameplay text only.

### Negative-query battery and positive-candidate audit (post-hoc extension)

The demo's `users` table records everyone who ever connected during
recording, so any name outside that roster is provably absent — fully labeled
negatives at zero labeling cost. A battery of 30 such queries was run in three
documented categories (`eval/r12_demo_validation/negative_queries.py`,
battery recorded in `output/r12-demo/negative_results.json`):

- **Realistic invented names (10): 0 queries returned any candidate.** A
  reviewer searching a plausible name that resembles nobody on the server
  gets a clean zero on this corpus.
- **Short names (10): 5 returned candidates, all by design.** The matcher
  scores a compacted-substring hit as 1.0, so `pen` hits rows of
  `sell me this pen` (63 candidates), `zip` hits `Starzip` variants (25),
  `nor` hits `Norwood` (38), `rizz` hits `Rizz HuzzHouse` (25), `jo3` hits
  garbled `Jo3o` rows (4). The five clean short names (`kaz`, `orb`, `vex`,
  `meep`, `tofu`) returned nothing.
- **Near-duplicates of real names (10): 7 returned candidates** — the
  "reviewer must squint" cases: absent `konner` matches every real `koner`
  row at 0.83 (23 candidates), `Chinney` matches `chimney`/`Chinmey` at 0.86
  (38), plus `chimneyy`, `sunpoisonlng`, `Don T Beakunt`, `Rizz HuzzHouse`,
  `500lbs Norwood 5 Janitor` (19–27 each). `eto1`, `Yorr`, `JoJo2` stayed
  below the 0.82 consensus threshold (appending a character to a 4-letter
  name scores 0.80).

Totals: **12 of 30 negative queries returned 326 false candidates**; 18
stayed clean. False candidates here cluster entirely around short-substring
and near-duplicate collisions with real players — the workload that matters
for a reviewer searching absent-but-plausible names.

The 18 **positive** queries were also audited per candidate (592 candidates
total). With a strict whole-word rule: **366 (62%) contain the queried name
verbatim; 226 (38%) are non-verbatim but still score ≥ 0.82 against it** —
mostly garbled/truncated variants of the *same* name (`soolbs Norwood 4
Janitor`, `[MOMs] Rizz HuzzHouse`); **0 score below 0.82** (no unrelated junk).
Two workload caveats: (a) candidate volume is high — 18–62 candidates per
name, one cluster per candidate, reflecting cluster multiplicity rather than
unique events (the deferred occurrence-level comparison would quantify
this); (b) for short names the ≥ 0.82 bucket includes coincidental
substrings inside *other* players' names — `eto` matches inside
`wifeTO paris`, so roughly half its 62 candidates are rows about a different
player. `negative_results.json` predates this stricter classification (its
audit buckets use the weaker substring rule); the verbatim/name-similar/weak
split above was computed with the word-boundary rule and the production
scorer.

These are counts on one 200 s corpus whose non-name text is unusually
abundant (numbers, FPS overlay, console lines), not per-hour rates, and no
HLD workload gate is established by them.

### What this run does not establish (still deferred)

- Occurrence-level correctness: no alignment of detections to kill/death
  timestamps, no duplicate-hit or over/under-merging measurement, no
  per-occurrence crops, full-row transcripts or CER.
- Discovery-form P and per-candidate false rates / reviewer workload
  (false candidates per query-hour) — presence form only was measured.
- Any second HUD/compression source, multi-VOD splits, threshold tuning and
  model comparisons (R15) — the manually labeled real-video set, including a real
  compressed-VOD window, is deferred post-MVP; R13 covers occurrence-level
  alignment on the demo-derived set.
