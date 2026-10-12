# 8. Manual real-video validation

Status: open, **deferred post-MVP**. This is the manually labeled evaluation the
MVP gate re-scope set aside in order to ship faster. The current accuracy
reference is the demo event stream ([demo-window validation](../docs/demo-window-validation.md),
[high-level design §6](../docs/high-level-design.md#6-evaluation-plan)); a clean
SourceTV render cannot speak for compressed real VODs. Reopen this ticket when
accuracy is doubted or a threshold/model change needs a real-input regression
check. It restores the occurrence-level detail, negative regions and uncertainty
reporting that the old R12/R13 carried before they were narrowed.

## Problem

Demo-derived occurrence recall is cheap and adequate for the MVP, but it is an
upper bound on easy input. It says nothing about YouTube compression, stock icon
HUDs, motion backgrounds, or the false-candidate behavior of real killfeeds, so
the MVP cannot claim its provisional gates (≥95% occurrence recall, ≤0.5 false
candidates/query-video-hour) from demo counts. There is also no real-video
regression set for later threshold or model changes. This ticket builds that set
deliberately and labels it by hand.

## Work

### Data selection

- Select three roughly 10-minute real VODs with different HUD layouts,
  resolutions, motion backgrounds and compression artifacts.
- Split by source video, never by frame, so near-identical neighbouring frames do
  not leak across evaluation groups.
- The set need not contain the user's actual name: label the visible names and
  treat each as a temporary target.

### Occurrence labeling

- The unit is a **visible username occurrence**, not a frame; adjacent frames
  showing the same notice are one occurrence.
- For each occurrence record the text polygon, exact visible transcription,
  source video and visible time span, with a stable occurrence ID spanning its
  adjacent frames.
- Include shifted/evicted notices, dense multi-kill activity, and lookalike names
  (`HumanWorm` / `HumanW0rm` / `HurnanWorm`).
- Label enough sampled negative regions to measure false detections and false
  candidate hits, including non-name HUD text near the killfeed. Mark unlabelled
  regions as unknown, not negative.
- Attach `source_duration_s` only to an exhaustively labeled window; never attach
  a full video duration to sparse positives, which would corrupt per-hour rates.

### Manifest and harness

- Use the JSONL manifest in the [HLD §6.2 example](../docs/high-level-design.md#62-manual-annotation-set-deferred-post-mvp)
  (`image`, `source_id`, `occurrence_id`, `visible_names`, `text_polygons`,
  `scenario_tags`, `hud`, `resolution`, `compression`, optional exact `text`).
  Version it and validate frame timestamps, dimensions, polygons, source
  grouping, exact text when CER is requested, and window duration.
- Reject source leakage across splits and inconsistent source durations; treat
  missing duration coverage as null rates, never fabricated zeros.
- Keep a crop-only mode for controlled model comparisons, but add frame/sequence
  and video-window modes that exercise the production detector, retention,
  tracker and matcher. Do not let the harness group predictions by ground-truth
  occurrence ID.

### Metrics

- **Detection:** region recall, under-20-pixel name recall, false boxes/frame,
  polygon coverage.
- **Recognition:** normalized exact match, CER (needs a full-row transcript, not
  names alone), target-alias recall.
- **Matching:** precision/recall at each fuzzy threshold and positive/negative
  score distributions, including alias-length groups.
- **End to end:** occurrence and video recall against labelled occurrences, with
  unmatched predicted hits and duplicate predicted events counted rather than
  hidden.
- **Reviewer workload:** candidates and false candidates per query-video-hour,
  with source durations deduplicated across frames/regions.
- **Breakdowns:** resolution, HUD, compression, text height, alias length,
  crowding/scenario tags.
- **Uncertainty:** Wilson intervals for proportions; source-group bootstrap
  (grouped by video) for corpus-level rates, with a reproducible seed/resample
  count. Do not claim a gate is established from a few hundred labeled
  occurrences.

Existing tooling to reuse and generalize: `eval/r12_demo_validation/evaluate_presence.py`
(presence/retrieval comparison) and `negative_queries.py` (negative battery and
candidate audit). `extract_ground_truth.py` is demo-specific and stays with the
demo path.

## Acceptance

- The manifest validates, splits are source-separated, and the three VODs run end
  to end through the same pipeline as scanning.
- Reports include detection, recognition, matching, end-to-end, workload and
  performance metrics with coverage and uncertainty disclosed.
- Deterministic checks cover known synthetic misses, duplicate events, false
  positives, zero denominators and uneven source durations; a frame-based check
  catches a detector miss that a crop-only check cannot.
- Results are compared to the provisional gates and reported as measured
  estimates with intervals, not as confirmation that a gate is met.
