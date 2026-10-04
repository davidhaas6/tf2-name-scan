# Ingestion readiness audit (2026-10-01)

Read-only inspection of `output/results.sqlite3` found newer GPU runs than the
2026-09-28 CPU measurements in [scan profiling findings](scan-profiling-findings.md).
The current scanner uses GPU detection and recognition, top-right sampling at
1 fps, and 600-second chunks. The older CPU findings remain historical evidence.

## Longer runs already completed

Nine online scans completed on 2026-09-29, each longer than ten minutes. All nine
have exactly one stored sample at every integer second from zero through the
last expected second, including across chunk boundaries.

| Scan | Video | Duration | Chunks | Samples | Elapsed wall | Wall / video |
|---|---|---:|---:|---:|---:|---:|
| 23 | Demoman | 1,360 s | 3 | 1,360 | 698 s | 0.513 |
| 24 | Sniper | 758 s | 2 | 758 | 940 s | 1.240 |
| 25 | Sniper extras | 874 s | 2 | 874 | 505 s | 0.578 |
| 26 | Scout | 696 s | 2 | 696 | 354 s | 0.509 |
| 27 | Spy | 610 s | 2 | 610 | 308 s | 0.505 |
| 28 | Pyro extras | 1,041 s | 2 | 1,041 | 528 s | 0.507 |
| 29 | Engineer | 659 s | 2 | 659 | 338 s | 0.513 |
| 31 | Pyro | 1,051 s | 2 | 1,051 | 539 s | 0.513 |
| 32 | Soldier | 606 s | 2 | 606 | 306 s | 0.505 |

Durations are catalog values. Elapsed wall is `video_scans.started_at` through
`completed_at`, rounded to seconds by SQLite. It includes interruption time and
per-video acquisition, but excludes model construction before ingestion. The
combined 7,655 video seconds took 4,516 wall seconds (RTF 0.590). Excluding the
interrupted Sniper run gives 6,897 video seconds in 3,576 wall seconds (RTF 0.518).
These are operational measurements, not controlled repeated benchmarks or name
accuracy measurements. They establish that longer scans and chunk boundaries
have already been exercised; another pilot solely to exercise them is redundant.

## Interruption and queue continuation

The user reports interruption/resume is working after changes. Scan 24 supports
that report: its first chunk has three abandoned attempts, followed by a
successful attempt starting at 430 seconds. The next chunk starts decoding at
597 seconds for overlap. The final stored sample grid is complete and unique.
The abandoned attempts have no completion time or elapsed duration, so summing
successful attempt timings would substantially understate this run's wall time.

Scan 30 (Heavy) failed with an HTTP 403 and zero stored frames; scans 31 and 32
then completed. Queue continuation after a failed video is therefore also
demonstrated. The database does not establish the cause of that HTTP rejection
or whether retries with newly resolved URLs would succeed.

## Scan 15's short coverage

The saved scan for `4iLIfCzToFY` requested 0–477 seconds, but stores only samples
0–454 (455 frames). Its sole attempt has no error and the chunk is completed.

On 2026-10-01, a fresh yt-dlp resolution still reported 477 seconds. FFprobe of
the selected 720p format 136 reported 476.966667 seconds and a zero start time.
Decoding a fresh 450–477-second section returned all 27 expected samples through
second 476. A full decode likewise returned 477 samples spanning seconds 0–476.
Both checks used the production sampler with a two-pixel output height to avoid
unnecessary image transfer; OCR was not involved. The current source therefore
contains the missing tail. The old
attempt did not retain its selected format or decoder diagnostics, so the
precise historical reason for the short decode cannot be reconstructed.

Previously, successful FFmpeg exit plus any decoded frames was sufficient to
complete a chunk. Bounded decoding now checks its sample count after successful
process exit and raises an early-EOF error for a shortfall greater than one
sample. One sample is tolerated because FFmpeg's fps filter can round away a
final sample at EOF. The existing retry/resume path then preserves committed
work and retries the missing tail instead of silently completing the chunk.
This guard applies to bounded actual decoding; intentional sample limits and
injected fixture sources retain their existing behavior.

Validation: a real short local file fails its scan, preserves committed frames,
and resumes to complete unique coverage after the source is restored. Clean
decoder exits with materially short output fail; complete output and one-sample
EOF rounding pass. The full suite passes: 103 tests, with Ruff passing.

The video was reprocessed on 2026-10-01 with the current GPU configuration and
the coverage guard. The new selected generation is scan 33: all 477 unique
timestamps 0–476, zero foreign-key violations, and all 1,518 representative
evidence files present. Original scan 15 remains intact as historical lineage;
default queries now select scan 33, while old derived hits remain with their
original generation until matching is rerun.

`output/coverage-recheck-profile.json` records 269.756 scan-wall seconds
(RTF 0.566), plus 18.301 seconds of model setup. The scan processed 4,432 crops.
Its stages were decoding 175.990 s, recognition 31.703 s, persistence/evidence
23.264 s, startup cleanup 20.083 s, detection 11.632 s, crop preparation 3.909 s,
and other 3.176 s. This reinforces acquisition/decode and corpus-startup cost as
the next places to investigate if faster channel processing is needed.

## Remaining useful work before channel scale

- Measure database/evidence growth per video-hour and cleanup cost against a
  larger corpus, using the existing scans rather than repeating the pilot.
- Evaluate name accuracy on the verified occurrence set when labels are ready.
- Treat HTTP 403 handling as a distinct acquisition issue if it recurs; resume
  correctness and queue continuation are already demonstrated.

Read-only evidence inventory for these nine runs found 14,891 unique referenced
files totaling 63,995,058 bytes (61.03 MiB, or about 28.70 MiB per video-hour),
with no missing files. This counts retained crops/frame evidence, not temporary
downloads or SQLite storage. The whole current SQLite file is 39.79 MiB and
contains other generations and legacy records, so its total is not a per-hour
measurement for this set. Cleanup scaling as unrelated corpus size grows remains
unmeasured.

Further runtime tuning should follow current GPU stage profiles, rather than
the older CPU detector oneDNN task. `output/admiralty-perf-gpu-2.json`, for
example, reports decoding as the largest measured stage (64.903 of 87.742 scan
seconds), with detection at 4.062 seconds and recognition at 6.438 seconds.

The next optimization work is scoped in the root [tickets folder](../tickets/),
with its implementation order and checkboxes in [todo](todo.md). These tickets
cover acquisition/decode attribution, evidence-reference indexes, bounded startup
recovery, and moving the existing region crop before pipe transfer.
