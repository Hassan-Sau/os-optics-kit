"""Evaluation adapter (study.evaluation_adapter): fixed-length residuals, structured reasons, selective scope."""

from __future__ import annotations

import dataclasses
from fractions import Fraction
import importlib.util
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from os_optics_kit.study import evaluation_adapter as ea

ROOT = Path(__file__).resolve().parents[1]
HAS_ENGINE = importlib.util.find_spec("os_optics") is not None
engine = pytest.mark.skipif(not HAS_ENGINE, reason="os_optics is not importable")

PROJECT = {"targets": [{"target_id": "spot"}, {"target_id": "waist"}]}
VAR = ea.Variable("shift", [], (-1.0, 1.0), scale=0.1, unit="mm")


def _problem(**kw):
    base = dict(project=PROJECT, variables=[VAR], objectives=[ea.Objective("spot", {"target": "spot"}, scale=0.5)],
                constraints=[ea.Constraint("waist", {"target": "waist"}, minimum=-1.0, maximum=1.0)])
    base.update(kw)
    return ea.Problem(**base)


def _target(tid, value, status="PASS", value_basis=None, validity=()):
    return SimpleNamespace(target_id=tid, value=value, unit="mm", status=SimpleNamespace(value=status), metric="m",
                           assessment=None, value_basis=value_basis, validity=validity, issue_codes=(), message="")


def _scope():
    return SimpleNamespace(model_dump=lambda mode=None: {
        "requested": ["requirements"], "executed": ["requirements"],
        "not_computed": [{"capability": "power_ledger", "reason": "NOT_REQUESTED", "produces": []}],
        "targets_not_evaluated": []})


class FakeSession:
    """Returns target results (``values``: id -> number, or id -> a ready target object) and, when ``analysis`` is
    given, that imaging analysis for every plane."""

    def __init__(self, values=None, error=None, analysis=None, findings=()):
        self.values, self.error = values or {"spot": 1.0, "waist": 2.0}, error
        self.analysis, self.findings = analysis, findings

    def evaluate(self, document, outputs=(), targets=None):
        if self.error:
            raise self.error
        targets_ = [v if isinstance(v, SimpleNamespace) or hasattr(v, "target_id") else _target(k, v)
                    for k, v in self.values.items()]
        analysis = self.analysis
        return SimpleNamespace(result=SimpleNamespace(targets=targets_, evaluation=_scope()), identity={"h": 1},
                               model_scope_issues=self.findings,
                               results=lambda: SimpleNamespace(imaging=lambda observation, request: analysis))


def _adapter(problem=None, **session):
    return ea.Adapter(problem or _problem(), session=FakeSession(**session), edit=lambda d, ops: d)


def test_valid_candidate_reports_margins_aggregation_and_scope():
    r = _adapter().evaluate({"shift": 0.0})
    assert r.valid and r.feasible is False
    assert r.residuals == pytest.approx([2.0, 1.0])
    assert r.objective == pytest.approx(4.0)
    assert r.constraints[0]["margin"] == pytest.approx(-1.0) and r.constraints[0]["satisfied"] is False
    assert r.constraints[0]["value_kind"] == "EXACT" and r.constraints[0]["assessment"] is None
    assert r.evaluation["not_computed"][0]["capability"] == "power_ledger"


@pytest.mark.parametrize("x, session, code", [
    ({"shift": 5.0}, FakeSession(), "OUT_OF_BOUNDS"),
    ({"shift": float("nan")}, FakeSession(), "OUT_OF_BOUNDS"),
    ({"shift": "0.1"}, FakeSession(), "OUT_OF_BOUNDS"),
    ({"shift": 0.0}, FakeSession(values={"spot": None, "waist": 0.0}), "QUANTITY_UNAVAILABLE"),
    ({"shift": 0.0}, FakeSession(values={"waist": 0.0}), "QUANTITY_UNAVAILABLE"),
    ({"shift": 0.0}, FakeSession(error=RuntimeError("boom")), "ENGINE_ERROR"),
])
def test_invalid_candidates_keep_residual_length_and_carry_a_reason(x, session, code):
    adapter = ea.Adapter(_problem(), session=session, edit=lambda d, ops: d)
    r = adapter.evaluate(x)
    assert not r.valid and len(r.residuals) == adapter.size == 2
    assert r.reason["code"] == code and r.reason["code"] in ea.REASON_CODES and r.reason["message"]


def test_refused_edit_is_a_reason():
    def refuse(d, ops):
        raise ValueError("no")
    adapter = ea.Adapter(_problem(variables=[ea.Variable("shift", [{"op": "x", "v": "$value"}], (-1.0, 1.0))]),
                         session=FakeSession(), edit=refuse)
    r = adapter.evaluate([0.5])
    assert r.reason["code"] == "EDIT_REFUSED" and len(r.residuals) == 2


def test_mtf_is_refused_as_an_objective_and_accepted_as_a_constraint():
    mtf = {"mtf": {"observation": "image", "frequency_cyc_per_mm": 20}}
    with pytest.raises(ea.ProblemError, match="constraint, not an objective"):
        _problem(objectives=[ea.Objective("m", mtf)])
    p = _problem(constraints=[ea.Constraint("m", mtf, minimum=0.5)])
    assert p.outputs() == ("requirements", "plane_readings")


def test_problem_declaration_errors():
    with pytest.raises(ea.ProblemError):
        ea.Variable("v", [], (1.0, 0.0))
    with pytest.raises(ea.ProblemError):
        _problem(objectives=[ea.Objective("o", {"target": "missing"})])
    with pytest.raises(ea.ProblemError):
        _problem(aggregation="median")
    with pytest.raises(ea.ProblemError):
        _problem(value_use="loose")


