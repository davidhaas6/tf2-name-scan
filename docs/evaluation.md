# Building and measuring the evaluation set

Current accuracy evidence is demo-derived: the demo event stream supplies
occurrence truth at no annotation cost, and R12's first slice — a presence
(name-set) comparison on one clean SourceTV window plus a negative-query
battery — is recorded in [demo-window validation](demo-window-validation.md).
R13 adds occurrence-level recall and the ship check. The manually labeled,
real-video manifest and crop harness described below are deferred until after
the MVP ships ([ticket 8](../tickets/08-manual-real-video-validation.md)); they
remain the accuracy evaluation for real compressed VODs.

For a fresh-corpus production scan baseline with retained timings and optional
local-versus-stream comparison, see [scan benchmark](scan-benchmark.md).
That small wrapper measures throughput and sample coverage; it does not replace
the labelled accuracy evaluation below.

The demo parser and SVR renderer are now in the repo for the demo-derived path;
this page's manifest and crop harness instead target the deferred manual
real-video set, using manually verified real-video crops. The old
`killfeed-search/test-data` directory contains useful candidate images and video,
but its OCR output is not verified ground truth. No labels are inferred
automatically from those filenames.

Each JSONL record names an image relative to its manifest:

```json
{"image":"rows/example.png","source_id":"demo_001","visible_names":["PlayerOne","HumanWorm"],"text":"PlayerOne HumanWorm","occurrence_id":"death_1234","timestamp_s":12,"split":"test","hud":"default","resolution":"1280x720","compression":"youtube_like","crowded":false,"source_duration_s":60}
```

Required fields are `image`, `source_id`, `visible_names`. Optional `text` is an
exact full-row transcript for exact-match/CER measurement; names alone cannot
provide a valid full-row CER. Use the same `occurrence_id` for adjacent crops of
the same notice and distinct timestamps for temporal consensus. Without an ID,
each image is treated as one occurrence. Keep every source in exactly one split;
the harness rejects source leakage between splits.

Each visible name becomes a temporary query against every crop, including negative
rows. Reports include threshold precision/recall, occurrence and video recall,
per-pair scores, and breakdowns by resolution, HUD, compression, crowding and alias
length. Include negative-only crops with `visible_names: []`.

`source_duration_s` must describe an **exhaustively labeled evaluation window**.
Never attach a full video's duration to a handful of sparse positive samples:
that would make false-candidate rates misleading. Rates are measured per query
video-hour, since the harness runs many temporary usernames. Missing duration
coverage produces null rates, not fabricated zeros.

Run separate T/S/B configs on the same manifest, batch size, and preprocessing;
also compare RGB, grayscale and contrast settings. Save each report separately.
Reports measure crop throughput, mean batch latency and Python heap peak (not
native tensor RAM or VRAM). Scan durations/download sizes in `videos` support
operational measurements. Measure process RAM/VRAM and decoding throughput on the
target machine separately; the crop benchmark does not certify those metrics.

The crop harness evaluates recognition/matching on labeled occurrences. Full-video
audits remain necessary to quantify row-detection and clustering misses, download
failure rates and final product gates. Begin with 500–1,000 unique demo crops,
then 100–200 verified real YouTube occurrences before tuning production thresholds.
