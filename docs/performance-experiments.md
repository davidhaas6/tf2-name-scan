# Experiments toward near-realtime scanning

Proposed on 2026-09-27 after reviewing [profiling findings](scan-profiling-findings.md), the local JSON report, and the production scan path. These are proposed experiments, not measured improvements.

## Target and current budget

Use wall time / video duration (RTF) <= 1 as the first throughput target at the current 1 sampled frame/second. Aim for <= 0.8 on longer representative clips to leave headroom. Report model startup separately, plus total command time for short jobs. This is a proposed milestone for the current near-realtime goal; the high-level design's older “10x real-time” wording should not be used as the benchmark definition.

Throughput and result latency need separate measurements. A scan can keep up on average while holding results in a batch for many seconds. Record time to first committed result and the wall-clock delay from a sampled frame entering the pipeline to its commit. The current commit interval is measured in video timestamps, not elapsed wall time, and settings restrict it to 10–30 seconds. The scanner currently requires finite VODs; live ingestion would be separate work.

For the detailed top-right run (11.486 seconds of video, 11 sampled frames):

| Component | Wall seconds | Share | Interpretation |
|---|---:|---:|---|
| Recognition | 18.622 | 41.9% | Largest measured stage; about 174 ms per crop across 107 crops, including adapter work |
| Batch compaction | 11.965 | 26.9% | Almost entirely corpus-wide orphan sweeps |
| Other | 8.165 | 18.4% | Startup/final cleanup is a plausible contributor, not yet measured |
| Detection | 5.046 | 11.3% | About 459 ms per sampled frame |
| Decode, crop preparation, batch transaction | 0.694 | 1.6% | Small in this run; totals differ slightly due to rounding |

Removing only measured batch compaction would leave about 32.528 seconds, or RTF 2.83. Even optimistically removing all persistence and all `other` leaves 24.198 seconds, or RTF 2.11. Cleanup improvements alone cannot reach realtime on this workload.

If detection, decoding and crop preparation stayed unchanged, only about 5.91 seconds would remain for recognition within this clip's realtime budget, before allowing any persistence overhead. Recognition would need roughly a 3.15x reduction. This is a budget calculation, not a prediction; longer runs may have different warmup and content costs.

## 1. Finish attribution and establish repeatable baselines

**Question:** How much of the cost is fixed startup, first inference, recurring inference, and corpus maintenance?

- Time startup sweep, final compaction/sweep, tracker restore, scan finalization and source resolution separately. Pass the profiler into cleanup calls and label their phases so nested timers are not counted twice.
- Record each detector and recognizer call: first-call versus subsequent time, actual batch size, crop count and dimensions, frame timestamp, and median/p95/max latency. Keep these summaries bounded in memory.
- Inside the Paddle adapters, separate RGB/BGR conversion from the fully consumed `predict` call. The latter still includes framework preprocessing, inference and postprocessing; use a backend profiler only if this split leaves the next decision unclear. Record actual resized/padded tensor shapes when available.
- Measure process CPU time, CPU utilization, peak memory and effective inference thread settings. Include the FFmpeg subprocess when interpreting CPU use. The current `decoding` timer measures time waiting for sampled frames; FFmpeg can already work while Python runs OCR.
- Run the current short clip plus 2–5 minute quiet and busy clips, and one clip crossing the default 600-second chunk boundary. Include both full-frame and top-right baselines; they have different coverage contracts.
- Repeat each configuration at least three times, alternating order. Separate fresh-process runs from repeated inference with loaded models. Record median and range; use longer runs for meaningful tail latency.
- Use separate output roots restored from the same starting corpus snapshot for each trial: empty, approximately 6,000 assets, and a larger representative corpus. Preserve database references as well as files. Repeated `--reprocess` scans into one growing root are not a controlled comparison.

Record code revision, input hash/duration, effective settings, model hashes, runtime versions, actual hardware, frame/crop counts, committed observations, starting/ending asset counts and memory alongside timing. Save profiles under ignored experiment output folders and summarize results in the findings document. Force actual rescanning; already-completed scans may be skipped.

**Decision:** Use steady-state seconds per sampled frame and RTF versus corpus size to distinguish model throughput from maintenance growth. Do this before a large parameter search.

## 2. Remove corpus-wide sweeps from each persistence batch

**Hypothesis:** Durable commits are cheap; global cleanup attached to them is expensive.

Compare baseline with `evidence.compact=false` as an initial diagnostic. This disables batch compaction, but startup and final orphan sweeps still run, and extra evidence is retained. It is not a “cleanup off” or equivalent-retention configuration.

Then prototype separating retention decisions from orphan recovery:

- Keep frequent durable commits, but compact only changed clusters and consider only paths whose references were removed by that batch.
- Delete candidates after committing reference changes and confirming no remaining references. Full-frame paths and representative/query evidence may be shared across records.
- Run a full orphan sweep at an explicit recovery/maintenance boundary rather than after every batch. Moving it just to finalization improves throughput but still leaves corpus-dependent completion latency; measure that separately.
- Inspect repeated whole-scan compaction as scans grow: it revisits clusters and, with aliases, their observations every batch. This may become a second growth problem after filesystem sweeps are removed.

