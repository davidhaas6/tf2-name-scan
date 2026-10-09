# 7. Bound HTTP requests in streaming acquisition

Status: open. Follow-up to [acquisition/decode attribution](01-acquisition-decode-attribution.md),
which isolated unbounded HTTP requests as the dominant cause of the local/remote
gap. See [acquisition attribution](../docs/acquisition-attribution.md).
Production defaults are unchanged until this ticket delivers a measured win.

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

- Replace direct FFmpeg URL input with an acquisition path that uses bounded HTTP
  requests. Evaluate either a supported FFmpeg build exposing bounded-request
  options or a seekable yt-dlp-backed reader, per the attribution
  [recommendation](../docs/acquisition-attribution.md#recommendation-and-limits).
  Pick one approach and document why.
- Preserve existing behavior: one upstream stream per host, chunk overlap,
  timestamp watermarks, complete 1-fps sample grids, EOF/short-source detection,
  retry/resume, and cancellation. A plain yt-dlp-to-stdin pipe is not a drop-in
  replacement for the current input seeks.
- Cancel superseded reads explicitly and enforce the upstream concurrency limit.
  The diagnostic proxy permits brief overlapping responses during FFmpeg seeks;
  that is acceptable for attribution, not for production.
- Keep production sampling, model, region and reporting defaults unchanged. If
  bounded streaming is not supportable, download-first is a measured fallback.
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
