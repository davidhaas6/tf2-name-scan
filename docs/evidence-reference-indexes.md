# Evidence-reference indexes (ticket 2, 2026-10-02)

Schema v6 adds five partial indexes, excluding null paths:

- `text_clusters.evidence_path` and `frame_path`
- `observations.crop_path` and `legacy_evidence_path`
- `sampled_frames.full_frame_path`

New databases receive them through the normal migration chain. Opening a v5
database with `Store` first creates `results.pre-v5-migration.sqlite3`, then
creates the indexes and advances `user_version` in one transaction. Reopening
does not repeat the upgrade; the index statements also use `IF NOT EXISTS`.
The reference query and file ownership rules are unchanged. Profiled targeted
cleanup now records lookup time in `persistence_detail_s.reference_lookup`.

## Reproduce the measurements

```powershell
.venv/Scripts/python.exe eval/benchmark_evidence_indexes.py --corpus output --output output/ticket2-measurements --clip output/gameplay-smoke.mp4 --config config.yaml
```

Use a new output directory for each experiment. The script opens the source
SQLite database read-only and backs it up to a disposable snapshot; it never
opens the source with `Store`. The scan trials copy the evidence into the
snapshot and use links to those copies, falling back to file copies where
needed. Scanner saves replace files atomically and deletions unlink trial
files. The production corpus is preserved.

Lookup measurements use real path values from each column plus 50 absent paths,
three warm repetitions, and the exact query used by `prune_evidence_paths`.
The scaling variants duplicate only the three reference tables at 2x and 4x,
retaining path lengths and null density, with unique sample/geometry keys and
valid foreign keys. These are synthetic unrelated rows rather than additional
scanned videos. The fixed local scan is also compared at those larger sizes.
To reproduce the scaling scan pairs, add `--scan-multipliers 2 4 --scan-repeats 1`;
omit those options for three repeated pairs at the original size.

Write measurements alternate changing and restoring 1,000 non-null crop paths,
including each commit, for six transactions per variant. Index storage is the
change in occupied SQLite pages (`page_count - freelist_count`) when toggling
the five indexes. Freed pages stay in the database file; dropping an index
does not shrink that file. Index creation time excludes the migration backup.

The GPU scan experiment discards a warmup scan, reuses loaded models, and runs
three indexed/unindexed pairs in alternating order. It uses identical starting
database and evidence snapshots, local decoding, and the configured 1-fps
top-right detector/recognizer pipeline. Scan wall time includes startup orphan
recovery; model setup, snapshot copying, and migration are outside that timer.
Compare routine compaction separately because startup recovery remains a full
sweep and is outside ticket 2's optimization.

## Measurements

Measured on Windows with Python 3.10.11, SQLite 3.40.1, an RTX 3060 Ti (8 GiB,
driver 596.49), Paddle GPU 3.3.1, PaddleOCR 3.7.0, and PaddleX 3.7.2. The models
were cached `PP-OCRv6_small_det` and `PP-OCRv6_small_rec`. The working change
was based on commit `20cf108f7a9565b81b29d1bca29ede3dc9c8bb0b`.
Raw results are in ignored `output/ticket2-measurements-2/results.json`.
The source corpus remained at v5 during evaluation.

| Corpus | Clusters / observations / frames | Unindexed lookup, ms | Indexed lookup, ms | Unindexed 1,000 updates + commit, ms | Indexed 1,000 updates + commit, ms | Added index MiB | Index build, s |
|---|---|---:|---:|---:|---:|---:|---:|
| 1x | 27,950 / 51,643 / 10,884 | 30.940 | 0.00818 | 6.155 | 6.850 | 4.676 | 0.111 |
| 2x | 55,900 / 103,286 / 21,768 | 61.260 | 0.00801 | 5.401 | 8.637 | 9.344 | 0.266 |
| 4x | 111,800 / 206,572 / 43,536 | 120.718 | 0.00840 | 5.270 | 7.530 | 18.707 | 0.551 |

