# Why direct FFmpeg streaming is slow: unbounded HTTP requests (2026-10-03)

The dominant delay is **HTTP media delivery under large continuous requests**.
The production scanner resolves a media URL with yt-dlp, but FFmpeg then opens
that URL itself. It loses yt-dlp's roughly 10 MiB bounded-request policy. Giving
the same URL to yt-dlp's native downloader is fast; disabling only that policy
makes the native downloader nearly as slow as FFmpeg. This isolates acquisition
from decoding, OCR, disk writes, and the choice of codec.

This is evidence of request-dependent upstream pacing/throttling, not a claim
about YouTube's undocumented internal rate-control algorithm. Measurements apply
to these videos, this connection, and the installed software.

## Production baseline and timing boundaries

Ran the prescribed fresh-process/fresh-corpus benchmark:

```powershell
uv --cache-dir .uv-cache run --no-sync python eval/benchmark_scan.py compare --label acquisition-attribution-baseline
```

The uv cache override and `--no-sync` use the installed Paddle environment. Both
runs used `GCN-CzALyq0`, format 298, video-only H.264 MP4, 1280×720 at 60 fps,
catalog duration 148 s, and the unchanged GPU configuration. Code revision was
`2978757bd14597e1de3db8a157ef7edee8ef393a`; the benchmark tree was initially clean.
Later diagnostic runs include the new evaluation helper as uncommitted work.

| Measurement | Local | Direct stream |
|---|---:|---:|
| Scan wall, s | 12.258 | 80.650 |
| Scan RTF | 0.083 | 0.545 |
| Decoding / frame waiting, s | 0.852 | 66.036 |
| Detection, s | 4.149 | 3.912 |
| Recognition, s | 4.990 | 6.779 |
| Model setup, s (outside scan) | 5.982 | 5.793 |
| Whole per-run command, s | 19.548 | 87.681 |

Runs `20261004T031948.958485Z-local-16892081` and
`20261004T032008.509722Z-stream-56894ada` have matching settings/models/media and
complete identical 148-sample grids. `comparison.json` reports equivalence with
no warnings. The UTC IDs correspond to October 3 in America/New_York.
65.184 s of the 68.392 s scan-wall difference is frame waiting: **95.3%**.
This pair reused the validated cache, so it does not measure a new download.

The earlier [matched comparisons](scan-benchmark.md) did include initial download:
35.628 versus 86.954 s total for this video, and 129.720 versus 382.729 s for
the 736-second AV1 video `tFwLwEcSTwc`. That is why the download advantage survives
even when download time is included. These are individual ordered scan pairs,
not a randomized throughput estimate.

An initial sandboxed comparison failed during remote resolution because the
sandbox's proxy rejected network access. Its stream run
`20261004T031912.213465Z-stream-ebac032e` has no decoded frames and is excluded
from performance comparisons. The successful pair above ran with network access.

## Acquisition control experiment

`eval/attribute_acquisition.py` is a focused diagnostic companion to the scan
benchmark. It validates the existing cache, pins its exact format, and resolves
one URL shared by all treatments in a run. It retains safe numerical diagnostics
under `output/benchmarks/attribution/`; URLs and full FFmpeg debug logs are omitted.
It does not load OCR models or open the production corpus.

```powershell
uv --cache-dir .uv-cache run --no-sync python eval/attribute_acquisition.py --repeat 2 --steps local remote chunked unchunked
```

FFmpeg drains mirror the production input seek/duration, video mapping, fps and
scale filters, RGB pipe, section boundaries, and overlap. The consumer immediately
reads and hashes all eligible RGB frames. Logging/CPU accounting and hashing add
diagnostic overhead; these are decoder/acquisition timings, not scan timings.
Transfer timings exclude resolution and hash verification and include disk writes.

For both transfers, the client, URL, selected media, headers, output destination
policy, and retry setting are identical. Only the native downloader's
`http_chunk_size` differs: 10 MiB versus zero. yt-dlp randomizes each normal range
between 95% and 100% of that bound. The disabled treatment makes one full-file
request with no Range header. Both transfer the entire **46,281,243-byte** file,
and all four downloaded SHA-256 values exactly match the published cache.

| Treatment | Repeat 1, s | Repeat 2, s |
|---|---:|---:|
| Local FFmpeg drain | 2.964 | 2.976 |
| Direct remote FFmpeg drain | 78.875 | 78.812 |
| Native transfer, bounded ranges | 2.738 | 2.727 |
| Same native transfer, continuous request | 76.307 | 76.309 |

