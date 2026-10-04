# 3. Bound startup evidence recovery

Status: open. Implement after targeted frame-delivery optimization.

## Problem

Crash-orphan recovery runs once per open store but gathers references and walks
the entire evidence corpus. Startup cleanup took 7.338 seconds in the Admiralty
GPU repeat and 20.083 seconds in the coverage recheck. This cost grows with the
corpus and disproportionately affects short scans and restarts.

## Work

- Persist which scanner-owned evidence directories may need recovery before
  writing files; clear that state only after safe reconciliation.
- Recover affected directories at startup, including interrupted transactions
  and temporary files, without walking unrelated clean directories.
- Retain a full sweep for maintenance and legacy/untracked corpus recovery.

## Acceptance

Verify crashes before/after file writes and commits, cancellation, shared
references, and path ownership. No referenced evidence is deleted and recoverable
orphans are removed. Measure startup time against increasing clean corpus size;
routine recovery should scale with affected work rather than total corpus size.
