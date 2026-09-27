# Near-term tasks

This is a lightweight task tracker for the next practical checks, not a complete implementation plan.

- [x] **Profile one representative scan.** Full-frame and top-right runs, plus detailed persistence timings, are recorded in [scan profiling findings](scan-profiling-findings.md).

- [ ] **Complete the full-frame versus top-right comparison.** Timing is documented in [scan profiling findings](scan-profiling-findings.md); manually compare player-name coverage before drawing an accuracy conclusion. Investigate the unmeasured startup/final cleanup portion of `other` and confirm persistence behavior on a longer scan if performance work proceeds.

- [ ] **R12: Build a small verified video set.** Manually label visible username occurrences and negative regions in one short, exhaustively checked video window; record timestamps, exact names, and stable occurrence IDs. Add a second window from a different HUD or compression setting, keeping each source separate and marking any unlabelled regions as unknown rather than negative.

- [ ] **R13: Evaluate the production pipeline on those windows.** Compare scanner hits with the labels at the occurrence and video level, and record missed names, false candidates, duplicate hits, and review workload. Report counts and coverage alongside recall and false-hit rates; use the failures to decide whether to expand the dataset, fix accuracy, or optimize the measured bottleneck.
