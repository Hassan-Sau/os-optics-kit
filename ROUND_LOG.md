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