The bounded transfer is about **28× faster** than the continuous transfer, despite
making five requests instead of one. This comparison holds decoding, OCR and
format selection out of the experiment entirely. It also rules out an inherent
network bandwidth limit at the slower rate. The improvement survives the second
repeat; both treatments reuse the same URL throughout.

Direct FFmpeg debug output shows `Range: bytes=0-` and then
`Range: bytes=5504-`: a normal initial demuxer seek followed by an **unbounded**
request. Each drain reads about 46.4 MB and makes one demuxer seek. There is no
per-sample HTTP request storm or substantial duplicate full-file transfer here.
The normal scan's 600-second time sections are different from HTTP byte chunks:
a bounded time section does not bound the size of each HTTP request.

Both local and remote drains emit all timestamps 0–147. Their timestamp hashes
match the scan benchmark, and every drain's RGB hash is identical:
`6d703bbf12f2dfd79c9ed41bf817fce91ad13fdfd208a31f10b6ddfc2c89e73e`.
Removing OCR therefore preserves the gap while preserving the decoded pixels.

FFmpeg's summed user+system CPU time is 23.485/23.797 s locally and
16.203/22.125 s remotely. This is cumulative multithreaded CPU time, not wall time.
The process averages roughly 8 CPU cores locally but less than 0.3 core remotely,
consistent with upstream waiting. Sampled peak working sets are roughly 15 MiB
for both paths; no growing buffer is needed to explain the difference.
First-frame delivery is 0.100–0.101 s locally and 5.670–5.728 s remotely, but most
of the remote delay continues after the first frame. It is not just model startup
or a slow initial probe.

Raw results:
`output/benchmarks/attribution/20261004T032258.762383Z-GCN-CzALyq0/result.json`.
Installed software: FFmpeg 7.1 essentials build, yt-dlp 2026.08.19, psutil 7.2.2.
The format advertises HTTPS delivery, 2,499.729 kbit/s and
`downloader_options.http_chunk_size = 10485760`.

## Why the implementation differs

- `download.resolve_media()` asks yt-dlp to select/resolve video-only media.
  `stream_details()` returns URL, duration, dimensions and HTTP headers, but not
  the downloader options. No yt-dlp media downloader runs during scanning.
- `detected_ingestion.ingest_detected()` hands that URL to `frames.sample_frames()`.
  FFmpeg performs HTTP acquisition, demuxing, software decoding, sampling and pipe
  output. Its work already overlaps OCR; `ScanProfile.decoding` measures time
  inside the Python frame iterator, not all decoder CPU time.
- `scan_benchmark.ensure_cache()` invokes yt-dlp with `download=True`. Its native
  YouTube downloader honors the selected format's bounded byte requests. FFmpeg
  subsequently reads a fast local file.
- The high-level design illustrates a yt-dlp-media pipe into FFmpeg. The current
  implementation uses yt-dlp for resolution followed by direct FFmpeg HTTP input;
  those acquisition paths have materially different request behavior.

