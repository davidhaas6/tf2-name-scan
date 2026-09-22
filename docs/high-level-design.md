# TF2 YouTube Username Finder - MVP Design

Status: implementation handoff - streaming MVP with adaptive text detection  
Primary MVP goal: find likely appearances of one player username (plus aliases) in TF2 gameplay videos and produce timestamps and evidence crops for human review.  
Long-term direction: scan a large video corpus once and let people search it for their own usernames.

## 1. Product definition

Given:

- a curated list of YouTube channels, playlists, or video URLs;
- a date range;
- one target username and optional historical aliases;

the program streams suitable video-only inputs in bounded chunks, samples frames, detects text regions across the frame, recognizes those regions with a small OCR model, matches the results against the target aliases, clusters repeated observations, and produces a reviewable report.

Success is not perfect killfeed transcription. Success is high recall for the target name with few enough candidates that a person can quickly verify them.

The scanner must remain **target-independent** even though the MVP exposes a single-name search. OCR results are corpus data; matching a configured username is a downstream query. Useful nonmatching rows must not be discarded, because retaining them makes it possible to add new username searches without rescanning every video.

## 2. MVP scope

### Included

- Known channels, playlists, and individual video URLs
- Date/title/duration filtering
- Video-only streaming inputs at up to 720p, processed in resumable chunks
- Full-frame sampling at a configurable rate, initially 1 frame/second
- Fast, HUD-independent text detection with PP-OCRv6-small-det
- Batched recognition with PP-OCRv6-small-rec
- Target-independent retention and deduplication of recognized rows
- Exact and fuzzy alias matching
- Temporal clustering and consensus
- SQLite state so jobs can resume
- Evidence crops and a static HTML/JSON report

### Explicitly deferred

- Crawling all of YouTube
- Geographic filtering
- Classification of detected text as killfeed, scoreboard, chat, or spectator UI
- OCR-specific VLM inference on every frame
- Model fine-tuning
- A Flask/FastAPI service or multi-user UI
- A public username-search API and dedicated search engine
- Distributed processing

The MVP should be a local, resumable CLI. A server adds little until the scanner proves useful.

## 3. Core design

```mermaid
flowchart TD
    A["URLs + aliases"] --> B["Index with yt-dlp"]
    B --> C["Stream 720p chunks"]
    C --> D["FFmpeg sample at 1 fps"]
    D --> E["PP-OCRv6-small-det text detection"]
    E --> F["Rectify and pad text crops"]
    F --> G["Batch PP-OCRv6-small recognition"]
    G --> H["Temporal text clusters + corpus"]
    H --> I["Alias query + report"]
```

Conceptually, ingestion and search are separate:

```text
ingestion: video -> detect text -> recognize crops -> text clusters -> corpus
MVP query: corpus -> configured alias matcher -> candidate hits
later:     corpus -> search index/API -> many users and usernames
```

The expensive work - acquisition, decode, crop, and OCR - should happen once per video. Changing or adding a username should rerun matching only.

### 3.1 Video discovery and streaming acquisition

Use `yt-dlp` for metadata extraction and for opening a video-only stream. For a curated-channel MVP, this avoids a YouTube Data API key and another integration.

Indexing should:

1. Expand each channel or playlist into video records.
2. Persist video ID, URL, channel, title, upload date, duration, and available resolution.
3. Apply configured date, title, and duration filters.
4. Mark records as `pending`, `scanning`, `scanned`, `failed`, or `skipped`.

Do not make a complete 720p video file the normal input. Process fixed time chunks (start with 5-10 minutes) by piping a video-only `yt-dlp` stream into FFmpeg and then into the scanner. A conceptual single-chunk invocation is:

```bash
yt-dlp --quiet --no-warnings -f "bv*[height<=720]" -o - VIDEO_URL \
  | ffmpeg -i pipe:0 -vf fps=1 -f rawvideo -pix_fmt rgb24 -
```

The scanner should consume frames at the speed of the download or the OCR bottleneck and should not write the video or every sampled frame to disk. This removes the large temporary-video requirement, but it does not defeat YouTube throttling: keep one active stream per host, avoid speculative parallel downloads, apply bounded backoff, and record retry/failure reasons.

