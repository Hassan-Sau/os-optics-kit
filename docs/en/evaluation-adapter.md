# Evaluation adapter: parameters, objectives and constraints for your own solver

[os-optics-kit](../../README.md) · English · [简体中文](../zh-CN/evaluation-adapter.md)

`os_optics_kit.study.evaluation_adapter` turns a Project into a function of named parameters for an optimiser,
a root finder or a scan you write yourself. Each call edits the Project, evaluates only what the declared quantities
need on one `os_optics.local.EvaluationSession` (os-optics 0.14), and returns a fixed-length residual vector with
every value, margin and reason behind it. For a batch of independent Projects in worker processes, use the study
executor instead ([Studies](studies.md#candidate-designs-and-external-search-code)).

## Declare the problem

```python
import json
from pathlib import Path

from os_optics_kit.study.evaluation_adapter import Adapter, Constraint, Objective, Problem, Variable

REFOCUS = [{"op": "transform", "selector": {"ids": ["focus"]}, "frame": "design", "translation_mm": ["$value", 0, 0]}]
problem = Problem(
    project=json.loads(Path("project.json").read_text(encoding="utf-8")),
    variables=[Variable("refocus", REFOCUS, (-2.0, 2.0), unit="mm", scale=0.1)],
    objectives=[Objective("spot", {"target": "spot_radius"}, scale=0.02)],
    constraints=[Constraint("waist", {"target": "waist_location"}, minimum=-0.5, maximum=0.5, scale=0.5)])
adapter = Adapter(problem)
result = adapter.evaluate({"refocus": 0.3})   # or adapter.evaluate([0.3]), in declaration order
```

| Part | Fields |
| --- | --- |
| `Variable` | `name`, `ops` (edit ops of `os-optics-kit edit`; `"$value"` is the value in `unit`), `bounds` (finite, low < high), `nominal`, `scale` |
| `Objective` | `name`, `source`, `goal`, `scale` (> 0), `weight` (>= 0) |
| `Constraint` | `name`, `source`, `minimum` and/or `maximum` (finite, `minimum <= maximum`), `scale` (> 0) |
| `Problem` | `project`, `variables`, `objectives`, `constraints`, `aggregation` (`sum_squares`, `sum_abs`, `max_abs`), `imaging`, `invalid_residual`, `value_use` (`exact`, `reported`) |

Objective and constraint names are unique together: they name the residuals (`problem.residual_names`). A
declaration that breaks these rules raises `ProblemError` when the `Problem` is built.

## Quantity sources

- `{"target": TARGET_ID}`: a target the Project declares, evaluated through the Engine's `requirements`.
- `{"mtf": {"observation": ID, "frequency_cyc_per_mm": F, "direction": "tangential"}}`: the MTF curves of the
  imaging analysis of plane `ID`, read at `F`. MTF is a constraint only: an MTF source, or a target whose metric
  name contains `mtf`, is refused as an objective.

The imaging analysis states one MTF curve per source and wavelength: `SRC#0`, `SRC#1`, `SRC#2` for one source at
three wavelengths, `SRC0_0#0` … `SRC2_2#0` for three field points at three wavelengths. An MTF constraint covers
every curve of its direction unless it narrows them with `source_id`, `wavelength_nm` (matched within 0.05 nm) or
`quantity_id`. It holds when it holds on every selected curve. Its result names the binding curve
(`quantity_id`, `source_id`, `field_id`, `wavelength_nm`, `value`, `margin`) and lists every curve under
`members`. A selection that matches no curve is the reason `MTF_SOURCE_NOT_FOUND`, which lists the curves the
analysis states.

`Problem.imaging` is the imaging request (`launch_grid`, `psf_half_width_um`, `psf_samples`, ...). Without it
the adapter uses `launch_grid` 24, which undersamples many pupils: the curves then read `OUT_OF_MODEL_SCOPE`.
Pass the case's own request, for example `cases/achromatic-imaging/imaging-request.json`.

## What a value is

Every reading has a `value_kind`, by the rule of the study tables: `EXACT`, `BOUNDED` (a lower or upper bound),
`QUANTITY_UNKNOWN` (an estimate, an `UNKNOWN` quantity or an MTF curve whose refinement check did not converge),
`OUTSIDE_MODEL` (status or state `OUT_OF_MODEL_SCOPE`, a model-scope finding that names the target, a failed
validity check), `UNDECIDED` (a target status other than PASS, WARNING or FAIL) or `NO_VALUE`. Target readings
also carry the Engine's `status` and `assessment`; MTF readings carry the curve's `state`, `convergence` and
`value_basis` or `basis_note`.

With `value_use="exact"` (the default) only `EXACT` values are used; any other reading makes the candidate invalid
with `QUANTITY_UNAVAILABLE`. With `value_use="reported"` every finite value is used for the residuals and its kind
is recorded; a constraint read from a value that is not exact has `satisfied: null` unless an exact value violates
it, and `feasible` is `null` when a constraint is not established.

## The result

`CandidateResult` (`as_dict()` for JSON):

| Field | Meaning |
| --- | --- |
| `valid` | the candidate was evaluated and every quantity is used |
| `residuals` | always `len(objectives) + len(constraints)` entries: `sqrt(weight) * (value - goal) / scale` per objective, then `max(0, -margin)` per constraint |
| `objective` | the `aggregation` of the objective errors (constraint violations do not count) |
| `residual_objective` | `sum(r ** 2 for r in residuals)`; with `sum_squares` it equals `objective + sum(violation ** 2)` and `dls_compatible` is true |
| `feasible` | every constraint holds (`true`), one is violated (`false`), or not established (`null`) |
| `objectives`, `constraints` | one entry per declared item with value, kind and facts; empty on an invalid candidate |
| `evaluation`, `identity` | the Engine's evaluation scope and the identity of the numbers |
| `reason` | for an invalid candidate: `code`, `stage`, `message` and details |

An invalid candidate keeps the residual length: each entry is `invalid_residual` (1000 by default; choose it above
any residual a valid candidate reaches), and for `OUT_OF_BOUNDS` it grows with the distance outside the bounds.
`evaluate` never raises for a candidate. Reason codes:

| Code | When |
| --- | --- |
| `OUT_OF_BOUNDS` | a value outside its bounds or not a finite real number |
| `UNKNOWN_VARIABLE`, `MISSING_VARIABLE` | the candidate names a variable the problem does not declare, or omits one (a sequence: too many or too few values) |
| `EDIT_REFUSED` | the edit ops refused the value |
| `PROJECT_REFUSED`, `PLAN_REFUSED`, `RUN_STOPPED` | the Engine refused the Project or the evaluation plan, or stopped |
| `ANALYSIS_REFUSED` | the imaging analysis refused the request |
| `MTF_SOURCE_NOT_FOUND` | an MTF selection matches no curve |
| `QUANTITY_UNAVAILABLE` | a quantity was not computed, or its value is not used under `value_use` |
| `RESIDUAL_NOT_FINITE` | a residual or margin overflowed |
| `ENGINE_ERROR` | any other Engine error |

A session evaluates one candidate at a time: do not call one `Adapter`, or adapters that share a session, from
several threads. For parallel evaluation, create one `Adapter` per process.

## Example

[`cases/expansion-focus/adapter_example.py`](../../cases/expansion-focus/adapter_example.py) scans the refocus
offset of the expander/focus design, one candidate outside the bounds included, and prints every result as JSON:

```bash
python cases/expansion-focus/adapter_example.py
```
