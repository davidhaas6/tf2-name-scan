# Near-term tasks

This is a lightweight task tracker for the next practical checks, not a complete implementation plan.

- [ ] **Profile one representative scan.** Run a cached local video and record wall time, video duration, hardware, resolution, sampling rate, and effective configuration. Measure time spent in decoding, detection, recognition, and persistence/evidence, then identify the largest bottleneck before changing performance code.

- [ ] **R12: Build a small verified video set.** Manually label visible username occurrences and negative regions in one short, exhaustively checked video window; record timestamps, exact names, and stable occurrence IDs. Add a second window from a different HUD or compression setting, keeping each source separate and marking any unlabelled regions as unknown rather than negative.

- [ ] **R13: Evaluate the production pipeline on those windows.** Compare scanner hits with the labels at the occurrence and video level, and record missed names, false candidates, duplicate hits, and review workload. Report counts and coverage alongside recall and false-hit rates; use the failures to decide whether to expand the dataset, fix accuracy, or optimize the measured bottleneck.
