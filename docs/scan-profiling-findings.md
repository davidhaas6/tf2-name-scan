# Scan profiling findings (2026-09-27)

This note summarizes three local runs of the same 11.485833-second video (`input/uw1.mp4`, video ID `local-31b63f8a4f55987ce1f4`). The source is 3440×1440; sampling was 1 frame/second, with 11 frames processed. The scanner ran on Windows build 26200 with an AMD64 Family 25 Model 33 CPU (12 logical CPUs). Detection and recognition used the Paddle PP-OCRv6 small CPU models. Model setup time is reported separately from scan wall time. The full JSON reports are ignored local artifacts at `output/scan-profile.json`, `output/top-right-profile.json`, and `output/top-right-detailed-profile.json`.

## End-to-end comparison

| Measurement | Full frame | Top-right quarter | Top-right quarter, detailed profile |
|---|---:|---:|---:|
| Detector input | 1720×720 | 860×360 | 860×360 |
| Scan wall time | 106.848 s | 48.696 s | 44.493 s |
| Real-time factor (wall/video) | 9.303× | 4.240× | 3.874× |
| Wall time per sampled frame | 9.713 s | 4.427 s | 4.045 s |
| Detector proposals / recognized crops | 298 | 107 | 107 |
| Accepted observations | 254 | 100 | 100 |
| Detection | 19.212 s | 6.174 s | 5.046 s |
| Recognition | 66.834 s | 21.169 s | 18.622 s |
| Persistence/evidence | 12.476 s | 12.586 s | 12.130 s |
| Other | 7.485 s | 8.217 s | 8.165 s |
| Model setup (outside scan wall time) | 12.880 s | 4.845 s | 7.226 s |

The first top-right run was 2.19× faster than the full-frame run by scan wall time. The detailed run used the same region and processed the same number of frames and crops; its different stage times show run-to-run variation. These are short runs, so they should not be extrapolated to long-video throughput without further measurement. The top-right crop excludes names elsewhere in the frame; name coverage has not been evaluated. Visually inspected full-frame crops included unrelated HUD text and occasional non-text images, but no measured player-name precision is available.

## Detailed persistence result

The detailed profile recorded three persistence batches of 5, 5, and 1 frames, with 46, 46, and 8 accepted observations. Each batch checked about 5,900 files and took 4.056, 3.993, and 3.915 seconds in compaction, respectively. Across the three batches:

| Nested measurement | Time / count |
|---|---:|
| Batch persistence/evidence total | 12.130 s |
| Compaction total | 11.965 s |
| Orphan sweep: filesystem walk | 8.095 s |
| Orphan sweep: reference lookup and path resolution | 3.836 s |
| Compaction work outside those sweep phases | 0.034 s |
| Transaction total, including image saves | 0.165 s |
| Image saves (nested within transaction) | 0.124 s |
| Asset files checked / orphan files deleted | 17,824 / 13 |
| Images saved during the batches | 71 |

The measured transaction and image-save times are small relative to the sweep. The near-constant compaction time despite the final batch having only 8 observations is consistent with work proportional to the existing output corpus rather than to new observations. A separate read-only reproduction of one sweep on the then-current 5,888-file corpus took 5.365 seconds (1.604 seconds gathering/resolving references and 3.761 seconds walking files). That reproduction did not run during the profiled scan.

## Code path and measurement boundaries

- [`detected_ingestion.py`](../src/tf2scan/detected_ingestion.py) collects frames into batches using `persistence.max_bytes`; this setting counts the *full sampled frame* even when detector input is cropped. Each batch calls `_commit_batch`, which writes observations and evidence, then calls `Store.compact_evidence` when compaction is enabled.
- [`storage.py`](../src/tf2scan/storage.py) `compact_evidence` examines the current scan's clusters and crop references, then calls `prune_orphan_evidence`. The latter gathers referenced paths from all scans and walks all scanner-owned asset folders. The detailed profile recorded three such sweeps inside batch persistence.
- [`performance.py`](../src/tf2scan/performance.py) reports nested persistence timers. `image_save` is included in `transaction_total`; reference lookup and file walk are included in `compaction_total`. These values must not be summed as independent stages.
- The top-level `other` stage includes work outside timed decoding, detection, crop preparation, recognition, and batch persistence. [`detected_ingestion.py`](../src/tf2scan/detected_ingestion.py) also invokes evidence cleanup before batch processing and after scan completion; those calls are not broken out in the current detailed profile. Their share of `other` is unmeasured.

Open questions for further diagnosis: the exact cost of startup/final cleanup, whether longer scans show the same per-sweep cost as the asset corpus grows, and how the top-right crop changes player-name coverage. Any cleanup change needs to preserve evidence references and interrupted-scan recovery behavior.
