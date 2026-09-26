# Working local scanner: R04, R05 and R08 core

This page records validation at the local scanner milestone. Streaming and
recovery were implemented afterward; see [current progress](refinement-progress.md)
for the latest status and validation.

Validated on 2026-09-26 with Windows x64, Python 3.10.11, PaddleOCR 3.7.0,
PaddleX 3.7.2, PaddlePaddle 3.3.1 and NumPy 2.2.6. Both models ran on CPU,
with MKL-DNN disabled and no PyTorch backend. Model construction and prediction
use the separate PaddleOCR `TextDetection` and `TextRecognition` APIs.

## Real clip check

Source: the prototype's `test-data/One kill with every engineer weapon in 12 minutes.mp4`.
SHA-256: `2f28ebadb7f119bf0afe7e531b7cbe33db5788bbc6f69e6196770b50533d0f96`.
The source is 640x360. Two 12-second excerpts were encoded using FFmpeg/libx264,
starting at source times 30 and 240 seconds. Scans used one frame per second,
native detector resolution, default filtering/tracking, and full-frame evidence.
Stored timestamps are relative to each excerpt. `local-smoke.mp4` is the first
excerpt and has no visible killfeed; it exercises HUD text and negative regions.
`gameplay-smoke.mp4` is the second excerpt and has visible killfeed notices.

| Excerpt | Frames | Raw / recognized | Accepted observations | Clusters |
|---|---:|---:|---:|---:|
| 30–42 seconds | 12 | 197 / 197 | 149 | 49 |
| 240–252 seconds | 12 | 100 / 100 | 86 | 67 |

Neither excerpt triggered duplicate or cap drops. Deterministic tests exercise
those paths. Both CLI scans completed with zero failures.

Visually checked examples:

- `Healer: robot with human hair`: four observations between excerpt times 2–6 s;
  representative crop at 5 s shows the recognized text.
- `Seojer`: a killfeed name at excerpt times 8 and 9 s becomes one cluster with
  two support links. Its representative crop and full frame show the same name.
- Stable HUD text, such as `Waiting For Players`, has twelve supporting frames.

SQLite-only `query --name Seojer` returned one candidate. Actual crop files and
full frames are under the ignored smoke-result directories; clusters are exported
to their `rows.jsonl` files. The local clips, model
weights, database and evidence are ignored artifacts, not committed fixtures.
This is a functional smoke check, not a precision/recall benchmark. Some tiny
names are missed or misread, and changing numeric HUD text can share a cluster.

### Model artifacts

Official model directories were downloaded from Paddle's BOS mirror. The hash
is SHA-256 over sorted relative artifact names and their content hashes, covering
the inference graph, parameters and YAML (including recognition dictionary).

| Model | Artifact SHA-256 |
|---|---|
| PP-OCRv6_small_det | `b2e3688bdbc086d2a90b8b9bbc7a4d6ddd0c9d305eb9d0950fe24eebae46183b` |
| PP-OCRv6_small_rec | `072d8017521f30a880a620a789d9ae67ed016e66b646dda68654517de4f111c4` |

The factory checks the artifact's model name against the requested model. Loaded
artifact hashes, dependencies, device and preprocessing are saved in each scan's
immutable provenance. See [setup](usage.md) for installation and offline paths.

## Retention and clustering policy

Proposals pass geometry, source height and confidence checks, then convex-polygon
IoU/containment suppression. Confidence sorts descending; canonical geometry and
detection identity break ties. Suppression precedes the recognition cap. OCR
retains at least two Unicode letters/numbers, with finite confidence at or above
the floor. Missing confidence remains null and is retained. The five frame
counters include empty frames. Invalid/low-detector-confidence proposals can be
derived as raw minus duplicates, recognized and cap-dropped; OCR rejections are
recognized minus accepted.

Tracking uses normalized source centers and dimensions, normalized text similarity,
and the last measured velocity. A frame scores all candidate edges before taking
the best available pairs; geometry breaks equal-score ties independently of input
order. Each track receives at most one observation per frame. Center displacement
is bounded by `center_distance`; after the first motion sample, prediction error
is bounded by `max(0.005, motion_tolerance * text_height)`. Relative width/height
changes are also bounded by `motion_tolerance`. These are provisional defaults.

Gaps up to and including three seconds are compatible; longer gaps close tracks.
Nearby incompatible text/motion closes an unmatched track, while unrelated text
cannot close a track that received a compatible observation in that frame.
Close reasons distinguish time gap, text, motion, and end of scan. The database
stores support scores, latest motion state, and the highest-confidence observed
text/geometry/evidence as representative (earliest wins confidence ties).

## Boundaries and checks

At this milestone, 69 tests passed, including two real FFmpeg fixtures. Added tests cover filtering,
polygon intersection, deterministic ties, Unicode/confidence, counters, motion,
gap boundaries, same-frame association, representative evidence, Paddle order/
cardinality/coordinate contracts, model identity and local CLI model lifetime.

At this milestone, R06/R07 streaming, incremental transactions, retry
reconstruction and cross-chunk reconciliation were still separate work. Those
capabilities have since been implemented. R09–R11 query promotion, evidence
compaction and report refinements have also been implemented; see
[refinement progress](refinement-progress.md) for current validation. The legacy
HUD/OpenOCR path remains explicitly selectable.