NAN, INF = float("nan"), float("inf")


@pytest.mark.parametrize("objectives, constraints, match", [
    ([ea.Objective("o", {"target": "spot"}, goal=NAN)], [], "goal"),
    ([ea.Objective("o", {"target": "spot"}, goal=INF)], [], "goal"),
    ([ea.Objective("o", {"target": "spot"}, scale=INF)], [], "scale"),
    ([ea.Objective("o", {"target": "spot"}, weight=INF)], [], "weight"),
    ([ea.Objective("o", {"target": "spot"}, weight=NAN)], [], "weight"),
    ([ea.Objective("o", {"target": "spot"}, weight=0.0)], [], "weight > 0"),
    ([], [ea.Constraint("c", {"target": "waist"}, minimum=NAN)], "minimum"),
    ([], [ea.Constraint("c", {"target": "waist"}, maximum=NAN)], "maximum"),
    ([], [ea.Constraint("c", {"target": "waist"}, minimum=-1.0, maximum=NAN)], "maximum"),
    ([], [ea.Constraint("c", {"target": "waist"}, maximum=INF)], "maximum"),
    ([], [ea.Constraint("c", {"target": "waist"}, minimum=True)], "minimum"),
    ([], [ea.Constraint("c", {"target": "waist"}, minimum=1.0, maximum=-1.0)], "exceeds"),
    ([], [ea.Constraint("c", {"target": "waist"}, maximum=1.0, scale=INF)], "scale"),
])
def test_non_finite_or_misordered_declarations_are_refused(objectives, constraints, match):
    with pytest.raises(ea.ProblemError, match=match):
        ea.Problem(project=PROJECT, variables=[VAR], objectives=objectives, constraints=constraints)


def test_a_nan_maximum_no_longer_reads_satisfied():
    # before: minimum=-1, maximum=nan, value 1e6 gave margin 1000001 and satisfied/feasible true
    with pytest.raises(ea.ProblemError, match="maximum"):
        _problem(constraints=[ea.Constraint("waist", {"target": "waist"}, minimum=-1.0, maximum=NAN)])
    assert math.isnan(ea.margin_of(1e6, SimpleNamespace(minimum=-1.0, maximum=NAN, scale=1.0)))
    assert math.isnan(ea.margin_of(1e6, SimpleNamespace(minimum=NAN, maximum=2.0, scale=1.0)))


def test_objective_and_constraint_names_are_unique_together():
    mtf20 = {"mtf": {"observation": "image", "frequency_cyc_per_mm": 20}}
    mtf10 = {"mtf": {"observation": "image", "frequency_cyc_per_mm": 10, "direction": "sagittal"}}
    with pytest.raises(ea.ProblemError, match="unique"):
        _problem(constraints=[ea.Constraint("m", mtf20, minimum=0.5), ea.Constraint("m", mtf10, minimum=0.5)])
    with pytest.raises(ea.ProblemError, match="unique"):
        _problem(objectives=[ea.Objective("x", {"target": "spot"})],
                 constraints=[ea.Constraint("x", {"target": "waist"}, maximum=1.0)])


@pytest.mark.parametrize("how", ea.AGGREGATIONS)
def test_objective_residual_relation_with_a_violated_constraint(how):
    # spot error (1-0)/0.5 = 2, weight 2 -> residual 2*sqrt(2); waist 2 outside [-1, 1] -> violation 1
    p = _problem(aggregation=how, objectives=[ea.Objective("spot", {"target": "spot"}, scale=0.5, weight=2.0)])
    r = _adapter(p).evaluate({"shift": 0.0})
    assert r.valid and r.feasible is False
    assert r.residuals == pytest.approx([2.0 * 2 ** 0.5, 1.0])
    assert r.residual_objective == pytest.approx(sum(x * x for x in r.residuals))
    expected = {"sum_squares": 8.0, "sum_abs": 4.0, "max_abs": 4.0}[how]
    assert r.objective == pytest.approx(expected)  # constraint violations do not count in the objective
    assert r.dls_compatible is (how == "sum_squares")
    if how == "sum_squares":
        assert r.residual_objective == pytest.approx(r.objective + 1.0 ** 2)


def test_mtf_target_metric_is_refused_as_objective_and_accepted_as_constraint():
    project = {"targets": [{"target_id": "mtf20", "metric": "mtf_tangential"}, {"target_id": "spot", "metric": "m"}]}
    with pytest.raises(ea.ProblemError, match="MTF is a constraint"):
        ea.Problem(project=project, variables=[VAR], objectives=[ea.Objective("m", {"target": "mtf20"})])
    p = ea.Problem(project=project, variables=[VAR], objectives=[ea.Objective("s", {"target": "spot"})],
                   constraints=[ea.Constraint("m", {"target": "mtf20"}, minimum=0.5)])
    assert p.residual_names == ["objective:s", "constraint:m"]
    # counter-example: a metric name without "mtf" is an ordinary objective
    plain = {"targets": [{"target_id": "strehl", "metric": "strehl_ratio"}]}
    assert ea.Problem(project=plain, variables=[VAR], objectives=[ea.Objective("s", {"target": "strehl"})])
    assert not ea.is_mtf_metric("spot_rms_mm") and ea.is_mtf_metric("MTF_sagittal")