Treat each chunk as a resumable `scan_chunk` child of one logical `video_scan`. Persist its start and end timestamps and the last processed frame timestamp. If a process fails, restart only the current chunk using a section request such as `yt-dlp --download-sections "*START-END"`; this is a range re-request (often aligned to a nearby keyframe), not a promise of exact byte-level resume. The scanner must tolerate a small overlap at chunk boundaries, deduplicate observations by video timestamp and geometry, and merge compatible clusters across a chunk boundary after both sides are complete.

Keep an optional short-lived disk cache only for debugging or failed chunks. A download archive is still useful for indexing, but it is not a substitute for scan state. Once a chunk is complete, its stream can be discarded while its OCR lineage and evidence remain in SQLite.

### 3.2 Frame sampling

Use FFmpeg as a subprocess. Sample at 1 fps by default. The decoder should pipe frames to the scanner instead of writing every sampled frame to disk. The chunk start time must be added to each frame timestamp before it is persisted.

One frame/second is intentionally conservative: a killfeed notice normally persists long enough to appear in several samples. Make the rate configurable so crowded clips can be rescanned at 2 fps.

Do not build change detection in V1. A transparent HUD sits over a moving game background, which makes naive frame differencing less helpful than it appears. First measure OCR cost. Add a text-aware change gate only if OCR is actually the bottleneck.

### 3.3 Text detection and crop extraction

PP-OCRv6-small-rec recognizes cropped text but cannot locate it. Use **PP-OCRv6-small-det** to locate text across each sampled frame. Do not attempt to identify the killfeed or classify HUD regions in the MVP; recognizing all detected text also finds useful appearances in scoreboards, chat, killcams, and spectator UI.

PP-OCRv6-small-det is the initial default because it provides a strong accuracy/size tradeoff: PaddleOCR reports a 9.6 MB model and 84.1 detection Hmean on its internal multi-scenario benchmark. The tiny variant is only 1.9 MB but reports 80.6 Hmean; benchmark it as the speed-oriented alternative. Do not compare these numbers directly with unrelated datasets.

Keep detection behind an adapter:

```python
class TextDetector(Protocol):
    def detect(self, frames: list[Image.Image]) -> list[list[Detection]]: ...

@dataclass
class Detection:
    polygon: list[tuple[float, float]]
    confidence: float
```

For every sampled frame:

1. Run PP-OCRv6-small-det over the frame.
2. Retain the polygon and detector confidence for every accepted region.
3. Perspective-rectify each quadrilateral into a horizontal crop.
4. Add a small configurable margin so character strokes are not clipped.
5. Reject implausibly tiny or malformed boxes.
6. Batch the remaining crops through PP-OCRv6-small-rec.

For 720p input, benchmark native resolution against `limit_type="min", limit_side_len=960`; upscaling may improve small HUD-text recall at additional compute cost. Start with detector defaults, then sweep the box-confidence threshold on the evaluation set. Favor recall slightly over precision because recognition and username matching provide downstream filters.

Use the Paddle runtime for both PP-OCRv6 models in the V1 vertical slice. This keeps the detector and recognizer in one framework and avoids a second CUDA context and tensor-conversion boundary. ONNX Runtime GPU remains the intended production boundary after correctness and evaluation are established; export only after the harness can catch regressions.

Store representative crops and polygons for evidence. Do not persist every sampled frame.

### 3.4 Useful-text retention policy

"Retain all useful recognized text" means retaining every detection that passes a bounded, deterministic filter - not every raw detector proposal.

For each sampled frame:

1. Reject invalid polygons and crops shorter than a configurable minimum source-text height; start with 6 pixels.
2. Deduplicate substantially overlapping proposals before recognition. If polygon IoU is at least 0.85 or one proposal is at least 90% contained by another, keep the higher-confidence proposal.
3. Sort remaining proposals by detector confidence and recognize at most 64 crops per frame by default. Make the cap configurable and record how many proposals were dropped.
4. After recognition, discard empty output, output below a configurable low confidence floor, and strings containing fewer than two Unicode letters or numbers.
5. Assign a coarse normalized screen-region tag from a 3x3 grid (`top_left` through `bottom_right`) and retain normalized polygon dimensions. This is metadata, not semantic HUD classification.
6. Cluster near-identical observations across time. Repeated static HUD text should become one long-lived cluster rather than one searchable corpus entry per sampled frame.

Persist accepted observation metadata while scanning, but retain crop files only for representative observations, borderline matches, and confirmed hits after clusters are finalized. Never silently truncate: per-frame counters must record raw detections, geometric duplicates, recognized crops, accepted observations, and cap-dropped proposals.

