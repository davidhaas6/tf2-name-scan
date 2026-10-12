# 7. Bound HTTP requests in streaming acquisition

Status: completed 2026-10-12. Approach selected 2026-10-12: native FFmpeg 9
bounded-request options (`-request_size` + `-multiple_requests`), not a seekable
reader. See [bounded streaming decision](../docs/bounded-streaming.md). Follow-up
to [acquisition/decode attribution](01-acquisition-decode-attribution.md), which
isolated unbounded HTTP requests as the dominant cause of the local/remote gap.
See [acquisition attribution](../docs/acquisition-attribution.md). Phase B proved
the end-to-end win and Phase C enabled bounded requests by default.

## Problem

The production scanner resolves a video-only media URL with yt-dlp, then lets
FFmpeg open that URL directly. FFmpeg 7.1 issues open-ended Range requests,
losing yt-dlp's roughly 10 MiB bounded-request policy. Under the production
request policy, identical encoded bytes that drain in about 3 seconds locally
take about 76 seconds to acquire remotely. The same-URL native-transfer control
(2.7 s bounded versus 76.3 s continuous, matching SHA-256) rules out decoding,
codec choice, disk writes, and selected format, and attributes about 95% of the
streaming scan-wall gap to acquisition waiting.

## Work

- Replace direct FFmpeg URL input with bounded HTTP requests. **Decided:** use the
  supported FFmpeg build's `-request_size` + `-multiple_requests` options; the
  diagnostic reader was only as fast and far more complex. See
  [bounded streaming decision](../docs/bounded-streaming.md). Require FFmpeg 9+
  with a clear capability error; do not fall back silently to unbounded requests.
- Preserve existing behavior: one upstream stream per host, chunk overlap,
  timestamp watermarks, complete 1-fps sample grids, EOF/short-source detection,
  retry/resume, and cancellation. With native options FFmpeg owns the HTTP
  lifecycle; Phase A must confirm persistent connections keep the production
  `-ss`/`-t` sections to one upstream connection per process.
- Do not copy the diagnostic proxy's permissive overlap into production. The
  native options replace the reader, so superseded-read cancellation is no longer
  hand-implemented; `acquisition.streams_per_host` keeps its `==1` validation and
  means one consumed media stream per host.
- Keep production sampling, model, region and reporting defaults unchanged until
  the end-to-end benchmark shows the measured win. Download-first remains a
  documented fallback for environments without FFmpeg 9.
- Change acquisition only; do not alter OCR, retention, or matching behavior.

## Acceptance

- A remote production scan of `GCN-CzALyq0` shows a measured end-to-end scan-wall
  and RTF improvement over the current unbounded baseline, using
  `eval/benchmark_scan.py compare` with fresh processes/corpora and matching
  media/settings.
- Sample-grid fingerprint and pixel/RGB hashes match the cached local reference,
  downloaded bytes hash identically, and `comparison.json` reports equivalence
  with no mismatch or incomplete-grid warnings.
- Interruption/resume, chunk-boundary overlap, and short/early-EOF handling still
  pass. Report the win against the streaming gap and full scan time, not against
  isolated diagnostic frame-drain timings.

## Outcome (2026-10-12)

Completed. Three fresh-process/fresh-corpus `benchmark_scan.py compare` pairs
with bounded requests enabled measured a remote scan wall of **16.857 s mean and
RTF 0.114**, down from the recorded unbounded baseline of 80.650 s and RTF 0.545.
Every pair reported `equivalent: true` with no warnings, complete 148-frame
grids, matching grid/RGB/cache hashes, and the 132-test suite passed with the
flag on. Persistent connections keep the production `-ss`/`-t` sections to one
probe connection plus one reused media connection (2 total, 0 reconnections),
independent of section start/length and request count. The default is now on;
FFmpeg 9+ is required. Details: [bounded streaming decision](../docs/bounded-streaming.md).