class NotEvaluatedSession(FakeSession):
    def evaluate(self, document, outputs=(), targets=None):
        ev = super().evaluate(document, outputs, targets)
        ev.result.targets = [t for t in ev.result.targets if t.target_id != "waist"]
        doc = ev.result.evaluation.model_dump()
        doc["targets_not_evaluated"] = [{"target_id": "waist", "metric": "m", "reason": "NOT_REQUESTED"}]
        ev.result.evaluation = SimpleNamespace(model_dump=lambda mode=None: doc)
        return ev


def test_target_not_evaluated_is_not_computed_and_quantity_unavailable():
    r = ea.Adapter(_problem(), session=NotEvaluatedSession(), edit=lambda d, ops: d).evaluate({"shift": 0.0})
    assert not r.valid and len(r.residuals) == 2
    assert r.reason["code"] == "QUANTITY_UNAVAILABLE"
    assert r.reason["reading"]["state"] == "NOT_COMPUTED" and r.reason["reading"]["not_evaluated"] is True
    assert r.evaluation["targets_not_evaluated"][0]["target_id"] == "waist"


def test_refused_candidate_carries_no_partial_entries():
    # the objective reads fine, the constraint's target is missing: nothing evaluated stays in the result
    r = _adapter(values={"spot": 0.02}).evaluate({"shift": 0.0})
    assert not r.valid and r.reason["code"] == "QUANTITY_UNAVAILABLE"
    assert r.objectives == [] and r.constraints == [] and r.objective is None and r.residual_objective is None


def test_out_of_bounds_residual_grows_with_distance_and_has_empty_scope():
    adapter = _adapter()
    near, far = adapter.evaluate({"shift": 1.1}), adapter.evaluate({"shift": 3.0})
    assert near.residuals[0] == pytest.approx(1e3 * (1 + 1.0)) and far.residuals[0] > near.residuals[0]
    assert near.evaluation == ea.EMPTY_SCOPE and len(far.residuals) == 2


def test_aggregations():
    assert ea.aggregate([1.0, -2.0], [1.0, 1.0], "sum_abs") == 3.0
    assert ea.aggregate([1.0, -2.0], [1.0, 1.0], "max_abs") == 2.0


# ------------------------------------------------------------------------------------------ candidate variables

@pytest.mark.parametrize("x, code, detail", [
    ({"shfit": 0.9}, "UNKNOWN_VARIABLE", ("unknown", ["shfit"])),
    ({"shift": 0.9, "extra": 3}, "UNKNOWN_VARIABLE", ("unknown", ["extra"])),
    ({}, "MISSING_VARIABLE", ("missing", ["shift"])),
    ([0.1, 0.2], "UNKNOWN_VARIABLE", ("got", 2)),
    ([], "MISSING_VARIABLE", ("missing", ["shift"])),
    (3.0, "MISSING_VARIABLE", ("declared", ["shift"])),
])
def test_candidate_must_name_exactly_the_declared_variables(x, code, detail):
    adapter = _adapter()
    r = adapter.evaluate(x)  # never raises
    assert not r.valid and r.reason["code"] == code and code in ea.REASON_CODES
    assert r.reason["stage"] == "parameters" and r.reason[detail[0]] == detail[1]
    assert r.residuals == [1e3, 1e3] and r.evaluation == ea.EMPTY_SCOPE
    with pytest.raises(ea._Refused):
        adapter.candidate(x)


def test_real_numbers_of_other_types_are_accepted():
    adapter = _adapter()
    r = adapter.evaluate({"shift": Fraction(1, 10)})
    assert r.valid and r.x == {"shift": 0.1}
    np = pytest.importorskip("numpy")
    r = adapter.evaluate({"shift": np.float32(0.25)})
    assert r.valid and r.x["shift"] == pytest.approx(0.25) and type(r.x["shift"]) is float
    assert adapter.evaluate(np.array([0.5])).valid


def test_a_template_that_cannot_take_a_value_is_refused_when_declared():
    with pytest.raises(ea.ProblemError, match="op template"):
        ea.Variable("v", [{"op": "x", "v": {"$value": {"scale": "two"}}}], (-1.0, 1.0))


def test_a_residual_that_overflows_is_a_reason():
    r = _adapter(_problem(objectives=[ea.Objective("spot", {"target": "spot"}, scale=1e-300)]),
                 values={"spot": 1e300, "waist": 0.0}).evaluate({"shift": 0.0})
    assert not r.valid and r.reason["code"] == "RESIDUAL_NOT_FINITE" and len(r.residuals) == 2


def _strict_json(result):
    """The result as strict JSON (no NaN or Infinity), as a consumer that refuses them reads it."""
    return json.loads(json.dumps(result.as_dict(), allow_nan=False))


def test_a_residual_that_overflows_names_it_and_serializes_as_strict_json():
    r = _adapter(_problem(objectives=[ea.Objective("spot", {"target": "spot"}, scale=1e-300)]),
                 values={"spot": 1e300, "waist": 0.0}).evaluate({"shift": 0.0})
    assert r.reason["code"] == "RESIDUAL_NOT_FINITE" and r.reason["not_finite"] == ["objective:spot"]
    assert r.reason["residuals"] == [None, 0.0]
    assert _strict_json(r)["reason"]["not_finite"] == ["objective:spot"]


