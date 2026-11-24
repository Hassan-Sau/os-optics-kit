"""Evaluation adapter (study.evaluation_adapter): fixed-length residuals, structured reasons, selective scope."""

from __future__ import annotations

import importlib.util
import json
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


class FakeSession:
    def __init__(self, values=None, error=None):
        self.values, self.error = values or {"spot": 1.0, "waist": 2.0}, error

    def evaluate(self, document, outputs=(), targets=None):
        if self.error:
            raise self.error
        targets_ = [SimpleNamespace(target_id=k, value=v, unit="mm", status=SimpleNamespace(value="PASS"), metric="m",
                                    value_basis=None, issue_codes=(), message="") for k, v in self.values.items()]
        scope = SimpleNamespace(model_dump=lambda mode=None: {
            "requested": ["requirements"], "executed": ["requirements"],
            "not_computed": [{"capability": "power_ledger", "reason": "NOT_REQUESTED", "produces": []}],
            "targets_not_evaluated": []})
        return SimpleNamespace(result=SimpleNamespace(targets=targets_, evaluation=scope), identity={"h": 1})


def test_valid_candidate_reports_margins_aggregation_and_scope():
    r = ea.Adapter(_problem(), session=FakeSession(), edit=lambda d, ops: d).evaluate({"shift": 0.0})
    assert r.valid and r.feasible is False
    assert r.residuals == pytest.approx([2.0, 1.0])
    assert r.objective == pytest.approx(4.0)
    assert r.constraints[0]["margin"] == pytest.approx(-1.0)
    assert r.evaluation["not_computed"][0]["capability"] == "power_ledger"


@pytest.mark.parametrize("x, session, code", [
    ({"shift": 5.0}, FakeSession(), "OUT_OF_BOUNDS"),
    ({"shift": float("nan")}, FakeSession(), "OUT_OF_BOUNDS"),
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


@pytest.mark.parametrize("how", ea.AGGREGATIONS)
def test_objective_residual_relation_with_a_violated_constraint(how):
    # spot error (1-0)/0.5 = 2, weight 2 -> residual 2*sqrt(2); waist 2 outside [-1, 1] -> violation 1
    p = _problem(aggregation=how, objectives=[ea.Objective("spot", {"target": "spot"}, scale=0.5, weight=2.0)])
    r = ea.Adapter(p, session=FakeSession(), edit=lambda d, ops: d).evaluate({"shift": 0.0})
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


def test_out_of_bounds_residual_grows_with_distance_and_has_empty_scope():
    adapter = ea.Adapter(_problem(), session=FakeSession(), edit=lambda d, ops: d)
    near, far = adapter.evaluate({"shift": 1.1}), adapter.evaluate({"shift": 3.0})
    assert near.residuals[0] == pytest.approx(1e3 * (1 + 1.0)) and far.residuals[0] > near.residuals[0]
    assert near.evaluation == ea.EMPTY_SCOPE and len(far.residuals) == 2


def test_aggregations():
    assert ea.aggregate([1.0, -2.0], [1.0, 1.0], "sum_abs") == 3.0
    assert ea.aggregate([1.0, -2.0], [1.0, 1.0], "max_abs") == 2.0


# ----------------------------------------------------------------------------------------------- engine

REFOCUS = [{"op": "transform", "selector": {"ids": ["focus"]}, "frame": "design", "translation_mm": ["$value", 0, 0]}]
IMAGE_SHIFT = [{"op": "transform", "selector": {"ids": ["image"]}, "frame": "design", "translation_mm": ["$value", 0, 0]}]


def _case(name):
    return json.loads((ROOT / "cases" / name / "project.json").read_text(encoding="utf-8"))


@engine
def test_engine_imaging_and_non_imaging_problems_share_one_interface():
    from os_optics.local import EvaluationSession

    session = EvaluationSession()
    focus = ea.Problem(project=_case("expansion-focus"), variables=[ea.Variable("refocus", REFOCUS, (-2.0, 2.0), unit="mm")],
                       objectives=[ea.Objective("spot", {"target": "spot_radius"}, scale=0.02)],
                       constraints=[ea.Constraint("waist", {"target": "waist_location"}, minimum=-0.5, maximum=0.5)])
    imaging = json.loads((ROOT / "cases/achromatic-imaging/imaging-request.json").read_text(encoding="utf-8"))
    image = ea.Problem(project=_case("achromatic-imaging"),
                       variables=[ea.Variable("image_shift", IMAGE_SHIFT, (-0.5, 0.5), unit="mm")],
                       objectives=[ea.Objective("spot_d", {"target": "pupil_spot_d"}, scale=0.005)],
                       constraints=[ea.Constraint("mtf20", {"mtf": {"observation": "image", "frequency_cyc_per_mm": 20,
                                                                    "field": 1}}, minimum=0.5, scale=0.1)],
                       imaging={**imaging, "launch_grid": 24, "include_psf_map": False})
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
    mtf = ea.Adapter(image, session=session).evaluate({"image_shift": 0.0}).constraints[0]
    assert 0.5 < mtf["value"] < 1.0 and mtf["margin"] > 0 and mtf["state"] == "KNOWN"
    reading = ea.Adapter(image, session=session)._readings(
        session.evaluate(_case("achromatic-imaging"), outputs=image.outputs(), targets=image.target_ids()))["mtf:mtf20"]
    assert reading["value_basis"] is not None or "imaging curve" in reading["basis_note"]


@engine
def test_engine_unusable_mtf_is_a_structured_reason():
    image = ea.Problem(project=_case("achromatic-imaging"), variables=[],
                       constraints=[ea.Constraint("mtf20", {"mtf": {"observation": "image", "frequency_cyc_per_mm": 20,
                                                                         "field": 1}}, minimum=0.5)],
                       imaging={"launch_grid": 24, "include_psf_map": False})
    r = ea.Adapter(image).evaluate({})
    assert not r.valid and len(r.residuals) == 1 and r.reason["code"] == "QUANTITY_UNAVAILABLE"
    assert r.reason["reading"]["state"] != "KNOWN"


@engine
def test_engine_example_script_consumes_the_adapter():
    script = ROOT / "cases/expansion-focus/adapter_example.py"
    env = dict(os.environ)
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=600, env=env)
    assert out.returncode == 0, out.stderr
    doc = json.loads(out.stdout)
    assert len({len(r["residuals"]) for r in doc["candidates"]}) == 1
    assert any(not r["valid"] and r["reason"]["code"] == "OUT_OF_BOUNDS" for r in doc["candidates"])
