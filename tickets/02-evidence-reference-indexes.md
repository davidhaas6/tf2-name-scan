# 2. Index evidence-reference lookups

Status: complete (2026-10-02). Implementation and measurements are in
[evidence-reference indexes](../docs/evidence-reference-indexes.md).

## Problem

`Store.prune_evidence_paths` scans `text_clusters`, `observations`, and
`sampled_frames` for every removed path. The coverage recheck performed 719 checks
and spent 19.284 seconds in compaction. The current corpus has 27,950 clusters,
51,643 observations, and 10,884 frames; warm reference checks took 30–36 ms.

An in-memory copy measured about 12.5 ms per unindexed check versus
0.0035–0.0042 ms with five partial indexes. This isolates lookup improvement,
not end-to-end scanner speed.

## Work

- Add indexes for non-null `text_clusters.evidence_path` and `frame_path`,
  `observations.crop_path` and `legacy_evidence_path`, and
  `sampled_frames.full_frame_path`.
- Apply the indexes to new and existing databases through an idempotent upgrade.
- Compare query plans, compaction time, write overhead, and scan RTF on fixed
  inputs/corpora, including increasing unrelated corpus size.

## Acceptance

Reference checks use indexes rather than whole-table scans. Shared references,
legacy evidence, and scanner-owned path protections remain correct. Record the
measured compaction improvement and any index storage/write cost.
