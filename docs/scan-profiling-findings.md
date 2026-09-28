# Scan profiling findings (2026-09-27–28)

The initial measurements below summarize three local runs of the same 11.485833-second video (`input/uw1.mp4`, video ID `local-31b63f8a4f55987ce1f4`). The source is 3440×1440; sampling was 1 frame/second, with 11 frames processed. The scanner ran on Windows build 26200 with an AMD64 Family 25 Model 33 CPU (12 logical CPUs). Detection and recognition used the Paddle PP-OCRv6 small CPU models. Model setup time is reported separately from scan wall time. The full JSON reports are ignored local artifacts at `output/scan-profile.json`, `output/top-right-profile.json`, and `output/top-right-detailed-profile.json`.

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

## Initial code path and measurement boundaries (before incremental cleanup)

- [`detected_ingestion.py`](../src/tf2scan/detected_ingestion.py) collected frames into batches using `persistence.max_bytes`; this setting counts the *full sampled frame* even when detector input is cropped. Each batch called `_commit_batch`, which wrote observations and evidence, then called `Store.compact_evidence` when compaction was enabled.
- At this baseline, [`storage.py`](../src/tf2scan/storage.py) `compact_evidence` examined the current scan's clusters and crop references, then called `prune_orphan_evidence`. The latter gathered referenced paths from all scans and walked all scanner-owned asset folders. The detailed profile recorded three such sweeps inside batch persistence.
- [`performance.py`](../src/tf2scan/performance.py) reports nested persistence timers. `image_save` is included in `transaction_total`; reference lookup and file walk are included in `compaction_total`. These values must not be summed as independent stages.
- In these historical profiles, the top-level `other` stage included work outside timed decoding, detection, crop preparation, recognition, and batch persistence. Evidence cleanup also ran before batch processing and after scan completion; its share of `other` was not measured. Current profiles time startup recovery separately.

These were questions at the initial baseline. Current startup timing is below; longer scans, player-name coverage, and focused evidence-reference/recovery checks remain open.

## oneDNN trial (2026-09-28)

At the time of this trial, `detector.enable_mkldnn` and `recognizer.enable_mkldnn` both defaulted to `false`. The trial reprocessed the same `input/uw1.mp4` clip at 1 fps with the top-right detector region, using the same output corpus. All completed runs had 11 frames and 107 recognized crops. Model setup remains outside scan wall time. These are single runs, and the corpus and filesystem cache were not reset between them.

| Run | Wall | Detection | Recognition | Persistence/evidence | Other |
|---|---:|---:|---:|---:|---:|
| Earlier CPU baseline | 44.493 s | 5.046 s | 18.622 s | 12.130 s | 8.165 s |
| oneDNN recognizer only | 52.663 s | 7.521 s | 8.379 s | 21.918 s | 13.809 s |
| CPU repeat after trial | 60.973 s | 6.493 s | 21.794 s | 19.078 s | 12.837 s |

With oneDNN on both models, detector inference failed on its first frame with `NotImplementedError: ConvertPirAttribute2RuntimeAttribute not support [pir::ArrayAttribute<pir::DoubleAttribute>]`. That run produced no successful profile. Recognizer-only oneDNN completed and reduced recognition time by 55% versus the earlier baseline and 62% versus the adjacent CPU repeat. Total scan time was 14% below the adjacent repeat but above the earlier baseline because persistence and other time varied greatly. The runs used separate cached model directories; their inference files have matching SHA-256 values, while one directory also contains a Hugging Face cache JSON file that makes `file_hash` differ. The results support further recognizer-only testing, not enabling oneDNN for the detector on this runtime. The ignored trial profiles are `output/top-right-mkldnn-rec-profile.json` and `output/top-right-cpu-repeat-profile.json`.

## Incremental cleanup and current CPU default (2026-09-28)

Routine compaction now visits clusters changed by the committed batch and checks only paths whose references it removed. A full orphan sweep runs once per open corpus before its first detected scan; there is no full sweep after every batch or at successful scan completion. The recognizer now defaults to oneDNN on CPU. Detector oneDNN stays off after the failure above; cross-frame crop pooling defaults to one frame and both adapters use the runtime's 10-thread default. Effective settings are recorded in each profile.

