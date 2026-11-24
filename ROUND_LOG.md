# Round log: os-optics-kit 0.5.0

Local evidence is labelled local. CI cannot install the unpublished engine; the engine checks there report
`ENGINE_CANDIDATE_NOT_PUBLISHED`.

| Item | Merged | Evidence | Deferred |
|---|---|---|---|
| A1 | PR #39 to main | main carries study execution, viewer, issue forms | - |
| A2 | PR #41 to main | required check `Engine checks (published os-optics)` green with the warning annotation (missing); local stub 0.14 index gives `engine_candidate=present` | - |
| A4 | PR #40 to main | `tests/test_version_guard.py`; `verified_version` 0.13.0 with `candidate_version` 0.14.0 | removed in H2 |
| C1 | merge into round/0.5.0 | `tests/test_tolerance_compensation.py` 20 passed under pytest with os-optics 0.14.0 (local); refocus off/on 1 PASS/3 FAIL -> 4 PASS; mirror alignment 1 PASS/3 FAIL -> 4 PASS, same sample ids | histogram overlay of as-built and compensated || B1 | merge into round/0.5.0 | `tests/test_ray_fans_engine.py` under pytest with os-optics 0.14.0 (local): ideal lens +-5 mm defocus, longitudinal aberration within 1e-6 mm at 2 planes x 2 wavelengths; aspheric-lens 3 fields x 3 wavelengths, OPD RMS = engine rms_waves (rel 1e-9), spot RMS within 10 % of engine geometric spot for spots >= 4 um; `tests/test_ray_fans.py` field-azimuth and tilted-chief-ray cases | vignetted-ray counts per path; tangential azimuth in output |
| F1 | merge into round/0.5.0 | `tests/test_evaluation_adapter.py` 19 passed under pytest with os-optics 0.14.0 (local): expansion-focus (non-imaging) and achromatic-imaging (MTF@20 constraint 0.809 at 0 mm, 0.778 at 0.2 mm) through one EvaluationSession interface; example script `cases/expansion-focus/adapter_example.py` executed by a test; fixed-length residuals with structured invalid reasons; residual_objective = objective + sum(violation^2) under sum_squares; MTF refused as an objective | docs page |
| B2 | merge into round/0.5.0 | `tests/test_field_aberrations_engine.py` and `tests/test_field_aberrations_known_mapping_engine.py` (12 passed with os-optics 0.14.0, local): ideal lens at 0/3/6 deg gives h = f tan(theta) within 1e-6 mm, zero distortion, flat focal surface; a plane offset DELTA reads distortion DELTA/f (sign follows DELTA) and focus -DELTA against closed-form values; no-engine synthetic suite covers the rotation, reference EFL and AXIS_FIELD_MISSING | a real-lens distortion reference better than the closed-form mapping (an independent Snell trace was attempted and shelved: sign conventions of the engine's surface model did not line up in the window) |
 
## Catalog 3.6 (local, offline)

Built from the committed inputs (material library merge + design catalog write), schema 3.6, catalog_version 2026-11-24.1, 1244 materials, 3273 products. The committed readiness hold names the previous release, so the build projects it only after relabelling the hold copy to 2026-11-24.1 in scratch (held list and reason unchanged). That artifact's sha256 is c305e34f9cfe3dc2c946f881402aaa8c79337d44aa556c0a07c73ebeb173b32a, matching the expected release hash. The artifact stays outside the repository; the case runs consume it with `cases verify --catalog <path>`.