These defaults are safety bounds for the vertical slice, not accuracy claims. The evaluation harness should tune minimum height, confidence floor, overlap threshold, and per-frame cap.

### 3.5 OCR model

Use **PP-OCRv6-small-rec** as the V1 recognizer alongside PP-OCRv6-small-det. Keeping the initial vertical slice in the Paddle ecosystem reduces integration time, memory pressure, and GPU context contention. Keep recognition behind a narrow adapter so alternative checkpoints can be benchmarked without changing the pipeline:

```python
class Recognizer(Protocol):
    def recognize(self, crops: list[Image.Image]) -> list[Recognition]: ...

@dataclass
class Recognition:
    text: str
    confidence: float | None
```

Benchmark the official OpenOCR SVTRv2 checkpoints as a later comparison. Their published latency was measured on a GTX 1080 Ti with PyTorch dynamic graph mode, so treat it only as a relative reference and measure on the actual machine.

| Model              | Parameters | Published latency | MVP role                                    |
| ------------------ | ---------: | ----------------: | ------------------------------------------- |
| PP-OCRv6-small-rec |          - |                 - | V1 default; same Paddle runtime as detector |
| SVTRv2-T           |      5.13M |            5.0 ms | Fastest cross-framework comparison          |
| SVTRv2-S           |     11.25M |            5.3 ms | Accuracy/recall comparison                  |
| SVTRv2-B           |     19.76M |            7.0 ms | Higher-capacity comparison                  |

Only absorb the OpenOCR/SVTRv2 dependency if PP-OCRv6-small-rec misses the target-name recall gate or has unacceptable throughput on the project benchmark. Move both detector and recognizer to ONNX Runtime only after the evaluation harness exists; otherwise performance work can hide detection or recognition regressions.

### 3.6 Username matching (MVP query layer)

The matcher consumes stored recognized text regions and should operate on aliases without requiring perfect transcription. It must not control which OCR results are retained.

For every OCR result, retain the raw text and create two normalized forms:

- `conservative`: Unicode NFKC, case-folded, repeated whitespace collapsed;
- `compact`: conservative form with whitespace and common separators removed.

Compare every alias against both forms. Use substring matching first, followed by normalized Levenshtein similarity over sliding windows near the alias length. RapidFuzz is suitable; convert its score to a consistent 0-1 range. A detected line may contain killer, assister, and victim names, so search within the recognized string rather than comparing only the complete string.

Initial decision rules, to be tuned on the evaluation set:

- accept one observation with similarity at least 0.95;
- or accept two observations within 3 seconds with similarity at least 0.82;
- aliases of four characters or fewer require exact normalized matching or a stricter, separately tuned rule;
- retain borderline results in SQLite even when they are not promoted to hits.

Avoid hard-coded character substitutions such as `0 -> o` during normalization. They may improve recall but can sharply increase false positives. If useful, treat them as scored edit alternatives.

### 3.7 Temporal clustering

A name visible for several seconds will produce repeated OCR observations. Start with a **3-second missing-observation gap**: close a cluster when no compatible observation supports it for more than 3 seconds. Keep the gap configurable because sampling rate, decoder drops, and source frame cadence can vary.

Cluster observations when they:

- come from the same video and nearby screen region, using polygon overlap or normalized center distance;
- have compatible recognized text; and
- have a plausible geometric continuation.

For killfeed-like rows, track the polygon center and text height over time. Estimate a local line height from nearby detections and allow a continuation when a box moves by an approximately integer number of line heights as newer notices push older notices down. If a similarly named box reappears at the feed's insertion position after a gap or an incompatible jump, start a new cluster rather than merging it with the older event. Treat this vertical-shift rule as a scored motion feature, not a HUD-specific hard-coded coordinate assumption; the 3x3 screen-region tag remains only supporting metadata.

Alias agreement may strengthen a cluster but must not be required to create it. Record whether a cluster was closed by a time gap, incompatible text, or incompatible motion so false merges can be diagnosed.

One cluster becomes one candidate hit. Record:

- start and end timestamp;
- best OCR text and score;
- number of supporting frames;
- distinct OCR strings seen across the cluster;
- motion/continuity summary and close reason;
- best full-frame and detected-text-crop evidence;
- direct YouTube timestamp URL.

This both reduces reviewer workload and makes noisy per-frame OCR useful.

Clustering has two outputs:

1. A target-independent `text_cluster`, created for every useful recognized region.
2. An optional target-specific `hit`, created when that cluster matches the current alias query.

Store text clusters even when they do not match the MVP username. Record detector, recognizer, preprocessing, and normalization versions so the corpus can later be rematched, migrated, or selectively reprocessed.

## 4. Storage and outputs

SQLite is sufficient for the MVP. Use migrations from the beginning and keep target-independent corpus records separate from target-specific search results.

```text
videos
  id, source_url, channel_id, title, upload_date, duration_s
  local_cache_path_nullable, created_at

scan_runs
  id, started_at, completed_at, status, config_hash
  detector_name, detector_version, detector_weights_hash
  recognizer_name, recognizer_version, recognizer_weights_hash
  preprocessing_version, normalization_version, geometry_encoding_version
  software_versions_json, config_json

video_scans
  id, scan_run_id, video_id, status, error
  sample_fps, input_width, input_height
  started_at, completed_at

scan_chunks
  id, video_scan_id, sequence_no, status, error
  chunk_start_s, chunk_end_s, last_processed_timestamp_s
  download_mode, started_at, completed_at

sampled_frames
  id, scan_chunk_id, timestamp_s
  raw_detection_count, duplicate_detection_count
  recognition_count, accepted_observation_count, cap_dropped_count
  full_frame_path_nullable

observations
  id, sampled_frame_id, polygon_blob
  screen_region, normalized_width, normalized_height
  detector_confidence, raw_text, normalized_text, ocr_confidence
  crop_path_nullable

text_clusters
  id, video_scan_id, start_s, end_s, representative_observation_id
  representative_polygon_blob, screen_region
  canonical_text, normalized_text, best_confidence
  support_count, motion_summary_json, close_reason

cluster_observations
  cluster_id, observation_id, support_score

hits
  id, text_cluster_id, query_id, matched_alias
  best_text, best_score, support_count
  review_status, reviewer_note, created_at

queries
  id, target_name, aliases_json, matcher_version, created_at
```

`scan_runs` owns every model, preprocessing, normalization, dependency, and configuration version needed to explain or reproduce a scan. `video_scans` links a video to one run. `cluster_observations` makes cluster provenance explicit instead of relying on timestamps or spatial inference.

Store polygons as a compact versioned binary encoding (packed little-endian float32 x/y pairs in source-pixel coordinates) in `polygon_blob`; export JSON only in debug or JSONL artifacts. The encoding version belongs to the scan run so a future geometry migration can decode old rows without guessing. If the deployed SQLite build provides a tested JSONB implementation, it may be used instead, but the application must not depend on a nonportable extension for the MVP.

`sampled_frames.full_frame_path_nullable` and `observations.crop_path_nullable` separate the two evidence types. Most paths should be null after compaction; `text_clusters.representative_observation_id` identifies the crop used as cluster evidence, and its sampled frame identifies the optional full-frame evidence.

`observations` and `text_clusters` form the reusable corpus. `queries` and `hits` are disposable derived data and can be regenerated when aliases or matching logic change. Reprocessing a video creates a new `video_scan` under a new `scan_run`; it does not overwrite the previous lineage.

Generate:

- `results.sqlite3` - authoritative scan state and evidence metadata;
- `text_clusters.jsonl` - optional export of target-independent text clusters;
- `hits.jsonl` - portable machine-readable results;
- `report/index.html` - sortable candidates with crop, video title, score, timestamp link, and confirm/reject state;
- `report/assets/` - only evidence images, not every sampled frame.

The first report may be static. Confirm/reject can export a small JSON file or be handled by a separate CLI command; a server is unnecessary for V1.

Do not retain every sampled frame. Retain compact text-cluster records and representative evidence crops. This keeps the corpus useful for future search without allowing storage to grow at the raw sampling rate.

### 4.1 SQLite write path

