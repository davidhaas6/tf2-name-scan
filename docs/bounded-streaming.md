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

## Phase B: end-to-end scan benchmark (2026-10-12)

With `acquisition.bounded_requests: true` (10 MiB `request_size_bytes`, persistent
connections on) in the local config, three fresh-process/fresh-corpus comparisons
were run, alternating after short pauses:

```powershell
uv --cache-dir .uv-cache run --no-sync python eval/benchmark_scan.py compare --label bounded-requests
```

`GCN-CzALyq0`, format 298, 148 s, unchanged sampling/detector/recognizer/GPU
settings. The local arm is a matched control over the validated cache; only the
stream arm acquires the resolved URL. Each pair reused the published cache
(`cache download 0.000 s`).

| Run | Local wall, s | Stream wall, s | Local RTF | Stream RTF | Stream−local, s | Stream decoding, s |
|---|---:|---:|---:|---:|---:|---:|
| `a24395ab` / `6a8c9cfb` | 11.941 | 16.833 | 0.081 | 0.114 | +4.892 | 5.172 |
| `bd661779` / `109da731` | 12.174 | 16.907 | 0.082 | 0.114 | +4.733 | 5.217 |
| `9337d88e` / `0bb7c90c` | 10.826 | 16.831 | 0.073 | 0.114 | +6.005 | 5.174 |
| **Mean** | **11.647** | **16.857** | **0.079** | **0.114** | **+5.210** | **5.188** |

Against the recorded unbounded production pair in
[acquisition attribution](acquisition-attribution.md) (local 12.258 s / stream
80.650 s, RTF 0.083 / 0.545), the remote scan wall fell from 80.650 s to a
16.857 s mean and RTF from 0.545 to 0.114. The stream−local decoding gap fell
from 65.184 s to about 4.35 s per run. The bounded stream arm is now within about
1.45× the local control instead of about 6.6×.

Verification for every run:

- `comparison.json` reports `equivalent: true` with no warnings; `effective_scan`,
  media and sample grids match between arms.
- All three stream runs and the local arms have the same complete 148-frame grid
  fingerprint as the recorded baseline: `4676878a0f2e4ab19f9675c7ddce1e95919912c21656c770ff1a16393a6998f1`.
- A production-path drain of the cached file and of the resolved URL with
  `sample_frames(..., bounded_requests=True, request_size_bytes=10485760,
  persistent_connections=True)` both reproduce 148 frames, RGB SHA-256
  `6d703bbf12f2dfd79c9ed41bf817fce91ad13fdfd208a31f10b6ddfc2c89e73e` and grid
  SHA-256 `4676878a…`.
- The cached file is 46,281,243 bytes and its SHA-256 matches the published
  metadata used by the local arm.
- The full test suite (132 tests) passes with `acquisition.bounded_requests`
  forced on by default, covering resume/overlap/watermark, early-EOF coverage
  checks, decoder termination, and the real-FFmpeg detection/operations tests.

These are three ordered pairs, not a randomized throughput estimate.

### Persistent-connection section shape

The open validation is resolved. FFmpeg debug `Statistics:` lines for
production-shaped `-ss`/`-t` sections show:

| Treatment | Connections | Requests | Reconnections |
|---|---:|---:|---:|
| Unbounded `-ss 0/60 -t 30` | 2 | 2 | 0 |
| `-request_size` only | 3 | 3 | 0 |
| `-request_size` + `-multiple_requests 1` | 2 | 2–6 | 0 |

Without persistence every bounded Range request opens its own connection
(connections track requests). With `-multiple_requests 1`, all post-probe media
requests share one connection and the connection count stays at **2** regardless
of `-ss` (0, 60, 120 s), section length, or the number of bounded requests: one
initial header/probe connection plus one reused media connection. There are no
reconnections. The earlier second-connection seek behavior does not recur per
section; it is limited to FFmpeg's initial probe on 9.0.2.

Known follow-up (pre-existing, out of scope here): remote `dimensions` can be
absent, in which case `sample_frames` calls `probe()` and sends `ffprobe` to the
remote URL without the bounded options. That path still uses unbounded requests
and should be brought under the same acquisition policy later.

## Evidence

Scratch probes retained locally under `output/` (gitignored): `ff9_probe.py`,
`head2head.py`, `ff9_repeat.py`, `ff9_attrib.py`, `ff9_variants.py`,
`av1_probe.py`, and the Phase B checks `phaseb_check.py` (RGB/grid/cache hashes)
and `phaseb_connections.py` (connection-shape counts). The diagnostic
`eval/attribute_acquisition.py` `bounded_proxy` remains available for attribution
experiments only; it is not production code.

Installed software for these measurements: FFmpeg 9.0.2 essentials (gyan),
yt-dlp 2026.08.19.