@pytest.mark.parametrize("how", ea.AGGREGATIONS)
def test_finite_residuals_whose_aggregate_overflows_are_a_reason(how):
    # (1e100 - 0) / 1e-100 = 1e200: a finite residual whose square (and so residual_objective) exceeds the float range
    p = _problem(aggregation=how, objectives=[ea.Objective("spot", {"target": "spot"}, scale=1e-100)])
    r = _adapter(p, values={"spot": 1e100, "waist": 0.0}).evaluate({"shift": 0.0})
    assert not r.valid and r.reason["code"] == "AGGREGATE_NOT_FINITE" and "AGGREGATE_NOT_FINITE" in ea.REASON_CODES
    assert r.reason["stage"] == "results" and r.reason["aggregation"] == how
    expected = ["objective", "residual_objective"] if how == "sum_squares" else ["residual_objective"]
    assert r.reason["overflowed"] == expected
    assert r.reason["residuals"] == [1e200, 0.0] and r.reason["largest_residual"] == 1e200
    assert r.objective is None and r.residual_objective is None and r.feasible is None and not r.dls_compatible
    assert r.residuals == [1e3, 1e3] and r.objectives == [] and r.constraints == []
    doc = _strict_json(r)
    assert doc["valid"] is False and doc["reason"]["code"] == "AGGREGATE_NOT_FINITE"


def test_a_weighted_objective_that_overflows_is_a_reason():
    # residual sqrt(1e300) * 1e10 = 1e160 is finite; weight * |error| = 1e310 is not
    p = _problem(aggregation="sum_abs", objectives=[ea.Objective("spot", {"target": "spot"}, weight=1e300)])
    r = _adapter(p, values={"spot": 1e10, "waist": 0.0}).evaluate({"shift": 0.0})
    assert r.reason["code"] == "AGGREGATE_NOT_FINITE" and r.reason["overflowed"] == ["objective", "residual_objective"]
    assert r.reason["residuals"] == [pytest.approx(1e160), 0.0]
    _strict_json(r)


def test_a_constraint_violation_that_overflows_the_residual_objective_is_a_reason():
    # the objective (errors only) is 0; the violation 1e200 squares past the float range
    p = _problem(constraints=[ea.Constraint("waist", {"target": "waist"}, maximum=0.0, scale=1e-200)])
    r = _adapter(p, values={"spot": 0.0, "waist": 1.0}).evaluate({"shift": 0.0})
    assert r.reason["code"] == "AGGREGATE_NOT_FINITE" and r.reason["overflowed"] == ["residual_objective"]
    assert r.reason["residuals"] == [0.0, 1e200]
    _strict_json(r)


@pytest.mark.parametrize("how", ea.AGGREGATIONS)
def test_large_residuals_within_the_float_range_stay_valid(how):
    # 1e150 squares to 1e300: valid, with the same numbers as before
    p = _problem(aggregation=how, objectives=[ea.Objective("spot", {"target": "spot"}, scale=1e-50)])
    r = _adapter(p, values={"spot": 1e100, "waist": 0.0}).evaluate({"shift": 0.0})
    assert r.valid and r.reason is None and r.feasible is True
    assert r.residuals == [1e150, 0.0] and r.residual_objective == 1e150 * 1e150
    assert r.objective == {"sum_squares": 1e150 * 1e150, "sum_abs": 1e150, "max_abs": 1e150}[how]
    assert _strict_json(r)["residual_objective"] == r.residual_objective


def test_the_normal_path_serializes_as_strict_json_with_unchanged_numbers():
    r = _adapter().evaluate({"shift": 0.0})
    doc = _strict_json(r)
    assert doc["valid"] is True and doc["residuals"] == [2.0, 1.0]
    assert doc["objective"] == 4.0 and doc["residual_objective"] == 5.0 and doc["reason"] is None


@pytest.mark.parametrize("variable, value", [
    (ea.Variable("shift", [], (-1.0, 1.0), scale=1e-300), 2.0),  # violation 1e300: invalid_residual * (1 + d) overflows
    (ea.Variable("shift", [], (-1e308, -1e307), nominal=-1e307), 1.7e308),  # the violation itself overflows
])
def test_out_of_bounds_growth_stays_finite(variable, value):
    adapter = _adapter(_problem(variables=[variable]))
    r = adapter.evaluate([value])
    assert r.reason["code"] == "OUT_OF_BOUNDS" and len(r.residuals) == 2
    ceiling = math.sqrt(sys.float_info.max / (2 * 2))
    assert r.residuals == [ceiling, ceiling] and math.isfinite(sum(x * x for x in r.residuals))
    violation = r.reason["violation"]
    assert violation is None or (math.isfinite(violation) and violation > 0)
    _strict_json(r)
    # the next float above the upper bound is out of bounds and not worse than the farther candidate
    near = adapter.evaluate([math.nextafter(variable.bounds[1], math.inf)])
    assert near.reason["code"] == "OUT_OF_BOUNDS" and 1e3 < near.residuals[0] <= r.residuals[0]


# ---------------------------------------------------------------------------------------- what a value is

def _bounded():
    return {"value": {"kind": "LOWER_BOUND", "validity": [{"parameter": "x", "within": True}]}}


def _estimate(within):
    return {"value": {"kind": "ESTIMATE", "validity": [{"parameter": "x", "within": within}]}}