Enable WAL during database initialization and use batched transactions:

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
```

Accumulate frame, observation, and cluster writes in memory and commit every 10-30 seconds of video time, or sooner when a bounded row/byte limit is reached. A crash may replay the current batch, so writes must be idempotent by `scan_chunk_id` and timestamp/geometry keys. Keep WAL checkpointing and maximum WAL size configurable for long scans.

Observation growth is linear in accepted detections and sampled frames, not exponential. The main controls are the per-frame crop cap, geometric deduplication, cluster compaction, and evidence retention policy. Add indexes on `video_scans(video_id, status)`, `scan_chunks(video_scan_id, status)`, `sampled_frames(scan_chunk_id, timestamp_s)`, `observations(sampled_frame_id)`, `text_clusters(video_scan_id, normalized_text)`, and `cluster_observations(cluster_id, observation_id)` so writes and future corpus exports remain predictable.

## 5. Suggested CLI and repository shape

```text
tf2-name-scan/
  pyproject.toml
  config.example.yaml
  src/tf2scan/
    cli.py
    config.py
    indexing.py
    download.py
    frames.py
    detection.py
    crops.py
    recognize.py
    matching.py
    clustering.py
    storage.py
    report.py
  tests/
  eval/
    manifest.jsonl
    run_benchmark.py