Measure persistence time per commit against corpus size, files examined, reference queries, retained evidence size and final cleanup time. Increasing `persistence.max_bytes` can diagnose sweep frequency, but retains more memory and delays commits; it is not the preferred fix.

**Acceptance:** Similar OCR observations and retained evidence policy, flat routine commit cost as unrelated corpus assets grow, and recovery tests covering interruption before/after transaction commit, referenced files, temporary files and ownership/path protections. Preserve the single-writer contract.

## 3. Isolate recognition and test real batching

**Hypothesis:** Recognition call overhead, batch occupancy or padding contributes to the 18.6 seconds.

Save a fixed corpus of the exact prepared crops, with frame association and original order. Replay it through one loaded recognizer, with disk loading outside the timed region. Measure the first pass separately, then repeated warm passes. This isolates recognition from changing detector output and filesystem cleanup.

Compare batch sizes 1, 4, 8, 16 and 32. Report milliseconds per crop, crops/second, call latency, occupancy and peak memory. Test original order versus grouping similar aspect ratios, recording actual padding where possible and restoring result order afterward. Treat any benefit from width grouping as a hypothesis until the backend's existing behavior is measured.

Current `recognize_crops` runs once per frame. The top-right run averages only 9.7 crops per frame, so `batch_size=32` does not imply full 32-crop batches. A second experiment should pool crops across a bounded number of frames, with a maximum wait time and explicit mapping back to frames. Preserve chronological tracking and commit watermarks; compare throughput gains against commit latency and memory.

Increasing detector batch size alone will not create batched inference: `PaddleDetector.detect` loops over frames and calls `predict(..., batch_size=1)`. Test true detector batching separately only after recognition results justify it, preserving per-image resize and source-coordinate mapping.

**Acceptance:** Repeatable end-to-end improvement, correct crop/result association, and no material regression in name recognition. Include zero-crop frames and incomplete final batches in implementation checks.

## 4. Test runtime settings on fixed inputs

The factory explicitly disables MKL-DNN and does not explicitly configure CPU threads. The available docs establish that this combination was validated, but do not explain why acceleration was disabled.

Inspect the installed runtime's supported options and the reason for disabling MKL-DNN before experimenting. Compare the baseline with supported acceleration settings and thread counts such as 1, 2, 4 and 6, adding higher counts only if utilization and scaling justify them. Change one variable at a time, using the fixed crop corpus and cached detector inputs; record stability, output differences, memory and warm/cold performance. Do not assume more threads are faster.

If a compatible GPU is available, measure the same models there after verifying runtime support. Include transfer, preprocessing and initialization costs. Runtime or device alternatives are experiments, not assumed speedups, and successful settings must be recorded in scan provenance.

**Decision:** Prefer a stable runtime/batching improvement before changing models. Consider alternate recognizers if measured recognition throughput still cannot meet the budget.

## 5. Reduce repeated OCR work, with a coverage check

The tracker associates observations after recognition, so repeated visible text currently still incurs OCR. Explore these options in increasing order of behavioral change:

1. **Reuse recognition for exactly identical prepared crop pixels** within a bounded cache keyed by model/preprocessing identity and crop shape/content. Measure hit rate and hashing cost first. Compression and moving backgrounds may make exact hits rare. Preserve each frame's geometry and observation lineage; repeated cached output is not independent OCR evidence.
2. **Recognize only changed text regions**, using geometry plus image similarity and periodic forced refresh. Evaluate changed names, fade animations, moving killfeed rows, scene changes and reappearance; text similarity from prior OCR alone cannot establish that pixels are unchanged.
3. **Use frequent ROI detection plus periodic full-frame detection**, evaluating names in killfeed, chat, scoreboard and spectator UI. Top-right-only scanning already reduces work substantially but excludes other regions. Maintain target-independent retention.
4. **Sweep sampling rate or working resolution** only against verified occurrence labels. Lower sampling can miss brief appearances; lower resolution can erase small glyphs. Try 0.5/1/2 fps and a small resolution sweep separately, reporting speed together with occurrence recall and false candidates.

The small verified set in R12/R13 should run alongside these experiments. Exact-output comparisons help for implementation-only changes; any cropping, sampling, approximate caching or model change needs occurrence-level accuracy evaluation. Retain examples of misses, not just aggregate scores.

## Recommended order

Start with attribution and the controlled compaction diagnostic. Next implement incremental cleanup and run the fixed-crop batching/runtime experiments. Re-profile the full pipeline after each successful change. Only then choose between temporal reuse, coverage-aware region policies and model/device changes based on the remaining budget.

Defer broad parallelization until these results are known. Detection and recognition currently run serially, but both may compete for the same CPU resources. A bounded pipeline experiment should measure queue depth, resource contention, memory and result latency; theoretical stage overlap is not an assured end-to-end gain. JPEG tuning, generic SQL tuning and faster decode are lower priority given the current measurements.
