# Near-term tasks

This is a lightweight task tracker for the next practical checks, not a complete implementation plan.

- [x] **Profile one representative scan.** Full-frame and top-right runs, plus detailed persistence timings, are recorded in [scan profiling findings](scan-profiling-findings.md).

- [ ] Investigate or fix performance issues to get the scanner to be near real time

- [ ] **R12: Build a small verified video set.** Manually label visible username occurrences and negative regions in one short, exhaustively checked video window; record timestamps, exact names, and stable occurrence IDs. Add a second window from a different HUD or compression setting, keeping each source separate and marking any unlabelled regions as unknown rather than negative.

- [ ] **R13: Evaluate the production pipeline on those windows.** Compare scanner hits with the labels at the occurrence and video level, and record missed names, false candidates, duplicate hits, and review workload. Report counts and coverage alongside recall and false-hit rates; use the failures to decide whether to expand the dataset, fix accuracy, or optimize the measured bottleneck.

# Maybe

- [ ] **Explore reusing OCR for unchanged text.** Measure exact crop matches across nearby frames first; if rare, test position-aware image similarity with periodic fresh OCR. Compare missed name changes and runtime on verified clips before adopting it.

- [ ] **P1: Complete performance attribution and longer baselines.** Measure startup/final cleanup, first versus warm inference, actual OCR batch occupancy, CPU use, memory and commit latency. Repeat short and longer scans against identical starting corpora. Use RTF <= 1 at 1 sampled fps as the first throughput milestone. See [performance experiments](performance-experiments.md).

- [ ] **P2: Separate batch commits from global evidence cleanup.** Compare compaction enabled/disabled, then prototype incremental retention/deletion with explicit orphan recovery. Measure scaling with unrelated corpus size and verify evidence references and interrupted-scan recovery. See experiment 2 in [performance experiments](performance-experiments.md).

- [ ] **P3: Benchmark recognition on fixed crops.** Measure warm batch sizes, actual padding, cross-frame pooling and supported CPU runtime/thread settings. Carry successful variants back to end-to-end scans; preserve crop association and record accuracy, memory and commit latency. See experiments 3–4 in [performance experiments](performance-experiments.md).