The installed [yt-dlp YouTube extractor](https://github.com/yt-dlp/yt-dlp/blob/2026.08.19/yt_dlp/extractor/youtube/_video.py)
sets the 10 MiB policy, and its [HTTP downloader](https://github.com/yt-dlp/yt-dlp/blob/2026.08.19/yt_dlp/downloader/http.py)
turns it into bounded Range headers. FFmpeg 7.1's
[HTTP implementation](https://github.com/FFmpeg/FFmpeg/blob/n7.1/libavformat/http.c)
defaults to open-ended ranges. Current [FFmpeg HTTP documentation](https://ffmpeg.org/ffmpeg-protocols.html#http)
describes a `request_size` option specifically for servers that throttle
unbounded requests, but **the installed 7.1 binary does not expose that option**,
as confirmed by `ffmpeg -hide_banner -h protocol=http`.

## Bounded streaming experiment

The helper's `proxy` treatment exposes a seekable localhost HTTP input to FFmpeg
and translates its open-ended reads into upstream ranges of at most 10,000,000
bytes. Media passes through 64 KiB read buffers; no video file is saved. This
tests the request policy with the existing decoder and filters. It is a diagnostic
adapter, not an integrated production acquisition mode.

```powershell
uv --cache-dir .uv-cache run --no-sync python eval/attribute_acquisition.py --steps proxy local --repeat 2
uv --cache-dir .uv-cache run --no-sync python eval/attribute_acquisition.py --video tFwLwEcSTwc --steps local proxy chunked --repeat 2
```

On H.264, bounded streaming drains in **7.773/7.569 s**, with all 148 samples and
exactly the same RGB hash as the local and direct-stream drains. The local
controls in this later invocation take 3.190/3.277 s. This shows that a full-file
disk download is not intrinsically necessary for fast delivery. The roughly
79-second direct drains were in the preceding resolved-URL run, so the cross-run
ratio is illustrative; the same-URL native transfer control above is the stronger
causal test. Retained results:
`output/benchmarks/attribution/20261004T033318.881283Z-GCN-CzALyq0/result.json`.

The longer AV1 run pins format 398, 1280×720 at 30 fps, HTTPS, advertised bitrate
1,546.432 kbit/s and file size 142,187,883 bytes. It uses two FFmpeg sections,
0–600 and 597–736 seconds, retaining the production 3-second overlap.

| AV1 treatment | Repeat 1, s | Repeat 2, s |
|---|---:|---:|
| Local drain | 63.965 | 66.555 |
| Bounded streaming drain | 66.296 | 67.791 |
| Native bounded transfer (entire file) | 2.921 | 1.813 |

Every drain has all 736 eligible timestamps, the benchmark's grid fingerprint,
and identical RGB hash:
`e31054d05f86620c0221b7d36d9b774595b86543642a782e2d5617316561e817`.
Both full transfers match the cache hash. FFmpeg CPU totals are 216.359/222.125 s
locally and 214.828/221.906 s through the proxy; sampled peak working sets remain
about 15 MiB. Fast input exposes **roughly 64–68 seconds of AV1 decoding/filtering
and RGB delivery**, whereas the short H.264 workload drains in a few seconds.
Thus codec cost is real, but it cannot explain why identical encoded input takes
so much longer when acquired with the production request policy.

The historical direct-stream AV1 scan had 312.181 s of frame waiting and 375.457 s
of total scan wall. No new direct AV1 scan or continuous AV1 transfer was run in
this investigation, so those older numbers are context rather than a matched
speedup estimate against the new proxy. Fresh native AV1 download time also
differs from the earlier 40.359 s cache-preparation time: network route/server
state and the inclusion of resolution/validation can change absolute timing.
The H.264 same-URL control is what isolates the request policy.

AV1 results:
`output/benchmarks/attribution/20261004T033353.642535Z-tFwLwEcSTwc/result.json`.

FFmpeg opens a new seek response before it closes the old one. An initial
single-threaded proxy therefore stalled during that startup seek and emitted
**zero frames**. Those attempts have incomplete-grid flags and are excluded from
speed comparisons. The corrected diagnostic server permits brief concurrent
responses during seeking. A production reader must explicitly cancel superseded
reads and enforce the configured one-upstream-stream limit; copying this helper
into production would skip that requirement.

## Recommendation and limits

**Resolved 2026-10-12:** see [bounded streaming acquisition](bounded-streaming.md).
FFmpeg 9 exposes `-request_size` and `-multiple_requests`; together they drain the
same URL in about 7 s versus 79 s direct, matching the diagnostic reader. The
project selects the supported FFmpeg option (ticket 7) and does not build a
production loopback reader. The recommendation below is retained as the original
attribution record.

Prioritize **bounded HTTP media requests** in the streaming acquisition path.
Download-first already supplies this behavior and explains the existing wins.
Hardware decoding, earlier cropping and Python frame prefetch do not address the
dominant upstream delay demonstrated here. Codec decoding can become material
after acquisition is fixed; the longer AV1 local drain demonstrates that cost.

Production defaults remain unchanged. A follow-up should evaluate either a
supported FFmpeg build with bounded-request options or a seekable yt-dlp-backed
reader. A plain yt-dlp-to-stdin pipe is not a drop-in replacement for the current
input seeks, chunk overlap and resume behavior. Any integration must preserve
EOF checks, retries, cancellation, watermarks and complete frame grids, then use
`benchmark_scan.py compare` for actual end-to-end measurements.

The existing timer already includes overlap: subtracting 76 seconds of isolated
acquisition from an 80-second scan is not a valid speed prediction. Once delivery
is fast, OCR becomes the main cost, as the local profiles show.

Validation: all 126 tests and Ruff checks pass. The diagnostic decoder is checked against the real production
`sample_frames()` on a synthetic video spanning a chunk boundary, including
overlap and an incomplete-tail case. Real H.264 and AV1 outputs are separately
checked for complete grids, identical RGB hashes, and matching downloaded bytes.
