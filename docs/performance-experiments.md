# Experiments toward near-realtime scanning

Proposed on 2026-09-27 after reviewing [profiling findings](scan-profiling-findings.md), the local JSON report, and the production scan path. The budget and experiment descriptions below record the original plan. See [measured follow-up](scan-profiling-findings.md#incremental-cleanup-and-current-cpu-default-2026-09-28) for the subsequent cleanup and runtime results.

As of 2026-09-28, incremental batch cleanup and oneDNN recognition are implemented. The short top-right profile took 16.031 seconds for 11.486 seconds of video (RTF 1.396), including 4.200 seconds of startup orphan recovery and excluding 4.880 seconds of model setup. Excluding startup recovery too gives RTF about 1.03. Four subsequent online scans of 148–477 catalog seconds each recorded RTF 0.682–0.836 by per-video database timestamps; three sampled every expected second, while the 477-second entry stopped at timestamp 454 s. See [longer scan findings](scan-profiling-findings.md#longer-online-scans-in-the-saved-corpus-2026-09-28). Four-frame pooling and four CPU threads were tried once and were not selected as defaults. Repeatability, full source coverage for the 477-second entry, and OCR accuracy remain open.

| Experiment | Current state | Remaining work |
|---|---|---|
| 1. Attribution and baselines | **Partial.** Short full-frame and top-right profiles, detailed persistence timing, and startup recovery timing exist. Four longer online scans have per-video elapsed times but no per-stage profiles. | Repeat from identical corpus snapshots; measure first/warm inference, CPU and memory use, source coverage, and result latency. |
| 2. Incremental evidence cleanup | **Implemented; acceptance pending.** Routine batches compact changed clusters and check only paths whose references they removed. Startup recovery runs once per open corpus; successful completion has no final full sweep. | Check commit cost against unrelated corpus size and add focused shared-reference, interruption, temporary-file, and ownership tests. |
| 3. Recognition batching | **Partial.** Bounded cross-frame pooling is implemented; one four-frame trial did not improve recognition time and the default remains one frame. | Replay fixed crops across batch sizes and aspect ratios, repeat end-to-end trials, and check OCR output, memory, and commit latency. |
| 4. Runtime settings | **Partial.** Recognizer oneDNN is the CPU default. Detector oneDNN failed on the tested runtime; one four-thread trial was not selected. | Repeat controlled thread and runtime comparisons on fixed inputs and verify output differences. |
| 5. Repeated OCR and coverage | **Open beyond region selection.** Full-frame and top-right modes exist and were timed; no temporal reuse or periodic full-frame policy is implemented. | Build verified occurrence labels, then evaluate reuse, region policies, sampling, and resolution for speed and coverage. |

The first throughput milestone is observed on three longer scans with expected timestamp coverage, but is not yet established as a repeatable, coverage-checked result. The short profiled scan remains above RTF 1. Sections below retain the original experimental detail; their status is updated where implementation changed the plan.

## Target and current budget

Use wall time / video duration (RTF) <= 1 as the first throughput target at the current 1 sampled frame/second. Aim for <= 0.8 on longer representative clips to leave headroom. Report model startup separately, plus total command time for short jobs. This is a proposed milestone for the current near-realtime goal; the high-level design's older “10x real-time” wording should not be used as the benchmark definition.

Throughput and result latency need separate measurements. A scan can keep up on average while holding results in a batch for many seconds. Record time to first committed result and the wall-clock delay from a sampled frame entering the pipeline to its commit. The current commit interval is measured in video timestamps, not elapsed wall time, and settings restrict it to 10–30 seconds. The scanner currently requires finite VODs; live ingestion would be separate work.

For the original detailed top-right run (11.486 seconds of video, 11 sampled frames), before these changes:

| Component | Wall seconds | Share | Interpretation |
|---|---:|---:|---|
| Recognition | 18.622 | 41.9% | Largest measured stage; about 174 ms per crop across 107 crops, including adapter work |
| Batch compaction | 11.965 | 26.9% | Almost entirely corpus-wide orphan sweeps |
| Other | 8.165 | 18.4% | Startup/final cleanup is a plausible contributor, not yet measured |
| Detection | 5.046 | 11.3% | About 459 ms per sampled frame |
| Decode, crop preparation, batch transaction | 0.694 | 1.6% | Small in this run; totals differ slightly due to rounding |

At that baseline, removing only measured batch compaction would have left about 32.528 seconds, or RTF 2.83. Even optimistically removing all persistence and all `other` would have left 24.198 seconds, or RTF 2.11. These were pre-change budget calculations, not current timings.

At that baseline, if detection, decoding and crop preparation stayed unchanged, only about 5.91 seconds would have remained for recognition within this clip's realtime budget, before allowing any persistence overhead. Recognition would have needed roughly a 3.15x reduction. The current oneDNN follow-up measured 5.411 seconds of recognition on the same short clip, but the total scan still exceeded its duration. Longer runs may have different warmup and content costs.

## 1. Finish attribution and establish repeatable baselines

**Status: partial.** The short full-frame/top-right comparison and detailed persistence profile are complete. Startup orphan recovery is now a separate stage and measured about 4.2 seconds on the short clip. Four longer online scans provide elapsed times, but were consecutive in one process, were not repeated from fixed corpus snapshots, and one has a source-coverage gap. There is no controlled baseline across corpus sizes or measurement of first/warm call latency, CPU use, memory, or commit delay.

**Question:** How much of the cost is fixed startup, first inference, recurring inference, and corpus maintenance?

- Keep startup recovery separate in profiles; additionally time tracker restore, scan finalization and source resolution. Successful scans no longer have a final full sweep. Preserve non-overlapping top-level stages when adding timers.
- Record each detector and recognizer call: first-call versus subsequent time, actual batch size, crop count and dimensions, frame timestamp, and median/p95/max latency. Keep these summaries bounded in memory.
- Inside the Paddle adapters, separate RGB/BGR conversion from the fully consumed `predict` call. The latter still includes framework preprocessing, inference and postprocessing; use a backend profiler only if this split leaves the next decision unclear. Record actual resized/padded tensor shapes when available.
- Measure process CPU time, CPU utilization, peak memory and effective inference thread settings. Include the FFmpeg subprocess when interpreting CPU use. The current `decoding` timer measures time waiting for sampled frames; FFmpeg can already work while Python runs OCR.
- Run the current short clip plus 2–5 minute quiet and busy clips, and one clip crossing the default 600-second chunk boundary. Include both full-frame and top-right baselines; they have different coverage contracts.
- Repeat each configuration at least three times, alternating order. Separate fresh-process runs from repeated inference with loaded models. Record median and range; use longer runs for meaningful tail latency.
- Use separate output roots restored from the same starting corpus snapshot for each trial: empty, approximately 6,000 assets, and a larger representative corpus. Preserve database references as well as files. Repeated `--reprocess` scans into one growing root are not a controlled comparison.

Record code revision, input hash/duration, effective settings, model hashes, runtime versions, actual hardware, frame/crop counts, committed observations, starting/ending asset counts and memory alongside timing. Save profiles under ignored experiment output folders and summarize results in the findings document. Force actual rescanning; already-completed scans may be skipped.

**Decision:** Use steady-state seconds per sampled frame and RTF versus corpus size to distinguish model throughput from maintenance growth. Do this before a large parameter search.

## 2. Remove corpus-wide sweeps from each persistence batch

**Status: implementation complete; acceptance pending.** Commit `cba470b` added changed-cluster compaction and targeted deletion after reference changes commit. The 11-frame follow-up spent 0.104 seconds on three batch compactions and checked 13 candidate paths, compared with 11.965 seconds of batch compaction in the earlier detailed run. Those are separate single runs, not a controlled scaling comparison. Existing tests check that routine batches skip the global sweep; the focused recovery and shared-reference matrix below remains open.

**Hypothesis:** Durable commits are cheap; global cleanup attached to them is expensive.

The original diagnostic proposal was to compare the baseline with `evidence.compact=false`. At that time this disabled batch compaction but left startup and final orphan sweeps, retaining extra evidence. The current implementation runs startup recovery once per open corpus and uses targeted cleanup after batches; a final full sweep no longer runs on successful completion. Disabling compaction is still not an equivalent-retention comparison.

The implementation now separates retention decisions from orphan recovery:

- **Done:** Frequent durable commits compact changed clusters and consider only paths whose references that batch removed.
- **Done in code; test further:** Candidate deletion follows committed reference changes and a fresh reference check. Verify shared full-frame, representative, and query evidence explicitly.
- **Done:** A full orphan sweep runs once per open corpus before its first detected scan, rather than after batches or successful scan completion. Measure its startup cost as the corpus grows.
- **Remaining:** Check whether changed clusters with many accumulated observations or aliases develop a new compaction cost as scans grow.

Measure persistence time per commit against corpus size, files examined, reference queries, retained evidence size and startup recovery time. Increasing `persistence.max_bytes` retains more memory and delays commits; it is not the preferred fix.

**Acceptance:** Similar OCR observations and retained evidence policy, flat routine commit cost as unrelated corpus assets grow, and recovery tests covering interruption before/after transaction commit, referenced files, temporary files and ownership/path protections. Preserve the single-writer contract.

## 3. Isolate recognition and test real batching

**Status: partial.** Cross-frame crop pooling with result mapping back to frames is implemented, but defaults to one frame. One four-frame trial had higher recognition time than the single-frame comparison and changed several OCR strings and one accepted observation. The fixed-crop replay, batch-size sweep, and repeatability checks have not been done.

**Hypothesis:** Recognition call overhead, batch occupancy or padding contributes to the 18.6 seconds.

Save a fixed corpus of the exact prepared crops, with frame association and original order. Replay it through one loaded recognizer, with disk loading outside the timed region. Measure the first pass separately, then repeated warm passes. This isolates recognition from changing detector output and filesystem cleanup.

Compare batch sizes 1, 4, 8, 16 and 32. Report milliseconds per crop, crops/second, call latency, occupancy and peak memory. Test original order versus grouping similar aspect ratios, recording actual padding where possible and restoring result order afterward. Treat any benefit from width grouping as a hypothesis until the backend's existing behavior is measured.

With the current one-frame pooling default, `recognize_crops` receives one frame's crops at a time. The top-right run averaged only 9.7 crops per frame, so `batch_size=32` does not imply full 32-crop batches. Optional `recognizer.pool_frames` can pool a bounded number of frames and restore their result slices; repeat its comparison with commit latency and memory measurements before changing the default.

Increasing detector batch size alone will not create batched inference: `PaddleDetector.detect` loops over frames and calls `predict(..., batch_size=1)`. Test true detector batching separately only after recognition results justify it, preserving per-image resize and source-coordinate mapping.

**Acceptance:** Repeatable end-to-end improvement, correct crop/result association, and no material regression in name recognition. Include zero-crop frames and incomplete final batches in implementation checks.

## 4. Test runtime settings on fixed inputs

**Status: partial.** Commit `da86b92` exposed oneDNN controls, and commit `cba470b` enabled recognizer oneDNN by default. A recognizer-only trial was faster; detector oneDNN failed on first inference with the tested Paddle runtime. Four CPU threads were tried once and were not selected. Fixed-input, repeated comparisons and accuracy checks remain open.

At the time of this proposal, the factory disabled oneDNN and did not explicitly configure CPU threads. The current CPU default enables oneDNN only for recognition and records a shared thread setting in scan provenance. Detector oneDNN failed on the tested Paddle runtime; the four-thread trial was slower than the default-thread trial on this clip.

Compare supported acceleration settings and thread counts on fixed crops and cached detector inputs, including the current 10-thread default and the available 12 logical CPUs. Change one variable at a time; record stability, output differences, memory and warm/cold performance. Revisit detector oneDNN only with a compatible runtime or a specific fix for its observed failure. Do not assume more threads are faster.

If a compatible GPU is available, measure the same models there after verifying runtime support. Include transfer, preprocessing and initialization costs. Runtime or device alternatives are experiments, not assumed speedups, and successful settings must be recorded in scan provenance.

**Decision:** Prefer a stable runtime/batching improvement before changing models. Consider alternate recognizers if measured recognition throughput still cannot meet the budget.

## 5. Reduce repeated OCR work, with a coverage check

**Status: open beyond region selection.** Full-frame and top-right detection modes are implemented and were timed, but no exact or approximate OCR reuse, periodic full-frame policy, or sampling/resolution accuracy sweep has been completed. The verified occurrence set needed to judge coverage is still open under R12/R13 in [todo](todo.md).

The tracker associates observations after recognition, so repeated visible text currently still incurs OCR. Explore these options in increasing order of behavioral change:

1. **Reuse recognition for exactly identical prepared crop pixels** within a bounded cache keyed by model/preprocessing identity and crop shape/content. Measure hit rate and hashing cost first. Compression and moving backgrounds may make exact hits rare. Preserve each frame's geometry and observation lineage; repeated cached output is not independent OCR evidence.
2. **Recognize only changed text regions**, using geometry plus image similarity and periodic forced refresh. Evaluate changed names, fade animations, moving killfeed rows, scene changes and reappearance; text similarity from prior OCR alone cannot establish that pixels are unchanged.
3. **Use frequent ROI detection plus periodic full-frame detection**, evaluating names in killfeed, chat, scoreboard and spectator UI. Top-right-only scanning already reduces work substantially but excludes other regions. Maintain target-independent retention.
4. **Sweep sampling rate or working resolution** only against verified occurrence labels. Lower sampling can miss brief appearances; lower resolution can erase small glyphs. Try 0.5/1/2 fps and a small resolution sweep separately, reporting speed together with occurrence recall and false candidates.

The small verified set in R12/R13 should run alongside these experiments. Exact-output comparisons help for implementation-only changes; any cropping, sampling, approximate caching or model change needs occurrence-level accuracy evaluation. Retain examples of misses, not just aggregate scores.

## Recommended order

First verify the 477-second entry's actual media duration and end behavior. Build the small verified occurrence set, then repeat longer full-frame and top-right baselines against identical starting corpora while measuring startup, throughput, memory and result latency. Validate incremental cleanup scaling and recovery cases. Run fixed-crop batching and runtime comparisons before changing their defaults. Use those results and the verified labels to decide whether temporal reuse, region policies, or model/device changes are justified.

Defer broad parallelization until these results are known. Detection and recognition currently run serially, but both may compete for the same CPU resources. A bounded pipeline experiment should measure queue depth, resource contention, memory and result latency; theoretical stage overlap is not an assured end-to-end gain. JPEG tuning, generic SQL tuning and faster decode are lower priority given the current measurements.