@pytest.mark.parametrize("spot, findings, kind", [
    (_target("spot", 0.1, status="OUT_OF_MODEL_SCOPE"), (), "OUTSIDE_MODEL"),
    (_target("spot", 0.1, status="UNKNOWN"), (), "UNDECIDED"),
    (_target("spot", 0.1, value_basis=_bounded()), (), "BOUNDED"),
    (_target("spot", 0.1, value_basis=_estimate(False)), (), "OUTSIDE_MODEL"),
    (_target("spot", 0.1, value_basis=_estimate(True)), (), "QUANTITY_UNKNOWN"),
    (_target("spot", 0.1, validity=({"parameter": "detector_linear_range", "within": False},)), (), "OUTSIDE_MODEL"),
    (_target("spot", 0.1), ({"code": "MODEL_WAVELENGTH_OUTSIDE_BAND", "affected_target_ids": ["spot"]},),
     "OUTSIDE_MODEL"),
])
def test_values_that_are_not_exact_are_not_used(spot, findings, kind):
    problem = _problem(constraints=[ea.Constraint("spot_max", {"target": "spot"}, maximum=0.5)], objectives=[])
    r = _adapter(problem, values={"spot": spot}, findings=findings).evaluate({"shift": 0.0})
    assert not r.valid and r.feasible is None and r.reason["code"] == "QUANTITY_UNAVAILABLE"
    assert r.reason["reading"]["value_kind"] == kind
    # opt-in: the reported value is used and its kind recorded, but the constraint is not established
    reported = _adapter(dataclasses.replace(problem, value_use="reported"), values={"spot": spot},
                        findings=findings).evaluate({"shift": 0.0})
    assert reported.valid and reported.value_use == "reported" and reported.feasible is None
    entry = reported.constraints[0]
    assert entry["value_kind"] == kind and entry["margin"] > 0 and entry["satisfied"] is None


def test_a_finding_on_another_target_leaves_the_value_exact():
    r = _adapter(values={"spot": 1.0, "waist": 0.0},
                 findings=({"code": "X", "affected_target_ids": ["other"]},)).evaluate({"shift": 0.0})
    assert r.valid and r.feasible is True and r.objectives[0]["value_kind"] == "EXACT"


def test_reported_values_keep_a_violation_definite():
    problem = _problem(value_use="reported")
    r = _adapter(problem, values={"spot": _target("spot", 1.0, status="UNKNOWN"), "waist": 2.0}).evaluate([0.0])
    assert r.valid and r.objectives[0]["value_kind"] == "UNDECIDED"
    assert r.constraints[0]["satisfied"] is False and r.feasible is False


# ----------------------------------------------------------------------------------------------- MTF curves

FREQS = [0.0, 10.0, 20.0]


def _analysis(curves):
    """``curves``: (quantity_id, source_id, wavelength_nm, state, [mtf at FREQS])."""
    quantities, arrays = [], {}
    for qid, source, wavelength, state, values in curves:
        prefix = qid.rsplit(".", 1)[0]
        quantities.append(SimpleNamespace(
            quantity_id=qid, state=SimpleNamespace(value=state), value_basis=None,
            values_ref=None if values is None else qid, convergence=None, reason=None, message="",
            scope=SimpleNamespace(observation_id="image", source_id=source, field_id=None, wavelength_nm=wavelength)))
        arrays[f"{prefix}.frequencies_cyc_per_mm"] = FREQS
        if values is not None:
            arrays[qid] = values
    return SimpleNamespace(quantities=quantities, arrays=arrays)


FIELDS = _analysis([
    ("SRC0_0#0.mtf_tangential", "SRC0_0", 486.1327, "KNOWN", [1.0, 0.9, 0.75]),
    ("SRC0_1#0.mtf_tangential", "SRC0_1", 587.5618, "KNOWN", [1.0, 0.9, 0.76]),
    ("SRC2_0#0.mtf_tangential", "SRC2_0", 486.1327, "KNOWN", [1.0, 0.3, 0.02]),
    ("SRC2_1#0.mtf_tangential", "SRC2_1", 587.5618, "KNOWN", [1.0, 0.3, 0.04]),
    ("SRC0_0#0.mtf_sagittal", "SRC0_0", 486.1327, "KNOWN", [1.0, 0.95, 0.8]),
    ("SRC0_0#0.geometric_mtf_tangential", "SRC0_0", 486.1327, "KNOWN", [1.0, 0.99, 0.99]),
])


def _mtf_problem(*constraints, **kw):
    return ea.Problem(project={"targets": []}, variables=[], constraints=list(constraints), **kw)


def _mtf(name, minimum=0.3, **spec):
    return ea.Constraint(name, {"mtf": {"observation": "image", "frequency_cyc_per_mm": 20, **spec}}, minimum=minimum)


def test_mtf_constraint_covers_every_curve_and_reports_the_binding_one():
    # before: the first curve ending in "#0.mtf_tangential" (SRC0_0, 0.75) was read and the design was feasible
    r = _adapter(_mtf_problem(_mtf("mtf20")), analysis=FIELDS).evaluate({})
    assert r.valid and r.feasible is False
    entry = r.constraints[0]
    assert entry["satisfied"] is False and entry["value"] == pytest.approx(0.02)
    assert entry["quantity_id"] == "SRC2_0#0.mtf_tangential" and entry["source_id"] == "SRC2_0"
    assert entry["wavelength_nm"] == pytest.approx(486.1327) and entry["margin"] == pytest.approx(-0.28)
    assert [m["source_id"] for m in entry["members"]] == ["SRC0_0", "SRC0_1", "SRC2_0", "SRC2_1"]
    assert r.residuals == pytest.approx([0.28])


