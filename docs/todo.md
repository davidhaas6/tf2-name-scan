# Near-term tasks

This is a lightweight task tracker for the next practical checks, not a complete implementation plan.

- [x] **Separate scanning from searching.** `scan` reports ingestion totals without querying or exporting; `query` reuses saved OCR. Evidence retention is independent of configured names and aliases.
- [ ] **Reassess optimization using the current GPU profiles.** The saved GPU profile is dominated by decoding; the older CPU detector oneDNN failure is historical. Inspect acquisition/decode cost before further detector tuning. See [ingestion readiness](ingestion-readiness.md).

- [ ] **Validate Python 3.11 or 3.12 for the pinned Paddle stack.** Run setup, indexing, and a short scan on Windows with a fresh environment; confirm OCR output and warnings, then update `requires-python`, the lockfile, and setup instructions to the verified minimum.

- [x] **Profile one representative scan.** Full-frame and top-right runs, plus detailed persistence timings, are recorded in [scan profiling findings](scan-profiling-findings.md).

- [x] **Remove corpus-wide cleanup from routine batches.** Changed-cluster compaction and targeted path deletion are implemented; the 11-frame follow-up checked 13 paths across three batches. Startup orphan recovery runs once per open corpus. See [measured follow-up](scan-profiling-findings.md#incremental-cleanup-and-current-cpu-default-2026-09-28). Focused interruption/reference tests and scaling checks remain under P2.

- [x] **Enable the measured faster CPU recognizer setting.** Recognizer oneDNN is on by default; detector oneDNN remains off after a runtime failure. Four-frame pooling and four CPU threads were tried once and were not selected. Fixed-crop and repeated benchmarks remain under P3.

- [x] **Exercise longer scans and chunk boundaries.** Nine completed GPU scans cover 7,655 seconds, each over ten minutes, with complete 1-fps sample grids across 600-second chunk boundaries. Uninterrupted runs have RTF 0.505–0.578 by database timestamps. These are operational results; controlled repetition remains under P1. See [ingestion readiness](ingestion-readiness.md).

- [x] **Validate real interruption/resume and queue continuation.** User-confirmed working; scan 24 resumes at 430 seconds and finishes with all 758 samples. The queue continues after scan 30's HTTP 403. See [ingestion readiness](ingestion-readiness.md).

- [x] **Investigate material early EOF and prevent silent completion.** The scan-15 source is still 476.967 seconds and fresh tail decoding reaches second 476. Bounded decoding now rejects a clean exit more than one sample short and enters existing retry/resume handling. Real local failure/resume and decoder-exit tests pass. The historical decoder cause is unrecorded. See [ingestion readiness](ingestion-readiness.md).

- [x] **Replace the historical short scan with a complete generation.** Reprocessed `4iLIfCzToFY` on 2026-10-01: selected scan 33 has all 477 unique timestamps through second 476, intact representative evidence, and RTF 0.566. Original scan 15 remains preserved with its 455 frames. See [ingestion readiness](ingestion-readiness.md).

- [ ] **R12: Build a small verified video set.** Manually label visible username occurrences and negative regions in one short, exhaustively checked video window; record timestamps, exact names, and stable occurrence IDs. Add a second window from a different HUD or compression setting, keeping each source separate and marking any unlabelled regions as unknown rather than negative.

- [ ] **R13: Evaluate the production pipeline on those windows.** Compare scanner hits with the labels at the occurrence and video level, and record missed names, false candidates, duplicate hits, and review workload. Report counts and coverage alongside recall and false-hit rates; use the failures to decide whether to expand the dataset, fix accuracy, or optimize the measured bottleneck.

# Maybe

- [ ] **Explore reusing OCR for unchanged text.** Measure exact crop matches across nearby frames first; if rare, test position-aware image similarity with periodic fresh OCR. Compare missed name changes and runtime on verified clips before adopting it.

- [ ] **P1: Complete performance attribution and repeatable longer baselines.** Startup recovery measured about 4.2 seconds on the short clip; successful scans no longer run a final full sweep. Measure first versus warm inference, actual OCR batch occupancy, CPU use, memory and commit latency. Repeat longer top-right scans against identical starting corpora, recording total command time and source coverage. Use RTF <= 1 at 1 sampled fps as the first throughput milestone. See [performance experiments](performance-experiments.md).

- [ ] **P2: Validate incremental evidence cleanup.** The routine path is implemented and examined 13 removed paths rather than the whole corpus in the short follow-up. Measure commit cost as unrelated corpus size grows; add focused tests for shared references, failed transactions, temporary files, interrupted scans and path ownership. See experiment 2 in [performance experiments](performance-experiments.md).

- [ ] **P3: Benchmark recognition on fixed crops.** Recognizer oneDNN is the current CPU default; single four-frame pooling and four-thread trials did not improve recognition time. Measure warm batch sizes, actual padding and repeatable thread comparisons (including 10 versus 12) on fixed crops. Check output differences, memory and commit latency before adopting another variant. See experiments 3–4 in [performance experiments](performance-experiments.md).