Values are medians of three warm query passes and six committed update
transactions respectively. Unindexed lookup cost grows approximately linearly;
indexed cost stays near 8 microseconds per check, about 3,780x faster at 1x.
The real-corpus index cost is 4,902,912 occupied bytes (about 11.8% of the
39.79-MiB starting database). Measured write overhead is 0.70–3.24 ms per 1,000
path changes across these corpora. This update-only microbenchmark does not
establish the overhead for every scanner insert or null-to-path transition.

The fixed input was `output/gameplay-smoke.mp4` (12 seconds), SHA-256
`44bf1785709e0832545cba8e18dd97acb766242ee0c9aba02e2783013572263e`.
The starting database snapshot SHA-256 was
`f6ee411a07b43953b00e78c2a21523c8a867062e21ff5a747fe43d4292706187`.
The six original-size scans all committed 12 timestamps (0–11), 18 observations,
16 clusters, and one persistence batch. Each compacted two candidate paths,
deleted exactly two files, and retained all representative evidence. Observation
hashes, including geometry, text, and OCR confidence, were identical in all six
runs (`7b60cbb178cf03b97904c571acd7be32b02707a8d037f163c3e1078f997665fc`).

| Original-size scan metric | Unindexed median (range) | Indexed median (range) |
|---|---:|---:|
| Routine compaction, s | 0.065 (0.063–0.068) | 0.003 (0.003–0.003) |
| Targeted reference lookup total, s | 0.062 (0.061–0.066) | <0.001 |
| Persistence transaction, s | 0.029 (0.028–0.035) | 0.035 (0.034–0.036) |
| Startup recovery, s | 30.022 (28.727–30.983) | 29.669 (29.652–30.010) |
| Scan wall, s | 30.722 (29.491–31.637) | 30.354 (30.323–30.700) |
| Scan RTF | 2.560 (2.458–2.636) | 2.529 (2.527–2.558) |

Routine compaction improves about 22x on this small workload. Overall scan RTF
does not show an established improvement: approximately 29–31 seconds of startup
cleanup dominates, and total timing ranges overlap. This short local clip checks
implementation cost and identical output; it is not a long online throughput
baseline. The earlier 719-check profile remains historical and was not rerun.

The larger corpora were also scanned as one additional warm pair each, with the
same evidence snapshot and local clip. Only reference-table row counts grow;
the referenced asset set stays fixed. All four scans reproduced the same
observation hash, sample grid, evidence counts, and zero missing representatives.

| Corpus | Unindexed compaction, s | Indexed compaction, s | Unindexed wall / RTF | Indexed wall / RTF |
|---|---:|---:|---:|---:|
| 2x | 0.115 | 0.002 | 28.065 / 2.339 | 27.901 / 2.325 |
| 4x | 0.295 | 0.002 | 30.265 / 2.522 | 29.060 / 2.422 |

Indexed routine compaction stays flat at these sizes while unindexed cost grows.
These single scaling pairs do not establish overall throughput gains or model
accuracy. Startup recovery still takes 27–29 seconds and is not bounded by the
changed batch. Ticket 3 addresses that separate remaining cost.

A subsequent full Admiralty replay reduces compaction from 1.668–1.701 seconds
in the two saved GPU baselines to 0.083 seconds (about 95%). Overall scan time
is longer because startup recovery has grown, while time excluding startup stays
within 0.7% of the baselines. See the full
[Admiralty comparison](scan-profiling-findings.md#admiralty-gpu-follow-up-after-ticket-2-2026-10-02)
for stage timings, coverage, and output checks.

## Correctness

`tests/test_evidence_indexes.py` checks fresh and populated v5 query plans,
idempotent upgrade/reopen, backup preservation, and migration failure rollback.
SQLite uses multi-index OR searches for the two cluster and two observation
columns, then a covering index search for frame paths, with no reference-table
scans. The same query protects shared references in every column until the last
owner's removal commits. Rolled-back removals preserve files. Unowned names,
absolute paths, traversal paths, and temporary files are excluded from targeted
cleanup. Existing scanner, interruption/resume, retention, and legacy migration
tests remain part of the full suite.