```

Suggested commands:

```bash
tf2scan index config.yaml
tf2scan scan --pending --chunk-seconds 600
tf2scan scan --video VIDEO_ID --fps 2
tf2scan detect FRAME_PATH --visualize
tf2scan report
tf2scan benchmark eval/manifest.jsonl
```

All stages must be idempotent. A failure in download, decode, or OCR should update that video's state and allow the rest of the queue to continue.

## 6. Evaluation plan

### 6.1 What the dataset should represent

The evaluation unit is a **visible username occurrence**, not an individual frame. Adjacent frames showing the same killfeed notice are one occurrence.

For the MVP, build the real-video set first. A small manual set is cheaper than implementing demo parsing, SourceDemoRender automation, and HUD-specific alignment before the scanner has been benchmarked. Add a demo-rendered set later when controlled variation or fine-tuning data is worth that engineering cost.

Split by source video (or source demo), never by frame. Otherwise nearly identical neighboring frames leak into both train and test sets and inflate accuracy.

The real-video set does not need to contain the user's actual name. Label the visible names and treat each one as a temporary target during evaluation. This tests the same retrieval behavior without first having to find known appearances of the real target.

### 6.2 MVP manual annotation set

1. Select three roughly 10-minute VODs with different HUD layouts, resolutions, motion backgrounds, and compression artifacts.
2. Sample them with the same frame policy as the scanner, then manually label about 100 visible killfeed username occurrences. For each occurrence, record the text polygon, exact visible transcription, source video, and visible time span.
3. Label enough sampled negative regions to measure false detections and false candidate hits, including non-name HUD text near the killfeed.
4. Tag cases where killfeed notices shift down as older notices expire, where several kills are close together, and where the same lobby contains lookalike names such as `HumanWorm`, `HumanW0rm`, and `HurnanWorm`.
5. Keep occurrence IDs spanning adjacent frames so metrics are computed per visible occurrence, not per frame. Split the three VODs into disjoint evaluation groups; do not split neighboring frames across groups.

Use a small JSONL manifest so every model sees exactly the same data:

```json
{
  "image": "frames/vod_001_123456.png",
  "source_id": "vod_001",
  "occurrence_id": "vod_001_occ_014",
  "visible_names": ["PlayerOne", "HumanWorm"],
  "text_polygons": [
    [
      [812, 24],
      [1012, 24],
      [1012, 47],
      [812, 47]
    ]
  ],
  "scenario_tags": ["notice_shift", "lookalike_names"],
  "hud": "custom_a",
  "resolution": "1280x720",
  "compression": "youtube_like"
}
```

This manual set is sufficient to choose the V1 recognizer and tune thresholds. It is not by itself enough to establish product gates with high confidence; report grouped confidence intervals and expand the set when a gate is close.

### 6.3 Later: generating the demo-rendered set

1. Collect TF2 POV/STV `.dem` files containing varied player names and combat density.
2. Parse them with the current `demostf/parser` project (the old GitHub repository points to its maintained Codeberg home).
3. Export death events with tick, killer, assister when present, victim, and weapon/event type.
4. Render short windows around selected ticks with SourceDemoRender using varied HUDs and resolutions where practical.
5. Sample rendered frames and run the same detector/crop/recognizer pipeline used by the scanner.
6. Label visible text polygons and player-name strings. Parser output supplies expected names, but automatically generated labels must still be spot-checked because assists, world kills, overlapping notices, name changes, and notice eviction can alter what is actually visible.
7. Deliberately include sequences where new killfeed notices move older notices, older notices expire, several kills occur close together, and the same lobby contains visually similar names such as `HumanWorm`, `HumanW0rm`, and `HurnanWorm`.
8. Create additional 720p compressed versions with FFmpeg to approximate YouTube degradation.
9. Deduplicate neighboring frames and split the dataset by demo.

A useful later controlled benchmark is roughly 500-1,000 labeled text regions, with at least 100 positive name occurrences and a much larger negative set. Keep parser-derived labels spot-checked; exact event metadata does not guarantee that a notice is visible in a rendered frame.

### 6.4 Models to benchmark

Benchmark PP-OCRv6-small-det and PP-OCRv6-tiny-det on the same frames at native 720p and 960 minimum-side processing. Use small as the starting default and switch to tiny only if it provides a material throughput gain without meaningful target-text recall loss.

Benchmark **PP-OCRv6-small-rec** as the V1 default on identical detected crops and batching configuration. Also benchmark the pretrained English SVTRv2-T, S, and B checkpoints as comparison arms. Only move to SVTRv2 if PP-OCRv6-small-rec misses the target-name recall gate or has unacceptable throughput; the benchmark harness should make that decision measurable. Generic OCR benchmark scores are not the decision criterion.

Track:

| Layer         | Metrics                                                                                               |
| ------------- | ----------------------------------------------------------------------------------------------------- |
| Detection     | visible-name region recall, recall for text under 20 pixels tall, false boxes/frame, polygon coverage |
| Recognition   | normalized exact-match accuracy, character error rate, target-alias recall                            |
| Matching      | precision/recall at each fuzzy threshold, score distributions for positives and negatives             |
| End to end    | occurrence recall, video-level recall, false candidate hits per video-hour                            |
| Reviewer cost | candidates requiring review per video-hour                                                            |
| Performance   | crops/second, decoded video-hours/wall-hour, batch latency, peak VRAM/RAM                             |
| Operations    | download bytes/video-hour, temporary disk use, failure/retry rate                                     |

Report results separately by source resolution, HUD, compression severity, text height, alias length, and crowded versus quiet scenes.

Report 95% confidence intervals, not only point estimates. Use Wilson intervals for proportions such as detection and occurrence recall, and a source-group bootstrap (grouped by video or demo) for false candidates per video-hour and other corpus-level rates. With only a few hundred labeled occurrences, do not treat a point estimate above 95% recall or below 0.5 false candidates/hour as confirmation that the corresponding gate has been met.

Provisional MVP gates:

- at least 95% recall on visible target-name occurrences;
- at least 97% detection recall for labeled visible-name regions before recognition;
- no more than 0.5 false candidates per scanned video-hour;
- at least 10x real-time scanning on the target machine, excluding download time;
- interrupted jobs resume without rescanning completed videos.

These are product targets, not assumptions. Early benchmark reports should show their uncertainty and guide dataset expansion; they should not claim the gates are statistically established until the intervals and source coverage support that conclusion.

## 7. Later: uncertain-crop recovery model

Only after benchmarking the small recognizer, add an OCR-specific VLM for ambiguous clusters - for example, HunyuanOCR. It is relevant because its published evaluation explicitly includes game, screen, and video text, and it can return text with coordinates.

Send only uncertain evidence to this model, such as:

- PP-OCRv6-small-rec (or a later promoted recognizer) score near the promotion threshold;
- conflicting strings across adjacent frames;
- a strong fuzzy match supported by only one frame;
- blank SVTRv2 output from a visually nonblank row.

Use the larger model's result as another scored observation, not unquestioned ground truth. Cache every result and measure whether it improves occurrence recall or reduces review burden enough to justify its latency and memory cost. It is not part of the MVP dependency graph.

## 8. Later: TF2-specific fine-tuning

Fine-tune recognition only if the pretrained variants miss the recall target on correctly detected and cropped text. If visible names are never proposed as boxes, fine-tune or replace the detector instead; recognition training cannot repair detection misses.

Fine-tune the recognizer selected by the benchmark. For the V1 PP-OCRv6-small-rec path, use the corresponding PaddleOCR training/export flow; if SVTRv2 is promoted later, use the OpenOCR recognition fine-tuning path with its pretrained weights. Train on isolated detected-text crops with exact visible text labels. A practical corpus would combine:

- verified demo-rendered rows;
- manually corrected real YouTube rows;
- synthetic TF2 killfeed rows using representative fonts, team colors, icons, backgrounds, scaling, blur, and video compression.

Keep an untouched test split grouped by channel/HUD or demo. Optimize for target-name recall and false candidates per video-hour, not only generic word accuracy. Start by manually auditing 1,000-2,000 automatically produced labels; do not fine-tune directly on unverified parser/render alignment.

## 9. Eventual corpus search

The MVP does not need a search service, but its stored text clusters should support one without re-running detection or recognition.

At larger scale:

1. Export finalized `text_clusters` from SQLite into PostgreSQL or a dedicated search engine.
2. Index both conservative and compact normalized text using character n-grams/trigrams. Player names contain unusual capitalization, punctuation, digits, and Unicode, so ordinary word tokenization is insufficient.
3. Search exact normalized substrings first, then fuzzy candidates within a length-aware edit-distance threshold.
4. Group results by source video and temporal cluster before returning them.
5. Show the original OCR text, evidence crop, confidence, channel/video metadata, and timestamp link so users can verify a result.

A future API can remain small:

```text
GET /search?username=HumanWorm
  -> exact hits
  -> likely fuzzy hits
  -> video, timestamp, crop, OCR text, and confidence
