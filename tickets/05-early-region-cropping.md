# 5. Crop the existing detector region before pipe transfer

Status: open. [Acquisition/decode attribution](01-acquisition-decode-attribution.md)
found the dominant streaming delay is upstream HTTP request policy, not pipe
payload, so this is a separate frame-delivery optimization rather than the fix
for the streaming gap.

## Problem

The current top-right mode transfers and retains a full sampled RGB frame before
cropping detector input in Python. At 1280×720, transferring only the existing
quarter-region would reduce raw pipe payload and frame-image memory by about 4×;
it would not reduce compressed-video acquisition or decode work.

## Work

- Move the existing top-right crop into FFmpeg while preserving current sampling
  and scaling behavior; keep full-frame mode available.
- Carry the source-region offset through `Frame` and update crop rectification,
  which currently assumes a full-frame image.
- Preserve full-frame evidence when requested, or use the existing full-frame
  path for that configuration. Keep detection and evidence in source coordinates.
- Benchmark pipe wait, memory, commit frequency, and total RTF on fixed clips.

## Acceptance

Verify odd dimensions, source scaling, polygons, crop transforms, and evidence
placement against the existing path. Preserve timestamp coverage and resume
behavior; investigate OCR differences. Adopt only with a measured benefit.
Changing region coverage, sampling rate, or resolution is outside this ticket.