@pytest.mark.parametrize("spec, sources, satisfied", [
    ({"source_id": "SRC0_0"}, ["SRC0_0"], True),
    ({"wavelength_nm": 587.56}, ["SRC0_1", "SRC2_1"], False),
    ({"quantity_id": "SRC0_1#0.mtf_tangential"}, ["SRC0_1"], True),
    ({"source_id": "SRC2_1", "wavelength_nm": 587.5618}, ["SRC2_1"], False),
    ({"source_id": "SRC0_0", "direction": "sagittal"}, ["SRC0_0"], True),
])
def test_mtf_selectors_narrow_the_curves(spec, sources, satisfied):
    r = _adapter(_mtf_problem(_mtf("mtf20", **spec)), analysis=FIELDS).evaluate({})
    entry = r.constraints[0]
    assert r.valid and [m["source_id"] for m in entry["members"]] == sources and entry["satisfied"] is satisfied
    assert entry["selector"] == {k: v for k, v in spec.items() if k != "direction"}


@pytest.mark.parametrize("spec", [{"source_id": "SRC9_9"}, {"wavelength_nm": 1064.0},
                                  {"quantity_id": "SRC#1.mtf_tangential"}])
def test_an_mtf_selector_that_matches_nothing_is_a_structured_reason(spec):
    r = _adapter(_mtf_problem(_mtf("mtf20", **spec)), analysis=FIELDS).evaluate({})
    assert not r.valid and r.reason["code"] == "MTF_SOURCE_NOT_FOUND" and r.reason["selector"] == spec
    assert [a["quantity_id"] for a in r.reason["available"]] == [
        "SRC0_0#0.mtf_tangential", "SRC0_1#0.mtf_tangential", "SRC2_0#0.mtf_tangential", "SRC2_1#0.mtf_tangential"]


def test_the_former_field_key_is_refused_when_declared():
    with pytest.raises(ea.ProblemError, match="source_id, wavelength_nm or quantity_id"):
        _mtf_problem(_mtf("mtf20", field=1))
    with pytest.raises(ea.ProblemError, match="frequency"):
        _mtf_problem(ea.Constraint("m", {"mtf": {"observation": "image", "frequency_cyc_per_mm": NAN}}, minimum=0.3))


def test_one_unusable_curve_makes_the_mtf_reading_unavailable():
    analysis = _analysis([("SRC0#0.mtf_tangential", "SRC0", 500.0, "KNOWN", [1.0, 0.9, 0.8]),
                          ("SRC1#0.mtf_tangential", "SRC1", 500.0, "OUT_OF_MODEL_SCOPE", None)])
    r = _adapter(_mtf_problem(_mtf("mtf20")), analysis=analysis).evaluate({})
    assert not r.valid and r.reason["code"] == "QUANTITY_UNAVAILABLE"
    assert [m["value_kind"] for m in r.reason["reading"]["members"]] == ["EXACT", "NO_VALUE"]
    assert _adapter(_mtf_problem(_mtf("mtf20", source_id="SRC0")), analysis=analysis).evaluate({}).valid


def test_two_mtf_constraints_keep_their_own_readings():
    # before: readings were keyed by name, so a second constraint named alike overwrote the first
    analysis = _analysis([("SRC#0.mtf_tangential", "SRC", 587.6, "KNOWN", [1.0, 0.6, 0.3]),
                          ("SRC#0.mtf_sagittal", "SRC", 587.6, "KNOWN", [1.0, 0.95, 0.9])])
    problem = _mtf_problem(_mtf("tan20", minimum=0.5),
                           ea.Constraint("sag10", {"mtf": {"observation": "image", "frequency_cyc_per_mm": 10,
                                                           "direction": "sagittal"}}, minimum=0.5))
    r = _adapter(problem, analysis=analysis).evaluate({})
    assert r.valid and r.feasible is False
    assert [(c["value"], c["satisfied"]) for c in r.constraints] == [(0.3, False), (0.95, True)]
    assert r.residual_names == ["constraint:tan20", "constraint:sag10"]


# ----------------------------------------------------------------------------------------------- engine

REFOCUS = [{"op": "transform", "selector": {"ids": ["focus"]}, "frame": "design", "translation_mm": ["$value", 0, 0]}]
IMAGE_SHIFT = [{"op": "transform", "selector": {"ids": ["image"]}, "frame": "design", "translation_mm": ["$value", 0, 0]}]
D_LINE_NM = 587.5618


def _case(name):
    return json.loads((ROOT / "cases" / name / "project.json").read_text(encoding="utf-8"))


def _imaging(name):
    return json.loads((ROOT / "cases" / name / "imaging-request.json").read_text(encoding="utf-8"))


