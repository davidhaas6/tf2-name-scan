# 1. Attribute acquisition and frame-delivery cost

Status: complete (2026-10-03). See [acquisition attribution](../docs/acquisition-attribution.md)
for the fresh production baseline, repeated acquisition/decoder controls, CPU and
memory measurements, and the measured recommendation. Bounded HTTP acquisition
is [ticket 7](07-bounded-http-acquisition.md); production defaults remain unchanged.

## Problem

Current GPU profiles spend 65–74% of scan wall time in `decoding`. This timer
combines network waiting, demuxing, decoding, filtering, pipe reads, and image
construction; it does not identify the bottleneck. See
`output/admiralty-perf-gpu-2.json` and `output/coverage-recheck-profile.json`.

## Work

- Record selected format ID, codec, source fps, bitrate, protocol, and resolution.
- Use ticket 6's cached local-versus-stream benchmark and saved history as the
  baseline. Add focused frame-draining measurements without OCR to distinguish
  upstream delivery from consumer work; do not build another benchmark suite.
- Measure FFmpeg CPU/wall time, frame wait, memory, and end-to-end RTF; repeat
  comparisons with fixed inputs and starting corpora.
- Use the attribution to select a targeted experiment: stream format, hardware
  decoding, bounded frame prefetch, or [earlier region cropping](05-early-region-cropping.md).
  Deliver a measured recommendation; implementing the chosen optimization is
  follow-up work. Keep production defaults unchanged during attribution.

## Acceptance

Document the dominant cost and a measured optimization decision. Preserve the
1-fps sample grid, chunk overlap, EOF checks, cancellation, and resume behavior.
Report model setup separately; do not assume frame-wait reductions translate
directly into total speedups because FFmpeg already overlaps with OCR.