The two runs below used the same 11.486-second clip, 11 frames, 107 crops, top-right region, and output corpus. Their saved profiles are `output/incr-cleanup-test.json` and `output/incr-cleanup-test-2.json`. Model setup is outside the scan wall time.

| Measurement | Incremental cleanup, oneDNN off | Incremental cleanup, recognizer oneDNN on |
|---|---:|---:|
| Scan wall / real-time factor | 30.599 s / 2.664× | 16.031 s / 1.396× |
| Detection | 5.544 s | 5.595 s |
| Recognition | 19.910 s | 5.411 s |
| Startup orphan cleanup | 4.257 s | 4.200 s |
| Batch compaction, all three batches | 0.141 s | 0.104 s |
| Asset paths checked during batches | 13 | 13 |
| Model setup, excluded from scan wall | 4.767 s | 4.880 s |

The oneDNN run is **not real time** by total scan wall time. Excluding startup orphan cleanup as well leaves 11.831 seconds, or about 1.03× the video duration. Detection plus recognition alone took 11.006 seconds. The initial detailed run's 11.965 seconds of batch compaction fell to 0.104 seconds in the later oneDNN run, but these are separate short runs rather than controlled repeated trials against an identical corpus snapshot.

Optional four-frame pooling and a four-thread runtime setting were each tried once on a copied corpus with recognizer oneDNN. Their scan wall times were 18.121 and 18.524 seconds, respectively, versus 21.186 seconds for the first incremental run on that copy; that first run still included a final full sweep. Pooling took 5.879 seconds in recognition versus 5.454 seconds in the single-frame run and changed several OCR strings and one accepted observation. Four threads took 6.157 seconds in recognition and 6.262 seconds in detection. These comparisons do not establish a benefit, so neither setting became the default. The ignored trial profiles are under `output/perf-incremental/`.

These measurements are single short runs. A longer, repeated baseline with a fixed starting corpus is needed to estimate sustained throughput and startup amortization. No manually verified name set exists yet to assess oneDNN or pooling accuracy or top-right coverage.

## Longer online scans in the saved corpus (2026-09-28)

Four later completed scans in `output/results.sqlite3` used the same top-right, 1-fps policy and CPU settings (recognizer oneDNN on, detector oneDNN off, 10 threads). No per-stage JSON profile was saved for them. The table uses `video_scans.started_at` to `completed_at`, rounded to whole seconds by SQLite, so it includes per-video setup within ingestion such as source resolution and first-scan orphan recovery but excludes model construction before ingestion. The `chunk_attempts.elapsed_s` values were slightly shorter.

| Video scan ID | Catalog duration | Scan wall | Wall / duration | Sampled frames / expected at 1 fps |
|---|---:|---:|---:|---:|
| 15, TF2 - Irritable Contract Worker | 477 s | 392 s | 0.822× | 455 / 477 |
| 16, Admiralty of an Anchor | 148 s | 101 s | 0.682× | 148 / 148 |
| 17, First days of Team Fortress 2 Classified | 197 s | 141 s | 0.716× | 197 / 197 |
| 18, This is Fine. (TF2 Commentary) | 397 s | 332 s | 0.836× | 397 / 397 |
| **Combined** | **1,219 s** | **966 s** | **0.792×** | **1,197 / 1,219** |

These recorded scans are faster than real time by catalog duration, and the three with complete timestamp coverage are individually below RTF 1. Scan 15 is marked completed but its last sampled timestamp is 454 s despite a 477 s catalog duration. The media may be shorter than the catalog entry or scanning may have stopped early; this needs source-duration verification before treating it as fully covered. Even dividing its 392 s wall time by the 455 sampled seconds gives about 0.86×, so that gap does not explain away its measured throughput. The scans were consecutive in one process, so model setup and one-time orphan recovery were amortized. They vary in text density and source content, and no manual name-coverage evaluation has been done.