@engine
def test_engine_imaging_and_non_imaging_problems_share_one_interface():
    from os_optics.local import EvaluationSession

    session = EvaluationSession()
    focus = ea.Problem(project=_case("expansion-focus"), variables=[ea.Variable("refocus", REFOCUS, (-2.0, 2.0), unit="mm")],
                       objectives=[ea.Objective("spot", {"target": "spot_radius"}, scale=0.02)],
                       constraints=[ea.Constraint("waist", {"target": "waist_location"}, minimum=-0.5, maximum=0.5)])
    image = ea.Problem(project=_case("achromatic-imaging"),
                       variables=[ea.Variable("image_shift", IMAGE_SHIFT, (-0.5, 0.5), unit="mm")],
                       objectives=[ea.Objective("spot_d", {"target": "pupil_spot_d"}, scale=0.005)],
                       constraints=[ea.Constraint("mtf20_d", {"mtf": {"observation": "image", "frequency_cyc_per_mm": 20,
                                                                      "wavelength_nm": D_LINE_NM}},
                                                  minimum=0.5, scale=0.1)],
                       imaging={**_imaging("achromatic-imaging"), "include_psf_map": False})
    for problem, name in ((focus, "refocus"), (image, "image_shift")):
        adapter = ea.Adapter(problem, session=session)
        results = [adapter.evaluate({name: v}) for v in (0.0, 0.1, 99.0)]
        assert [r.valid for r in results] == [True, True, False]
        assert {len(r.residuals) for r in results} == {adapter.size}
        assert results[2].reason["code"] == "OUT_OF_BOUNDS"
        scope = results[0].evaluation
        assert scope["complete"] is False and "requirements" in scope["executed"]
        assert any(n["capability"] == "power_ledger" for n in scope["not_computed"])
        assert "targets_not_evaluated" in scope
        assert results[0].objectives[0]["value_kind"] == "EXACT"
    mtf = ea.Adapter(image, session=session).evaluate({"image_shift": 0.0}).constraints[0]
    assert 0.5 < mtf["value"] < 1.0 and mtf["margin"] > 0 and mtf["state"] == "KNOWN" and mtf["satisfied"] is True
    # the d-line curve of the one source, named in the result
    assert mtf["quantity_id"] == "SRC#1.mtf_tangential" and mtf["source_id"] == "SRC"
    assert mtf["wavelength_nm"] == pytest.approx(D_LINE_NM) and len(mtf["members"]) == 1
    assert mtf["value_basis"] is not None or "imaging curve" in mtf["basis_note"]


@engine
def test_engine_unusable_mtf_is_a_structured_reason():
    # launch_grid 24 undersamples the pupil of achromatic-imaging: every MTF curve is OUT_OF_MODEL_SCOPE
    image = ea.Problem(project=_case("achromatic-imaging"), variables=[],
                       constraints=[ea.Constraint("mtf20", {"mtf": {"observation": "image", "frequency_cyc_per_mm": 20}},
                                                  minimum=0.5)],
                       imaging={"launch_grid": 24, "include_psf_map": False})
    r = ea.Adapter(image).evaluate({})
    assert not r.valid and len(r.residuals) == 1 and r.reason["code"] == "QUANTITY_UNAVAILABLE"
    assert {m["value_kind"] for m in r.reason["reading"]["members"]} != {"EXACT"}


class _ReplaySession:
    """A real ``EvaluationSession`` whose evaluation and imaging analyses are computed once per Project and request
    (the problems of one test read the same design)."""

    def __init__(self):
        from os_optics.local import EvaluationSession

        self.session, self.evaluations, self.analyses = EvaluationSession(), {}, {}

    def evaluate(self, document, outputs=(), targets=None):
        key = json.dumps([document, list(outputs), targets], sort_keys=True)
        if key not in self.evaluations:
            self.evaluations[key] = self.session.evaluate(document, outputs=outputs, targets=targets)
        ev = self.evaluations[key]

        def imaging(observation, request):
            cached = (key, observation, json.dumps(request, sort_keys=True))
            if cached not in self.analyses:
                self.analyses[cached] = ev.results().imaging(observation, request)
            return self.analyses[cached]

        return SimpleNamespace(result=ev.result, identity=ev.identity, model_scope_issues=ev.model_scope_issues,
                               results=lambda: SimpleNamespace(imaging=imaging))


@engine
def test_engine_mtf_floor_over_every_field_and_wavelength_is_not_met():
    """aspheric-lens: 3 field points x F/d/C. The 25 degree field misses an MTF20 floor of 0.3 that the axis meets
    (before: the first curve, on axis at 486 nm, was read and the design read feasible)."""
    session = _ReplaySession()  # one Engine evaluation and imaging analysis serve every problem below
    imaging = {**_imaging("aspheric-lens"), "launch_grid": 48, "include_psf_map": False}

    def run(value_use="exact", **spec):
        problem = ea.Problem(project=_case("aspheric-lens"), variables=[], imaging=imaging, value_use=value_use,
                             constraints=[ea.Constraint("mtf20", {"mtf": {"observation": "image_plane",
                                                                          "frequency_cyc_per_mm": 20, **spec}},
                                                        minimum=0.3)])
        return ea.Adapter(problem, session=session).evaluate({})

    every = run()  # never feasible: a 25 degree curve is below the floor or, unconverged, not established
    assert every.feasible is not True
    members = every.constraints[0]["members"] if every.valid else every.reason["reading"]["members"]
    assert len(members) == 9 and all(m["value"] < 0.3 for m in members if m["source_id"].startswith("SRC2_"))
    assert all(m["value"] > 0.3 for m in members if m["source_id"].startswith("SRC0_"))
    if not every.valid:
        assert every.reason["code"] == "QUANTITY_UNAVAILABLE"
        assert any(m["value_kind"] != "EXACT" and m["converged"] is False for m in members)
    reported = run(value_use="reported")
    entry = reported.constraints[0]
    assert reported.valid and reported.feasible is False and entry["satisfied"] is False
    assert entry["source_id"].startswith("SRC2_") and entry["value"] < 0.3 and len(entry["members"]) == 9
    field = run(source_id="SRC2_1")
    assert field.valid and field.feasible is False and field.constraints[0]["value_kind"] == "EXACT"
    axis = run(source_id="SRC0_1")
    assert axis.valid and axis.feasible is True and axis.constraints[0]["quantity_id"] == "SRC0_1#0.mtf_tangential"
    missing = run(source_id="SRC#1")
    assert not missing.valid and missing.reason["code"] == "MTF_SOURCE_NOT_FOUND"
    assert len(missing.reason["available"]) == 9