```

The search system should preserve short-name safeguards and rate limits because fuzzy matching a short string can generate many unrelated results. If later processing can reliably separate individual player names within each row, add a derived `name_candidates` index; do not make that extraction a prerequisite for the MVP.

Important corpus invariants:

- A video is identified independently of any query or user.
- Each `video_scan` belongs to exactly one immutable `scan_run`; reprocessing creates a new lineage rather than overwriting old observations.
- Target-independent text clusters are durable; target-specific hits are rebuildable.
- Detector, recognizer, weights, preprocessing, normalization, dependencies, and effective configuration are captured by the owning scan run.
- Every cluster is traceable to its supporting observations through `cluster_observations`.
- Evidence retains source provenance and timestamp information.
- Deleting a source video removes its clusters, index entries, and evidence together.

## 10. Implementation order

1. **Vertical slice:** one local or remote chunk, streamed through yt-dlp and FFmpeg, PP-OCRv6-small detection and recognition, geometric deduplication and crop caps, crop rectification, explicit useful-text filtering, alias matching, and console timestamps.
2. **Lineage, evidence, and clustering:** scan runs, chunked video scans, observation-to-cluster links, separate nullable frame/crop evidence, versioned geometry, confidence values, 3-second temporal clustering with motion continuity, target-independent text clusters, SQLite, and an HTML report.
3. **MVP evaluation:** manually annotate three short VODs, build the manifest and benchmark harness, measure PP-OCRv6-small-det/rec end to end, tune thresholds, and report grouped confidence intervals.
4. **Acquisition hardening:** yt-dlp indexing, fixed chunk ranges, stream retries/backoff, overlap deduplication, WAL/batched writes, and resume behavior.
5. **Model and runtime comparisons:** benchmark the tiny detector and SVTRv2 T/S/B recognition arms; adopt SVTRv2 only if the V1 Paddle recognizer misses the recall or throughput target; export to ONNX Runtime after correctness is established.
6. **Scale pass:** optional demo-rendered evaluation data, batch sizing, throughput tuning, deletion/retention policy, corpus export, and long-run reliability.

Do not implement the VLM fallback or fine-tuning until the evaluation report shows a specific failure mode they can address.

## 11. Deferred backlog

- Measure Unicode and character-set recall before expanding beyond the curated English-heavy MVP corpus.
- Review bandwidth costs, caching policy, YouTube terms, and takedown/deletion behavior before crawling at large scale or opening the corpus to many users.

## 12. Key references

- [OpenOCR repository](https://github.com/Topdu/OpenOCR)
- [SVTRv2 models, benchmarks, and commands](https://github.com/Topdu/OpenOCR/blob/main/docs/svtrv2.md)
- [OpenOCR recognition fine-tuning](https://github.com/Topdu/OpenOCR/blob/main/docs/finetune_rec.md)
- [PP-OCRv6 technical documentation](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/algorithm/PP-OCRv6/PP-OCRv6.en.md)
- [PaddleOCR text detection models and API](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/module_usage/text_detection.en.md)
- [FAST text detector](https://github.com/czczup/FAST)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [TF2 demo parser - current home](https://codeberg.org/demostf/parser)
- [Archived GitHub mirror of the demo parser](https://github.com/demostf/parser)
- [SourceDemoRender](https://github.com/crashfort/SourceDemoRender)
- [HunyuanOCR](https://github.com/Tencent-Hunyuan/HunyuanOCR)
