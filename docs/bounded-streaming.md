# Bounded streaming acquisition: FFmpeg 9 request_size decision (2026-10-12)

Resolution of [ticket 7](../tickets/07-bounded-http-acquisition.md), the follow-up
to [acquisition attribution](acquisition-attribution.md). The attribution isolated
unbounded HTTP Range requests as the dominant local/remote gap and left two
candidates: a supported FFmpeg build with bounded-request options, or a seekable
yt-dlp-backed reader. **This document selects the FFmpeg build option** and records
the measurements behind that choice.

The installed FFmpeg is now **9.0.2 essentials (gyan)**. FFmpeg 9 exposes both
`-request_size` and `-initial_request_size` on the `http` protocol, so the
installed attribution binary's original limitation (FFmpeg 7.1 did not expose
`request_size`) no longer applies. The option is absent from FFmpeg 7.1, 7.1.1 and
8.0; **FFmpeg 9.0 or newer is required.**

## Measurements

Diagnostic frame drains on video `GCN-CzALyq0` (format 298, video-only H.264 MP4,
1280×720 at 60 fps, catalog duration 148 s, 46,281,243 bytes). Each treatment
drains all eligible timestamps 0–147 at 1 fps with the production filter chain and
hashes every RGB frame. These are acquisition/decode times, not scan times; the
end-to-end acceptance remains `benchmark_scan.py compare` (see
[scan benchmark](scan-benchmark.md)).

| Treatment | Wall, s | Frames | RGB SHA-256 |
|---|---:|---:|---|
| Local cache (fast-input control) | 2.50 | 148 | `6d703bbf…` |
| Direct URL, no options (baseline) | 78.86 | 148 | `6d703bbf…` |
| `-multiple_requests 1` only | 78.79 | 148 | `6d703bbf…` |
| `-request_size 10485760` | 15.88 / 15.81 | 148 | `6d703bbf…` |
| `-request_size 10485760 -initial_request_size 10485760` | 15.83 | 148 | `6d703bbf…` |
| **`-request_size 10485760 -multiple_requests 1`** | **7.04 / 7.34 / 7.16** | 148 | `6d703bbf…` |
| `-request_size 3000000 -multiple_requests 1` | 7.23 | 148 | `6d703bbf…` |
| Loopback bounded reader (diagnostic proxy) | 7.11 / 7.12 | 148 | `6d703bbf…` |
| yt-dlp native transfer (same-URL control) | 2.68 / 2.67 | whole file | cache hash matches |

Every bounded/baseline drain reproduces the local reference exactly, including the
benchmark grid fingerprint `4676878a…` and the RGB hash
`6d703bbf12f2dfd79c9ed41bf817fce91ad13fdfd208a31f10b6ddfc2c89e73e` recorded in
[acquisition attribution](acquisition-attribution.md).

Attribution of the fix:

- `-multiple_requests 1` alone leaves the run at 78.79 s: connection reuse does not
  help while each resource request is still open-ended.
- `-request_size` alone drops 78.9 s to 15.8 s: the request policy is the dominant
  cause, consistent with the earlier native-transfer control.
- Adding `-multiple_requests 1` drops 15.8 s to about 7.1 s: without persistent
  connections FFmpeg pays a new connection per bounded Range request. Together the
  two options match the loopback reader.
- `-initial_request_size` makes no measurable difference here; it is not adopted.
- `-request_size` 3 MiB and 10 MiB are effectively equal; 10 MiB matches yt-dlp's
  existing `http_chunk_size` policy and is the proposed default.

Raw debug output confirms the request shape. Baseline: `Range: bytes=0-` then
`Range: bytes=5504-`. Bounded: `bytes=0-9999999`, `bytes=5504-10005503`,
`bytes=10005504-20005503`, … — the same bounded sequence the diagnostic proxy
produced.

One early variant sweep returned truncated drains (28/43/0 frames) before the
`-multiple_requests` measurement. That run was transient: six subsequent
`-request_size`/persistent runs all returned 148/148 frames with identical pixels
and clean exit codes. It is recorded here rather than treated as a result.

## Decision

**Use FFmpeg's native bounded-request options and do not build a production
loopback reader.** For `http(s)` inputs, `sample_frames` adds
`-request_size <bytes> -multiple_requests 1` alongside the existing
`-rw_timeout` and `-headers`; local, file and fixture inputs are unchanged.

Why this over the seekable yt-dlp-backed reader:

- Equal speed. The reader measured 7.1 s; the two flags measure 7.0–7.3 s.
- Far less machinery. The reader prototype required a threaded loopback HTTP
  server, per-request cancellation of superseded reads, a host-keyed concurrency
  limit, write-timeout handling so a blocked local write can be interrupted, and a
  teardown ordering contract. The flags require none of that: FFmpeg owns the HTTP
  lifecycle, and persistent connections avoid the second-connection seek behavior
  the diagnostic proxy had to permit.
- A supported, versioned dependency. `request_size` is released in FFmpeg 9.

`acquisition.streams_per_host` keeps its current `==1` validation. Its meaning is
"one consumed media stream per host"; with native acquisition FFmpeg owns the
connection and the scanner still runs one FFmpeg process per chunk.

## Requirements and compatibility

- **FFmpeg 9.0+** is required when bounded acquisition is enabled. The code probes
  `ffmpeg -hide_banner -h protocol=http` for `request_size` (cached) and raises a
  clear error when the option is absent. There is no silent unbounded fallback.
- The bounded-request parameters are configuration
  (`acquisition.bounded_requests`, `request_size_bytes`, `persistent_connections`).
  Because acquisition settings enter the effective-scan identity snapshot, enabling
  them changes the scan hash; scans run before and after the flip must not be
  compared as equivalent work.
- Production defaults stay unbounded until the end-to-end benchmark shows the
  measured win, then the default flips.

## Open validation

End-to-end `benchmark_scan.py compare` with fresh processes/corpora and matching
media/settings, repeated at least three times, is the acceptance gate. The
frame-drain numbers above are not scan speedups. Phase A must additionally confirm
that persistent connections keep the production `-ss`/`-t` section shape to a
single upstream connection per process; the second-connection seek behavior was
measured on FFmpeg 7.1 without persistence.

The cached AV1 video `tFwLwEcSTwc` (format 398) is no longer resolvable through
yt-dlp, so AV1 comparison needs a fresh cache or should be deferred; the default
`GCN-CzALyq0` comparison is unaffected.

## Evidence

Scratch probes retained locally under `output/` (gitignored): `ff9_probe.py`,
`head2head.py`, `ff9_repeat.py`, `ff9_attrib.py`, `ff9_variants.py`,
`av1_probe.py`. The diagnostic `eval/attribute_acquisition.py` `bounded_proxy`
remains available for attribution experiments only; it is not production code.

Installed software for these measurements: FFmpeg 9.0.2 essentials (gyan),
yt-dlp 2026.08.19.