@engine
@pytest.mark.parametrize("status, basis, validity, kind", [
    ("OUT_OF_MODEL_SCOPE", None, (), "OUTSIDE_MODEL"),
    ("UNKNOWN", None, (), "UNDECIDED"),
    ("PASS", "LOWER_BOUND", (), "BOUNDED"),
    ("WARNING", None, ({"parameter": "detector_linear_range", "maximum": 1.0, "observed": 2.0, "within": False},),
     "OUTSIDE_MODEL"),
])
def test_engine_target_results_that_are_not_exact_are_unavailable(status, basis, validity, kind):
    from os_optics.contracts.basis import ValidityCheck, ValueBasis
    from os_optics.contracts.results import TargetResult

    value_basis = None if basis is None else {
        "value": ValueBasis(kind=basis, validity=(ValidityCheck(parameter="x", within=True),))}
    spot = TargetResult(target_id="spot", metric="m", value=0.1, status=status, value_basis=value_basis,
                        validity=tuple(ValidityCheck(**v) for v in validity))
    waist = TargetResult(target_id="waist", metric="m", value=0.02, status="PASS")
    problem = _problem(constraints=[ea.Constraint("waist", {"target": "waist"}, maximum=0.05)])
    r = _adapter(problem, values={"spot": spot, "waist": waist}).evaluate({"shift": 0.0})
    assert not r.valid and r.feasible is None and r.reason["code"] == "QUANTITY_UNAVAILABLE"
    assert r.reason["reading"]["value_kind"] == kind
    exact = _adapter(problem, values={"spot": TargetResult(target_id="spot", metric="m", value=0.1, status="PASS"),
                                      "waist": waist}).evaluate({"shift": 0.0})
    assert exact.valid and exact.feasible is True and exact.objectives[0]["assessment"] == "MET"


@engine
def test_engine_example_script_consumes_the_adapter():
    script = ROOT / "cases/expansion-focus/adapter_example.py"
    env = dict(os.environ)
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=600, env=env)
    assert out.returncode == 0, out.stderr
    doc = json.loads(out.stdout)
    assert len({len(r["residuals"]) for r in doc["candidates"]}) == 1
    assert any(not r["valid"] and r["reason"]["code"] == "OUT_OF_BOUNDS" for r in doc["candidates"])
    assert all(c["value_kind"] == "EXACT" for r in doc["candidates"] if r["valid"] for c in r["constraints"])


@engine
def test_engine_finite_residual_whose_square_overflows_is_a_reason():
    from os_optics.local import EvaluationSession

    session = EvaluationSession()

    def evaluate(scale):
        problem = ea.Problem(project=_case("expansion-focus"),
                             variables=[ea.Variable("refocus", REFOCUS, (-2.0, 2.0), unit="mm", scale=0.1)],
                             objectives=[ea.Objective("spot", {"target": "spot_radius"}, scale=scale)])
        return ea.Adapter(problem, session=session).evaluate({"refocus": 0.0})

    normal, overflow = evaluate(0.02), evaluate(1e-160)
    assert normal.valid and math.isfinite(normal.residual_objective) and _strict_json(normal)["valid"] is True
    # the same spot radius divided by 1e-160 is a finite residual whose square exceeds the float range
    assert not overflow.valid and overflow.reason["code"] == "AGGREGATE_NOT_FINITE"
    assert overflow.reason["overflowed"] == ["objective", "residual_objective"]
    (residual,) = overflow.reason["residuals"]
    assert math.isfinite(residual) and residual == pytest.approx(normal.residuals[0] * 0.02 / 1e-160)
    assert overflow.residuals == [1e3] and overflow.identity == normal.identity
    assert _strict_json(overflow)["reason"]["code"] == "AGGREGATE_NOT_FINITE"


@pytest.mark.parametrize("n", [1, 2, 3, 5, 7, 16, 63, 64, 101])
def test_out_of_bounds_residuals_keep_the_sum_of_squares_finite_for_any_count(n):
    objectives = [ea.Objective(f"o{i}", {"target": "spot"}, scale=1.0) for i in range(n)]
    variable = ea.Variable("shift", [], (-1.0, 1.0), scale=1e-300)
    r = _adapter(_problem(variables=[variable], objectives=objectives, constraints=[])).evaluate([2.0])
    assert r.reason["code"] == "OUT_OF_BOUNDS" and len(r.residuals) == n
    assert math.isfinite(sum(x * x for x in r.residuals))
    _strict_json(r)


def test_a_non_finite_candidate_value_serializes_as_strict_json():
    r = _adapter().evaluate({"shift": math.nan})
    assert not r.valid
    assert _strict_json(r)["x"]["shift"] is None


def test_an_infinite_engine_reading_serializes_as_strict_json():
    r = _adapter(values={"spot": math.inf, "waist": 0.0}).evaluate({"shift": 0.0})
    assert not r.valid
    _strict_json(r)


def test_the_overflow_message_names_what_overflowed_and_how_to_bring_it_into_range():
    r = _adapter(_problem(objectives=[ea.Objective("a", {"target": "spot"}, weight=1e308, scale=1.0),
                                      ea.Objective("b", {"target": "spot"}, weight=1e308, scale=1.0)],
                          aggregation="sum_abs", constraints=[]), values={"spot": 0.9}).evaluate({"shift": 0.0})
    assert r.reason["code"] == "AGGREGATE_NOT_FINITE"
    message = r.reason["message"]
    if r.reason["overflowed"] == ["objective"]:
        assert "objective exceeds" in message and "smaller weights bring the objective into range" in message
    else:
        assert " exceed the float range" in message
