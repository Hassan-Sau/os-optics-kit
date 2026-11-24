# Round log: os-optics-kit 0.5.0

Local evidence is labelled local. CI cannot install the unpublished engine; the engine checks there report
`ENGINE_CANDIDATE_NOT_PUBLISHED`.

| Item | Merged | Evidence | Deferred |
|---|---|---|---|
| A1 | PR #39 to main | main carries study execution, viewer, issue forms | - |
| A2 | PR #41 to main | required check `Engine checks (published os-optics)` green with the warning annotation (missing); local stub 0.14 index gives `engine_candidate=present` | - |
| A4 | PR #40 to main | `tests/test_version_guard.py`; `verified_version` 0.13.0 with `candidate_version` 0.14.0 | removed in H2 |
| C1 | merge into round/0.5.0 | `tests/test_tolerance_compensation.py` 20 passed under pytest with os-optics 0.14.0 (local); refocus off/on 1 PASS/3 FAIL -> 4 PASS; mirror alignment 1 PASS/3 FAIL -> 4 PASS, same sample ids | histogram overlay of as-built and compensated |