# Refinement implementation

## R01 — effective configuration and contracts

`config.example.yaml` describes the detection pipeline without a HUD or OpenOCR
checkout. `settings.py` owns validated defaults and canonical JSON/SHA-256 scan
identity. CLI sampling overrides are validated before model/media work. Local
weight files are hashed by content; adapters supply actual runtime versions,
dependencies, preprocessing and weights identities when instantiated. Unloaded
model versions and absent hashes remain explicitly unknown, never fabricated.
Aliases, matching settings and report presentation do not enter the OCR identity.

`contracts.py` defines source frames, detections, prepared crops and adapter
metadata; `recognize.Recognition` retains ordered text/nullable-confidence results.
The provisional confidence floor is 0.1, detector confidence 0.3, padding 2 pixels,
center-distance tolerance 0.05 and motion tolerance 0.5. These require evaluation.
All effective settings, including versions, belong in immutable run metadata.

Old configurations must explicitly set `pipeline: legacy_hud`. Only that path
uses `scan`, `profiles` and `ocr`; its eight-second legacy tracker is unchanged.
The new settings contract uses three seconds. Paddle runtime construction is
R04, retention enforcement R05, bounded acquisition R06, and batching R07;
settings for those packages are contracts, not claims they already execute.
Until R04/CLI cutover, normal detection CLI scans fail clearly before downloading;
query/report remain available without inference packages.

Validation: baseline 19 tests passed. R01 adds minimal/legacy configuration,
invalid values/interdependent limits, weight/override identity and query exclusion
coverage; full pytest and Ruff results are recorded in the requirement commit.

R01 validation result: 31 tests passed; Ruff passed. Real model setup is deferred to R04.
