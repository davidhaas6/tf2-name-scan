# Building and measuring the evaluation set

Use the HLD's demo-parser/SourceDemoRender process for demo-derived samples and
manually verified real-video crops. The old `killfeed-search/test-data` directory
contains useful candidate images and video, but its OCR output is not verified
ground truth. No labels are inferred automatically from those filenames.

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
