"""Compensation of tolerance samples (study.compensation): solver, failure reasons, stages, off by default."""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import math
from pathlib import Path

import pytest

from os_optics_kit import workflow_common as wc
from os_optics_kit.study import compensation as cm

ROOT = Path(__file__).resolve().parents[1]
REFOCUS = ROOT / "cases/expansion-focus/studies/tolerance_expander_refocus.json"
MIRROR = ROOT / "cases/mirror-alignment/studies/tolerance_alignment_compensated.json"
HAS_ENGINE = importlib.util.find_spec("os_optics") is not None
engine = pytest.mark.skipif(not HAS_ENGINE, reason="os_optics is not importable")

COMP = {"name": "refocus", "unit": "mm", "nominal": 0.0, "ops": [], "bounds": [-1.0, 1.0], "tolerance": 1e-4}
TERMS = [{"target": "spot", "unit": "mm", "goal": 0.0, "scale": 1.0}]
PLAN = {"objective": TERMS, "compensators": [COMP], "max_evaluations": 60, "max_sweeps": 3,
        "cost_tolerance": 1e-9, "objective_definition": "sum of squares", "solver": "coordinate descent"}


def _record(value, error=None, **target):
    """A run record with one ``spot`` target (``target``: fields overriding the exact PASS row)."""
    record = wc._empty_record(None, None, ["spot"])
    if error:
        record["error"] = {"stage": "run", "code": error, "problems": [{"code": "X"}]}
        return record
    row = {"id": "spot", "value": value, "unit": "mm", "status": "PASS", "assessment": "MET",
           "quantity_state": "EXACT", **target}
    record.update(status="PASS", outcome="OUT_OF_MODEL" if "OUT_OF_MODEL_SCOPE" in row.values() else "PASS",
                  execution={"outcome": "COMPLETED"}, targets=[row],
                  identity={**record["identity"], "result_hash": f"h{value}"})
    return record


#: target fields of a value that is not exact, by its workflow_common.target_value_use kind
NOT_EXACT = {"OUTSIDE_MODEL": {"status": "OUT_OF_MODEL_SCOPE", "quantity_state": "OUT_OF_MODEL_SCOPE"},
             "BOUNDED": {"quantity_state": "BOUNDED"}, "QUANTITY_UNKNOWN": {"quantity_state": "UNKNOWN"},
             "UNDECIDED": {"status": "INDETERMINATE"}}


def _fake(monkeypatch, value_of):
    """Compensator offsets travel in the Project; ``value_of(offset)`` gives the spot value or raises."""
    monkeypatch.setattr(cm, "compensated_document",
                        lambda doc, comps, offsets: {**doc, "offset": offsets[comps[0]["name"]]})
    calls = []

    def run(sub, folder, limits):
        calls.append(sub["project"]["offset"])
        return value_of(sub["project"]["offset"])
    return run, calls


def _request():
    return {"sample_id": "s0001", "project": {"offset": 0.0}, "selected_targets": ["spot"],
            "outputs": {"run_directory": False, "ray_data": "summary"}, "labels": {"role": "sample"}}


# --------------------------------------------------------------------------------------------- solver

def test_solve_finds_interior_minimum_and_starts_at_as_built():
    out = cm.solve(lambda o: (o["refocus"] - 0.3) ** 2, [COMP], max_evaluations=60, max_sweeps=3,
                   cost_tolerance=1e-12, start_cost=0.09)
    assert out["state"] == cm.SOLVED and out["reason"] is None
    assert abs(out["offsets"]["refocus"] - 0.3) < 1e-3
    assert out["cost"] <= out["start_cost"]


def test_solve_two_compensators_coordinate_descent():
    comps = [dict(COMP, name="a"), dict(COMP, name="b")]
    out = cm.solve(lambda o: (o["a"] - 0.2) ** 2 + (o["b"] + 0.4) ** 2, comps, max_evaluations=200,
                   max_sweeps=5, cost_tolerance=1e-10)
    assert out["state"] == cm.SOLVED
    assert abs(out["offsets"]["a"] - 0.2) < 1e-3 and abs(out["offsets"]["b"] + 0.4) < 1e-3


def test_solve_bounds_hit_is_a_structured_failure():
    out = cm.solve(lambda o: (o["refocus"] - 5.0) ** 2, [COMP], max_evaluations=60, max_sweeps=2,
                   cost_tolerance=0.0)
    assert out["state"] == cm.FAILED
    assert out["reason"]["code"] == "BOUNDS_HIT" and out["reason"]["compensators"] == ["refocus"]


def test_solve_not_converged_when_budget_runs_out():
    out = cm.solve(lambda o: (o["refocus"] - 0.3) ** 2, [COMP], max_evaluations=4, max_sweeps=3,
                   cost_tolerance=0.0)
    assert out["state"] == cm.FAILED and out["reason"]["code"] == "NOT_CONVERGED"
    assert out["evaluations"] == 4


# --------------------------------------------------------------------------------------------- compensate

def test_compensate_records_three_stages(monkeypatch, tmp_path):
    run, _calls = _fake(monkeypatch, lambda x: _record(abs(x - 0.25) + 0.01))
    as_built = _record(0.26)
    block = cm.compensate(_request(), as_built, tmp_path, PLAN, run=run)
    assert block["state"] == cm.SOLVED and block["reason"] is None
    assert block["compensated"]["result_stage"] == "compensated"
    assert block["objective"]["compensated"]["cost"] < block["objective"]["as_built"]["cost"]
    assert abs(block["compensators"]["refocus"]["offset"] - 0.25) < 1e-3
    assert as_built["targets"][0]["value"] == 0.26  # the as-built result is not replaced


def test_compensate_evaluation_error_keeps_as_built(monkeypatch, tmp_path):
    def value_of(x):
        return _record(None, error="RUN_REFUSED") if x > 0.5 else _record(abs(x - 0.25))
    run, _calls = _fake(monkeypatch, value_of)
    block = cm.compensate(_request(), _record(0.25), tmp_path, PLAN, run=run)
    assert block["state"] == cm.FAILED and block["compensated"] is None
    assert block["reason"]["code"] == "EVALUATION_ERROR"
    assert block["reason"]["run_error"] == {"stage": "run", "code": "RUN_REFUSED"}
    assert block["objective"]["as_built"]["cost"] == pytest.approx(0.0625)


def test_compensate_as_built_error(monkeypatch, tmp_path):
    run, calls = _fake(monkeypatch, lambda x: _record(0.1))
    block = cm.compensate(_request(), _record(None, error="RUN_REFUSED"), tmp_path, PLAN, run=run)
    assert block["reason"]["code"] == "AS_BUILT_ERROR" and calls == []


def _fake_final(monkeypatch, trial_of, final_of):
    """Like ``_fake``, but the final compensated run (folder ``compensated``) is answered by ``final_of``."""
    monkeypatch.setattr(cm, "compensated_document",
                        lambda doc, comps, offsets: {**doc, "offset": offsets[comps[0]["name"]]})

    def run(sub, folder, limits):
        x = sub["project"]["offset"]
        return final_of(x) if Path(folder).name == "compensated" else trial_of(x)
    return run


def test_compensate_final_run_failure_keeps_as_built(monkeypatch, tmp_path):
    run = _fake_final(monkeypatch, lambda x: _record(abs(x - 0.25) + 0.01),
                      lambda x: _record(None, error="RUN_REFUSED"))
    block = cm.compensate(_request(), _record(0.26), tmp_path, PLAN, run=run)
    assert block["state"] == cm.FAILED and block["compensated"] is None
    assert block["reason"]["code"] == "EVALUATION_ERROR"
    assert block["reason"]["message"].startswith("compensated run:")
    assert block["compensators"]["refocus"]["offset"] is None


def test_compensate_worse_than_as_built_keeps_as_built(monkeypatch, tmp_path):
    run = _fake_final(monkeypatch, lambda x: _record(abs(x - 0.25) + 0.01), lambda x: _record(0.5))
    block = cm.compensate(_request(), _record(0.26), tmp_path, PLAN, run=run)
    assert block["state"] == cm.FAILED and block["compensated"] is None
    assert block["reason"]["code"] == "WORSE_THAN_AS_BUILT"
    assert block["reason"]["compensated_cost"] > block["reason"]["as_built_cost"]
    assert block["compensators"]["refocus"]["offset"] is None


def test_compensate_run_stopped(monkeypatch, tmp_path):
    run, _calls = _fake(monkeypatch, lambda x: _record(None, error="RUN_STOPPED"))
    block = cm.compensate(_request(), _record(0.26), tmp_path, PLAN, run=run)
    assert block["state"] == cm.FAILED and block["compensated"] is None
    assert block["reason"]["code"] == "RUN_STOPPED"


def test_compensate_ops_refused(monkeypatch, tmp_path):
    def refuse(doc, comps, offsets):
        raise wc.WorkflowError("OPS_INVALID", "no such component")
    monkeypatch.setattr(cm, "compensated_document", refuse)
    calls = []
    block = cm.compensate(_request(), _record(0.26), tmp_path, PLAN,
                          run=lambda sub, folder, limits: calls.append(1) or _record(0.1))
    assert block["state"] == cm.FAILED and block["compensated"] is None and calls == []
    assert block["reason"]["code"] == "EVALUATION_ERROR"
    assert "compensator ops refused" in block["reason"]["message"]


def test_bounds_must_strictly_contain_zero():
    kw = {"document": {}, "targets": ["spot"], "units": {"spot": "mm"}, "parameter_names": []}
    for bounds in ([0.0, 1.0], [-1.0, 0.0]):
        raw = {"objective": [{"target": "spot", "goal": 0, "scale": 1}],
               "compensators": [dict(COMP, bounds=bounds, ops=[])]}
        with pytest.raises(wc.WorkflowError):
            cm.normalize(raw, **kw)


def test_failed_sample_is_kept_in_aggregation_rows():
    record = {**_record(0.3), "result_stage": "as_built",
              "compensation": {"state": cm.FAILED, "reason": {"code": "BOUNDS_HIT"}, "evaluations": 9,
                               "objective": {"as_built": {"cost": 0.09}, "compensated": None},
                               "compensators": {"refocus": {"offset": None}}, "compensated": None}}
    row = {"sample_id": "s0001", "outcome": "PASS", "target:spot": 0.3, "target:spot:value_kind": "EXACT",
           **cm.row_fields(record, PLAN)}
    assert row["result_stage"] == "as_built" and row["compensation:reason"] == "BOUNDS_HIT"
    nominal = {"outcome": "PASS", "target:spot": 0.0}
    summary = cm.summarize([row], PLAN, ["spot"], {"spot": "mm"}, nominal)
    assert summary["requested"] == 1 and summary["states"] == {cm.SOLVED: 0, cm.FAILED: 1, cm.NOT_EVALUATED: 0}
    assert summary["failure_reasons"] == {"BOUNDS_HIT": 1}
    assert summary["compensated_or_as_built"]["by_outcome"]["PASS"] == 1
    assert summary["stages"] == ["nominal", "as_built", "compensated"]


# --------------------------------------------------------------------------------------------- exact values only

@pytest.mark.parametrize("kind", sorted(NOT_EXACT))
def test_as_built_value_that_is_not_exact_is_as_built_error(monkeypatch, tmp_path, kind):
    run, calls = _fake(monkeypatch, lambda x: _record(0.1))
    block = cm.compensate(_request(), _record(0.0, **NOT_EXACT[kind]), tmp_path, PLAN, run=run)
    assert block["state"] == cm.FAILED and calls == []
    assert block["reason"]["code"] == "AS_BUILT_ERROR" and block["reason"]["value_kind"] == kind
    assert block["objective"]["as_built"] is None


@pytest.mark.parametrize("kind", sorted(NOT_EXACT))
def test_trials_without_exact_values_are_never_selected(monkeypatch, tmp_path, kind):
    # cost 0 beyond offset 0.2, but those values are not exact: the solve stays in the exact region
    run, calls = _fake(monkeypatch, lambda x: _record(0.0, **NOT_EXACT[kind]) if x > 0.2
                       else _record(abs(x - 0.25) + 0.01))
    block = cm.compensate(_request(), _record(0.26), tmp_path, PLAN, run=run)
    assert any(x > 0.2 for x in calls)
    offset = block["compensators"]["refocus"]["offset"]
    assert offset is None or offset <= 0.2
    if block["state"] == cm.SOLVED:
        assert block["compensated"]["targets"][0]["quantity_state"] == "EXACT"
        assert block["objective"]["compensated"]["cost"] > 0.0
    infeasible = [t for t in block["trace"] if t.get("infeasible")]
    assert infeasible and all(t["cost"] is None and t["infeasible"]["value_kind"] == kind for t in infeasible)
    json.dumps(block, allow_nan=False)  # the trace holds no infinite cost


@pytest.mark.parametrize("kind", sorted(NOT_EXACT))
def test_final_run_without_exact_value_fails(monkeypatch, tmp_path, kind):
    run = _fake_final(monkeypatch, lambda x: _record(abs(x - 0.25) + 0.01),
                      lambda x: _record(0.0, **NOT_EXACT[kind]))
    block = cm.compensate(_request(), _record(0.26), tmp_path, PLAN, run=run)
    assert block["state"] == cm.FAILED and block["compensated"] is None
    assert block["reason"]["code"] == "EVALUATION_ERROR" and block["reason"]["value_kind"] == kind
    assert block["reason"]["message"].startswith("compensated run:")


def test_solve_with_only_infeasible_trials_is_evaluation_error():
    def evaluate(offsets):
        raise cm.NotExact({"code": "EVALUATION_ERROR", "message": "bounded", "target": "spot",
                           "value_kind": "BOUNDED", "codes": []})
    out = cm.solve(evaluate, [COMP], max_evaluations=30, max_sweeps=2, cost_tolerance=0.0)
    assert out["state"] == cm.FAILED and out["reason"]["code"] == "EVALUATION_ERROR"
    assert out["reason"]["value_kind"] == "BOUNDED" and all(t["cost"] is None for t in out["trace"])


def test_offset_zero_is_evaluated_when_it_does_not_reproduce_the_as_built_project(monkeypatch, tmp_path):
    # offset 0 adds a key to the Project: the as-built cost does not stand for it, offset 0 is a trial
    monkeypatch.setattr(cm, "compensated_document",
                        lambda doc, comps, offsets: {**doc, "offset": offsets[comps[0]["name"]], "moved": True})
    calls = []

    def run(sub, folder, limits):
        calls.append(sub["project"]["offset"])
        return _record(abs(sub["project"]["offset"] - 0.25) + 0.01)
    block = cm.compensate(_request(), _record(0.26), tmp_path, PLAN, run=run)
    assert calls[0] == 0.0 and block["state"] == cm.SOLVED


def _table(header, rows):
    return [" | ".join(map(str, row)) for row in [header, *rows]]


def test_solved_without_improvement_is_counted():
    record = {**_record(0.3), "result_stage": "as_built",
              "compensation": {"state": cm.SOLVED, "reason": None, "evaluations": 20,
                               "objective": {"as_built": {"cost": 0.09}, "compensated": {"cost": 0.09},
                                             "improvement": 0.0},
                               "compensators": {"refocus": {"offset": 0.0}},
                               "compensated": {"outcome": "PASS", "targets": _record(0.3)["targets"]}}}
    row = {"sample_id": "s0001", "outcome": "PASS", **cm.row_fields(record, PLAN)}
    summary = cm.summarize([row], PLAN, ["spot"], {"spot": "mm"}, {"outcome": "PASS", "target:spot": 0.0})
    assert summary["states"][cm.SOLVED] == 1 and summary["solved_without_improvement"] == 1
    assert "SOLVED without improvement | 1" in "\n".join(cm.report_lines(summary, _table))


# --------------------------------------------------------------------------------------------- not evaluated

def test_sample_run_without_the_compensation_evaluator_is_not_evaluated_not_failed():
    plain = {**_record(0.3), "state": "COMPLETED", "evaluator": "os_optics_kit.study.evaluators:run_project"}
    refused = {**_record(None, error="OPS_INVALID"), "state": "REFUSED", "evaluator": None}
    rows = [{"sample_id": "s0001", "outcome": "PASS", **cm.row_fields(plain, PLAN)},
            {"sample_id": "s0002", "outcome": "ERROR", **cm.row_fields(refused, PLAN)}]
    assert rows[0]["compensation:state"] == cm.NOT_EVALUATED
    assert rows[0]["compensation:reason"] == cm.NOT_RUN_REASON
    assert rows[1]["compensation:state"] == cm.FAILED and rows[1]["compensation:reason"] == "AS_BUILT_ERROR"
    command = f"os-optics-kit study run D --resume --evaluator {cm.EVALUATOR}"
    summary = cm.summarize(rows, PLAN, ["spot"], {"spot": "mm"}, {"outcome": "PASS", "target:spot": 0.0},
                           next_command=command)
    assert summary["states"] == {cm.SOLVED: 0, cm.FAILED: 1, cm.NOT_EVALUATED: 1}
    assert summary["failure_reasons"] == {"AS_BUILT_ERROR": 1}
    assert summary["not_evaluated"]["samples"] == 1 and summary["not_evaluated"]["sample_ids"] == ["s0001"]
    assert summary["not_evaluated"]["next"] == command
    assert summary["compensated_or_as_built"]["by_outcome"]["PASS"] == 1
    lines = "\n".join(cm.report_lines(summary, _table))
    assert f"NOT_EVALUATED: {cm.NOT_RUN_REASON} | 1" in lines and f"FAILED: {cm.NOT_RUN_REASON}" not in lines
    assert "FAILED | 1" in lines and command in lines


def test_explicit_run_project_evaluator_is_refused_for_a_compensated_study(tmp_path):
    from os_optics_kit import tolerance_study as ts

    spec = json.loads(REFOCUS.read_text(encoding="utf-8"))
    for evaluator in ("os_optics_kit.study.evaluators:run_project", cm.evaluators.run_project):
        with pytest.raises(wc.WorkflowError) as caught:
            ts.run_tolerance(spec, tmp_path / "s", spec_dir=REFOCUS.parent, evaluator=evaluator)
        assert caught.value.code == "EVALUATOR_MISMATCH"
        assert caught.value.details["recorded"] == cm.EVALUATOR
    assert not (tmp_path / "s").exists()


# --------------------------------------------------------------------------------------------- plan-time checks

def _fake_fields(monkeypatch, refuse_at=None):
    """Projects are ``{field: number}``; a compensator ``{"field", "mode"}`` sets the field to nominal + offset
    (``set``) or adds the offset (``move``); ``refuse_at``: an offset whose ops are refused."""
    def apply(doc, comps, offsets):
        out = dict(doc)
        for comp in comps:
            offset = offsets.get(comp["name"], 0.0)
            if refuse_at is not None and offset == refuse_at:
                raise wc.WorkflowError("PROJECT_OP_INVALID", "the selector matches nothing")
            out[comp["field"]] = comp["nominal"] + offset if comp["mode"] == "set" else out[comp["field"]] + offset
        return out
    monkeypatch.setattr(cm, "compensated_document", apply)


def _plan(*comps):
    return {**PLAN, "compensators": [{**COMP, "ops": [], **c} for c in comps]}


def test_set_compensator_on_a_perturbed_field_is_refused(monkeypatch):
    _fake_fields(monkeypatch)
    plan = _plan({"name": "waist_trim", "field": "waist", "mode": "set", "nominal": 500.0})
    base = {"waist": 500.0, "focus": 0.0}
    samples = [("s0001", {"waist": 500.0, "focus": 0.1}), ("s0002", {"waist": 511.9, "focus": 0.0})]
    with pytest.raises(wc.WorkflowError) as caught:
        cm.check_documents(plan, base, samples)
    assert caught.value.code == "COMPENSATOR_OVERLAP"
    assert caught.value.details["sample_id"] == "s0002" and caught.value.details["compensators"] == ["waist_trim"]


def test_compensator_nominal_that_changes_the_base_is_refused(monkeypatch):
    from os_optics_kit import sensitivity_analysis as sa

    _fake_fields(monkeypatch)
    monkeypatch.setattr(sa, "base_nominal", lambda document, comp: document[comp["field"]])
    plan = _plan({"name": "waist_trim", "field": "waist", "mode": "set", "nominal": 520.0})
    with pytest.raises(wc.WorkflowError) as caught:
        cm.check_documents(plan, {"waist": 500.0}, [("s0001", {"waist": 500.0})])
    assert caught.value.code == "NOMINAL_MISMATCH" and caught.value.details["compensators"] == ["waist_trim"]
    assert caught.value.details["base_values"] == {"waist_trim": 500.0}


def test_compensators_that_leave_offset_zero_as_built_are_accepted(monkeypatch):
    _fake_fields(monkeypatch)
    plan = _plan({"name": "waist_trim", "field": "waist", "mode": "set", "nominal": 500.0},
                 {"name": "refocus", "field": "focus", "mode": "move", "nominal": 0.0})
    base = {"waist": 500.0, "focus": 0.0}
    cm.check_documents(plan, base, [("s0001", {"waist": 500.0, "focus": 0.37})])  # focus perturbed: moved further


def test_compensator_ops_refused_at_a_bound_are_refused_before_any_run(monkeypatch):
    _fake_fields(monkeypatch, refuse_at=1.0)
    plan = _plan({"name": "refocus", "field": "focus", "mode": "move", "nominal": 0.0})
    with pytest.raises(wc.WorkflowError) as caught:
        cm.check_documents(plan, {"focus": 0.0}, [])
    assert caught.value.code == "SPEC_INVALID" and caught.value.details["compensator"] == "refocus"
    assert caught.value.details["offset"] == 1.0


def test_non_finite_goal_is_refused():
    kw = {"document": {}, "targets": ["spot"], "units": {"spot": None}, "parameter_names": []}
    raw = {"objective": [{"target": "spot", "goal": {"value": math.nan, "unit": "1"}, "scale": 1}],
           "compensators": [dict(COMP, ops=[])]}
    with pytest.raises(wc.WorkflowError) as caught:
        cm.normalize(raw, **kw)
    assert caught.value.code == "SPEC_INVALID" and "goal" in str(caught.value)


# --------------------------------------------------------------------------------------------- spec

def test_normalize_off_by_default_and_when_disabled():
    kw = {"document": {}, "targets": ["spot"], "units": {"spot": "mm"}, "parameter_names": []}
    assert cm.normalize(None, **kw) is None
    assert cm.normalize({"enabled": False}, **kw) is None
    assert cm.normalize({"objective": [], "compensators": []}, enabled=False, **kw) is None
    with pytest.raises(wc.WorkflowError):
        cm.normalize({"objective": [{"target": "nope", "scale": 1}], "compensators": []}, **kw)


def test_example_specs_declare_compensation():
    for path in (REFOCUS, MIRROR):
        spec = json.loads(path.read_text(encoding="utf-8"))
        assert spec["compensation"]["compensators"] and spec["samples"] <= 10


# --------------------------------------------------------------------------------------------- engine

@engine
@pytest.mark.parametrize("path", [REFOCUS, MIRROR], ids=["refocus", "mirror"])
def test_compensation_off_and_on_share_samples(path, tmp_path):
    from os_optics_kit import tolerance_study as ts

    spec = json.loads(path.read_text(encoding="utf-8"))
    spec["samples"], spec["max_evaluations"] = 2, 3
    off = ts.run_tolerance(spec, tmp_path / "off", spec_dir=path.parent, plots=False, report=False,
                           compensation=False)
    on = ts.run_tolerance(spec, tmp_path / "on", spec_dir=path.parent, plots=False, report=False)
    assert (tmp_path / "off/samples.csv").read_bytes() == (tmp_path / "on/samples.csv").read_bytes()
    assert "compensation" not in off and "result_stage" not in off["rows"][0]
    for a, b in zip(off["rows"], on["rows"], strict=True):
        assert a["sample_id"] == b["sample_id"] and a["result_hash"] == b["result_hash"]
        assert b["result_stage"] == "as_built" and b["compensation:state"] in (cm.SOLVED, cm.FAILED)
    assert on["compensation"]["nominal"]["outcome"] == on["nominal"]["outcome"]
    assert on["compensation"]["states"][cm.SOLVED] == 2
    brief = ts.compact(on)["compensation"]  # the stdout document carries the compensation block
    assert brief["states"] == on["compensation"]["states"]
    assert brief["failure_reasons"] == on["compensation"]["failure_reasons"]
    assert brief["compensated"] == {"by_outcome": on["compensation"]["compensated"]["by_outcome"],
                                    "denominator": 2}
    assert brief["runs_max"] == 1 + 2 * (spec["compensation"].get("max_evaluations", 100) + 2)
    assert "target_statistics" not in brief["as_built"]
    assert "compensation" not in ts.compact(off)


@engine
def test_spec_without_compensation_plans_identical_requests(tmp_path):
    from os_optics_kit import tolerance_study as ts

    spec = json.loads(REFOCUS.read_text(encoding="utf-8"))
    plain = copy.deepcopy(spec)
    plain.pop("compensation")
    ts.plan_tolerance(plain, tmp_path / "a", spec_dir=REFOCUS.parent)
    ts.plan_tolerance(spec, tmp_path / "b", spec_dir=REFOCUS.parent, compensation=False)
    for name in ("plan.jsonl", "samples.csv", "sampling.json"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes(), name


def _engine_failure_run(tmp_path, mutate):
    from os_optics_kit import tolerance_study as ts

    spec = json.loads(REFOCUS.read_text(encoding="utf-8"))
    spec["samples"], spec["max_evaluations"] = 2, 3
    mutate(spec["compensation"]["compensators"][0])
    off = ts.run_tolerance(spec, tmp_path / "off", spec_dir=REFOCUS.parent, plots=False, report=False,
                           compensation=False)
    on = ts.run_tolerance(spec, tmp_path / "on", spec_dir=REFOCUS.parent, plots=False, report=True)
    assert len(on["rows"]) == len(off["rows"]) == 2  # no sample is dropped
    for a, b in zip(off["rows"], on["rows"], strict=True):
        assert a["sample_id"] == b["sample_id"] and a["result_hash"] == b["result_hash"]
        assert a["outcome"] == b["outcome"] and b["result_stage"] == "as_built"
        for key in [k for k in a if k.startswith("target:")]:
            assert a[key] == b[key], key  # the as-built values stay
    return on


@engine
def test_engine_bounds_hit_keeps_as_built(tmp_path):
    def narrow(comp):
        comp["bounds"], comp["tolerance"] = [-0.01, 0.01], 0.0005
    on = _engine_failure_run(tmp_path, narrow)
    failed = [r for r in on["rows"] if r["compensation:state"] == cm.FAILED]
    assert failed and all(r["compensation:reason"] == "BOUNDS_HIT" for r in failed)
    assert on["compensation"]["failure_reasons"].get("BOUNDS_HIT") == len(failed)
    for r in failed:
        assert r.get("compensated:outcome") in (None, "")
    report = (tmp_path / "on" / "report" / "report.md").read_text(encoding="utf-8")
    assert "## Compensation" in report and "compensated_or_as_built" in report
    assert "FAILED: BOUNDS_HIT" in report


@engine
def test_engine_invalid_compensator_op_is_refused_before_any_run(tmp_path, monkeypatch):
    from os_optics_kit import tolerance_study as ts
    from os_optics_kit.study import evaluators

    spec = json.loads(REFOCUS.read_text(encoding="utf-8"))
    spec["samples"], spec["max_evaluations"] = 2, 3
    spec["compensation"]["compensators"][0]["ops"][0]["selector"] = {"ids": ["no-such-component"]}
    runs = []
    monkeypatch.setattr(evaluators, "run_project", lambda *a, **k: runs.append(1))
    for plan_only in (False, True):
        with pytest.raises(wc.WorkflowError) as caught:
            ts.run_tolerance(spec, tmp_path / "on", spec_dir=REFOCUS.parent, plots=False, report=False,
                             plan_only=plan_only)
        assert caught.value.code == "SPEC_INVALID" and caught.value.details["compensator"] == "refocus"
        assert "SELECTION_INVALID" in str(caught.value)
    assert runs == [] and not (tmp_path / "on").exists()


def _waist_spec(nominal=None, keep_parameter=False):
    """The refocus spec with a set_source_model compensator on the source waist (micrometres; base 500)."""
    spec = json.loads(REFOCUS.read_text(encoding="utf-8"))
    spec["samples"], spec["max_evaluations"] = 4, 5
    if not keep_parameter:
        spec["parameters"] = [p for p in spec["parameters"] if p["name"] != "source_waist_radius"]
    trim = {"name": "waist_trim", "unit": "um", "bounds": [-2, 2],
            "ops": [{"op": "set_source_model", "instance_id": "SRC", "changes": {"waist_radius_mm": "$value"}}]}
    if nominal is not None:
        trim["nominal"] = nominal
    spec["compensation"]["compensators"] = [trim]
    spec["compensation"]["objective"] = [{"target": "expanded_radius", "goal": 1.5, "scale": 0.001}]
    return spec


@engine
def test_engine_set_compensator_on_a_toleranced_field_is_refused(tmp_path):
    from os_optics_kit import tolerance_study as ts

    with pytest.raises(wc.WorkflowError) as caught:
        ts.plan_tolerance(_waist_spec(500, keep_parameter=True), tmp_path / "s", spec_dir=REFOCUS.parent)
    assert caught.value.code == "COMPENSATOR_OVERLAP"
    assert caught.value.details["compensators"] == ["waist_trim"]
    assert caught.value.details["parameters"] == ["source_waist_radius"]
    assert not (tmp_path / "s").exists()


@engine
def test_engine_compensator_nominal_off_the_base_is_nominal_mismatch(tmp_path):
    from os_optics_kit import tolerance_study as ts

    with pytest.raises(wc.WorkflowError) as caught:
        ts.plan_tolerance(_waist_spec(520), tmp_path / "s", spec_dir=REFOCUS.parent)
    assert caught.value.code == "NOMINAL_MISMATCH" and caught.value.details["compensators"] == ["waist_trim"]
    assert caught.value.details["base_values"]["waist_trim"] == pytest.approx(500.0)
    assert not (tmp_path / "s").exists()


@engine
@pytest.mark.parametrize("nominal", [500, None], ids=["given", "base"])
def test_engine_set_compensator_on_an_unperturbed_field_is_accepted(tmp_path, nominal):
    from os_optics_kit import tolerance_study as ts
    from os_optics_kit.study import store

    study_dir, normalized, _digest = ts.plan_tolerance(_waist_spec(nominal), tmp_path / "s",
                                                       spec_dir=REFOCUS.parent)
    assert normalized["compensation"]["compensators"][0]["nominal"] == pytest.approx(500.0)
    study = store.read_study(study_dir)
    assert study["evaluator"] == cm.EVALUATOR
    assert study["budget"]["runs_max"] == 1 + 4 * (40 + 2)


def _invoke(main, argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(argv)
    return code, json.loads(out.getvalue())


@engine
def test_engine_plan_only_then_study_run_and_resume_keep_the_compensation(tmp_path):
    from os_optics_kit import study_tool as st
    from os_optics_kit import tolerance_study as ts
    from os_optics_kit.study import store

    spec = json.loads(REFOCUS.read_text(encoding="utf-8"))
    spec["samples"], spec["max_evaluations"] = 2, 3
    study_dir = tmp_path / "p"
    planned = ts.run_tolerance(spec, study_dir, spec_dir=REFOCUS.parent, plots=False, report=False, plan_only=True)
    assert planned["status"] == "PLANNED" and planned["evaluator"] == cm.EVALUATOR
    assert planned["next"].endswith(f" --evaluator {cm.EVALUATOR}")
    assert store.read_study(study_dir)["evaluator"] == cm.EVALUATOR
    code, ran = _invoke(st.main, ["run", str(study_dir), "--evaluator", cm.EVALUATOR])
    assert code in (0, 1) and ran["compensation"]["states"][cm.SOLVED] == 2
    assert ran["compensation"]["states"][cm.NOT_EVALUATED] == 0
    sample = json.loads((study_dir / "samples/s0001/sample.json").read_text(encoding="utf-8"))
    assert sample["evaluator"] == cm.EVALUATOR and sample["compensation"]["state"] == cm.SOLVED
    before = json.loads((study_dir / "summary.json").read_text(encoding="utf-8"))["compensation"]
    code, resumed = _invoke(st.main, ["run", str(study_dir), "--resume", "--evaluator", cm.EVALUATOR])
    assert resumed["execution"]["ran"] == 0 and resumed["execution"]["reused"] == 3
    again = ts.run_tolerance(spec, study_dir, spec_dir=REFOCUS.parent, plots=False, report=False, resume=True)
    after = json.loads((study_dir / "summary.json").read_text(encoding="utf-8"))["compensation"]
    assert again["compensation"]["states"] == after["states"] == before["states"]
    assert ts.compact(again)["execution"]["ran"] == 0


def _sample_records(study_dir):
    return {path.parent.name: path.read_bytes() for path in sorted((study_dir / "samples").glob("*/sample.json"))}


def _tree(study_dir):
    return {path.relative_to(study_dir).as_posix(): path.read_bytes() for path in sorted(study_dir.rglob("*"))
            if path.is_file() and path.name != "owner.lock"}


@engine
def test_engine_study_run_uses_the_recorded_compensation_evaluator(tmp_path):
    """`study run` and `study status` of a compensated tolerance study use the evaluator study.json records: a resume
    without --evaluator keeps the compensated records, another evaluator is refused before anything is written."""
    from os_optics_kit import study_tool as st
    from os_optics_kit import tolerance_study as ts
    from os_optics_kit.study import evaluators, execute as ex

    spec = json.loads(REFOCUS.read_text(encoding="utf-8"))
    spec["samples"], spec["max_evaluations"] = 2, 3
    # planned only: every printed next command names the recorded evaluator, and `study run` without one compensates
    planned_dir = tmp_path / "p"
    ts.run_tolerance(spec, planned_dir, spec_dir=REFOCUS.parent, plots=False, report=False, plan_only=True)
    code, status = _invoke(st.main, ["status", str(planned_dir)])
    assert code == 0 and status["evaluator"] == cm.EVALUATOR
    assert status["next"] == f"os-optics-kit study run {planned_dir.as_posix()} --evaluator {cm.EVALUATOR}"
    code, ran = _invoke(st.main, ["run", str(planned_dir)])
    assert code in (0, 1) and ran["execution"]["ran"] == 3
    assert ran["compensation"]["states"] == {cm.SOLVED: 2, cm.FAILED: 0, cm.NOT_EVALUATED: 0}
    # executed: a resume without --evaluator runs nothing again and keeps every compensated record
    study_dir = tmp_path / "s"
    executed = ts.run_tolerance(spec, study_dir, spec_dir=REFOCUS.parent, plots=False, report=False)
    assert executed["compensation"]["states"][cm.SOLVED] == 2
    records = _sample_records(study_dir)
    assert len(records) == 3
    assert all(json.loads(r)["evaluator"] == cm.EVALUATOR for r in records.values())
    code, resumed = _invoke(st.main, ["run", str(study_dir), "--resume"])
    assert code in (0, 1)
    assert (resumed["execution"]["ran"], resumed["execution"]["reused"]) == (0, 3)
    assert resumed["compensation"]["states"] == executed["compensation"]["states"]
    assert _sample_records(study_dir) == records
    # another evaluator, named or as a function, with or without --resume: refused, nothing written
    before = _tree(study_dir)
    for argv in (["--resume", "--evaluator", evaluators.DEFAULT_EVALUATOR],
                 ["--evaluator", evaluators.DEFAULT_EVALUATOR]):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = st.main(["run", str(study_dir), *argv])
        error = json.loads(err.getvalue())["error"]
        assert code == 4 and out.getvalue() == ""
        assert (error["code"], error["recorded"], error["given"]) == (
            "EVALUATOR_MISMATCH", cm.EVALUATOR, evaluators.DEFAULT_EVALUATOR)
        assert _tree(study_dir) == before
    with pytest.raises(wc.WorkflowError) as caught:
        ex.execute_study(study_dir, evaluator=evaluators.run_project, resume=True)
    assert caught.value.code == "EVALUATOR_MISMATCH" and _tree(study_dir) == before


# --------------------------------------------------------------------------------------------- resume identity

def _normalized_plan(**changes):
    """A normalized plan as ``compensation.normalize`` writes it (with its description keys); ``changes``:
    ``comp.KEY`` (the compensator), ``term.KEY`` (the objective term) or a plan key."""
    comp = {**COMP, "nominal_source": "given", "ops": [{"op": "transform", "translation_mm": ["$value", 0, 0]}]}
    plan = {**PLAN, "compensators": [comp]}
    for key, value in changes.items():
        if key.startswith("comp."):
            comp[key[5:]] = value
        elif key.startswith("term."):
            plan["objective"] = [{**plan["objective"][0], key[5:]: value}]
        else:
            plan[key] = value
    return plan


RESULT_CHANGES = {"term.goal": 1.0, "term.scale": 0.5, "term.target": "other", "term.unit": "um",
                  "comp.name": "refocus2", "comp.unit": "um", "comp.nominal": 0.5, "comp.bounds": [-2.0, 2.0],
                  "comp.tolerance": 1e-3, "comp.ops": [{"op": "transform", "translation_mm": [0, "$value", 0]}],
                  "max_evaluations": 61, "max_sweeps": 4, "cost_tolerance": 1e-8}
DESCRIPTION_CHANGES = {"comp.nominal_source": "default", "objective_definition": "another text",
                       "solver": "another text"}


@pytest.mark.parametrize("key", sorted(RESULT_CHANGES))
def test_plan_identity_covers_every_result_setting(key):
    assert cm.plan_identity(_normalized_plan(**{key: RESULT_CHANGES[key]})) != cm.plan_identity(_normalized_plan())


@pytest.mark.parametrize("key", sorted(DESCRIPTION_CHANGES))
def test_plan_identity_ignores_descriptions(key):
    assert cm.plan_identity(_normalized_plan(**{key: DESCRIPTION_CHANGES[key]})) == cm.plan_identity(_normalized_plan())


def _labels(plan, role="sample"):
    from os_optics_kit.study import requests as rq

    return {"role": role, cm.PLAN_LABEL: plan, rq.EVALUATION_LABEL: cm.plan_identity(plan)}


def test_compensation_identity_enters_the_request_hash():
    from os_optics_kit.study import requests as rq

    def make(labels):
        return rq.make_request("s0001", {"offset": 0.0}, selected_targets=["spot"], labels=labels)

    base = make(_labels(_normalized_plan()))
    assert make(_labels(_normalized_plan(**{"term.goal": 1.0})))["request_hash"] != base["request_hash"]
    described = make(_labels(_normalized_plan(**DESCRIPTION_CHANGES)))
    assert described["request_hash"] == base["request_hash"] and described["labels"] != base["labels"]
    plain = make({"role": "sample"})  # a request without the label hashes as before
    assert plain["request_hash"] == rq.request_hash({"offset": 0.0}, ["spot"], plain["outputs"], None)
    assert plain["request_hash"] != base["request_hash"]
    with pytest.raises(wc.WorkflowError) as caught:
        make({"role": "sample", rq.EVALUATION_LABEL: "not a digest"})
    assert caught.value.code == "REQUEST_INVALID"


def goal_evaluator(request, sample_dir, limits=None):
    """A stand-in compensated evaluator: the record's ``spot`` value is the plan's objective goal (0 without a plan),
    and its ``compensation`` block names the objective it was evaluated with."""
    plan = (request["labels"] or {}).get(cm.PLAN_LABEL)
    goal = plan["objective"][0]["goal"] if plan else 0.0
    record = _record(goal)
    record["compensation"] = None if plan is None else {"state": cm.SOLVED, "objective": {"terms": plan["objective"]}}
    return record


GOAL = "tests.test_tolerance_compensation:goal_evaluator"


def _header():
    header = {"kind": "test.compensation", "engine": {}, "installed_catalog": {}}
    if HAS_ENGINE:
        header.update(engine=wc.engine_identity(), installed_catalog=wc.catalog_identity())
    return header


def _plan_requests(plan):
    from os_optics_kit.study import requests as rq

    out = [rq.make_request("s0000", {"offset": 0.0}, selected_targets=["spot"], labels={"role": "nominal"})]
    for sample_id in ("s0001", "s0002"):
        out.append(rq.make_request(sample_id, {"offset": 0.0, "id": sample_id}, selected_targets=["spot"],
                                   labels=_labels(plan)))
    return out


def test_resume_with_a_changed_plan_runs_the_compensated_samples_again(tmp_path):
    """Without an Engine: a changed result setting replaces the compensated records, a description change keeps them
    (relabelled), and the nominal record is kept throughout."""
    from os_optics_kit.study import execute as ex, store

    study_dir = tmp_path / "s"
    store.plan_study(study_dir, _header(), _plan_requests(_normalized_plan()))
    first = ex.execute_study(study_dir, evaluator=GOAL)
    assert (first.ran, first.reused) == (3, 0)
    store.plan_study(study_dir, _header(), _plan_requests(_normalized_plan(**{"term.goal": 1.0})), resume=True)
    changed = ex.execute_study(study_dir, evaluator=GOAL, resume=True)
    assert (changed.ran, changed.reused, changed.recovered) == (2, 1, 0)
    for sample_id in ("s0001", "s0002"):
        record = store.read_record(study_dir, sample_id)
        assert record["targets"][0]["value"] == 1.0
        assert record["compensation"]["objective"]["terms"][0]["goal"] == 1.0
        assert record[cm.PLAN_LABEL]["objective"][0]["goal"] == 1.0
    same = ex.execute_study(study_dir, evaluator=GOAL, resume=True)
    assert (same.ran, same.reused) == (0, 3)
    described = _normalized_plan(**{"term.goal": 1.0, **DESCRIPTION_CHANGES})
    store.plan_study(study_dir, _header(), _plan_requests(described), resume=True)
    kept = ex.execute_study(study_dir, evaluator=GOAL, resume=True)
    assert (kept.ran, kept.reused) == (0, 3)
    record = store.read_record(study_dir, "s0001")
    assert record[cm.PLAN_LABEL]["compensators"][0]["nominal_source"] == "default"  # relabelled
    assert record["targets"][0]["value"] == 1.0


# --------------------------------------------------------------------------------------------- interrupted samples

def _interrupted_sample(study_dir, evaluator):
    """A sample directory as an evaluation interrupted after its Engine run leaves it: attempt marker, run/ with a
    manifest, no sample.json."""
    from os_optics_kit.study import requests as rq, store

    request = rq.make_request("s0001", {"offset": 0.0}, selected_targets=["spot"], labels=_labels(_normalized_plan()))
    store.plan_study(study_dir, _header(), [request])
    run_dir = store.sample_dir(study_dir, "s0001") / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
    store.write_attempt(study_dir, "s0001", request["request_hash"], 1, evaluator=evaluator)
    return store.read_plan(study_dir)[0]


def test_interrupted_compensated_sample_is_not_read_back_from_its_as_built_run(tmp_path):
    from os_optics_kit.study import store

    entry = _interrupted_sample(tmp_path / "c", cm.EVALUATOR)
    assert not store.recoverable_run(tmp_path / "c", entry, cm.EVALUATOR)
    assert store.resume_selection(tmp_path / "c", [entry], cm.EVALUATOR) == ([], [], [entry])
    entry = _interrupted_sample(tmp_path / "d", cm.evaluators.DEFAULT_EVALUATOR)  # the default evaluator's record
    assert store.recoverable_run(tmp_path / "d", entry, cm.evaluators.DEFAULT_EVALUATOR)  # is its run: read back


# --------------------------------------------------------------------------------------------- per-run time limit

def _stopped(cause):
    record = wc._empty_record(None, None, ["spot"])
    record["error"] = {"stage": "run", "code": "RUN_STOPPED", "problems": [{"code": cause, "message": cause}]}
    return record


def _slow_run(sub, folder, limits):
    """A stand-in Engine run of ``project.run_s`` seconds (``project.trial_s`` for a solver trial) that honours the
    per-run time limit and a study cancellation as the Engine's run monitor does; spot = |offset - 0.25| + 0.01."""
    import time

    project = sub["project"]
    seconds = project["trial_s"] if Path(folder).name == "trial" else project["run_s"]
    limit = (limits or {}).get("time_limit_s")
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        if cm.evaluators.cancel_requested():
            return _stopped("STUDY_CANCELLED")
        if limit is not None and time.monotonic() - started > limit:
            return _stopped("TIME_LIMIT")
        time.sleep(0.01)
    return _record(abs(project.get("offset", 0.0) - 0.25) + 0.01)


def slow_compensated(request, sample_dir, limits=None):
    """``compensation.run_compensated`` with ``_slow_run`` as its Engine (compensator offsets travel in the
    Project); importable by name, so worker processes run it."""
    saved = cm.compensated_document
    cm.compensated_document = lambda doc, comps, offsets: {**doc, "offset": offsets[comps[0]["name"]]}
    try:
        return cm.run_compensated(request, sample_dir, limits, run=_slow_run)
    finally:
        cm.compensated_document = saved


slow_compensated.engine_runs = cm.engine_runs  # type: ignore[attr-defined]
SLOW = "tests.test_tolerance_compensation:slow_compensated"


def _slow_requests(run_s=0.05, trial_s=None, samples=("s0001",)):
    from os_optics_kit.study import requests as rq

    project = {"offset": 0.0, "run_s": run_s, "trial_s": run_s if trial_s is None else trial_s}
    out = [rq.make_request("s0000", {**project, "run_s": 0.01}, selected_targets=["spot"],
                           labels={"role": "nominal"})]
    for sample_id in samples:
        out.append(rq.make_request(sample_id, {**project, "id": sample_id}, selected_targets=["spot"],
                                   labels=_labels(PLAN)))
    return out


def test_engine_runs_per_evaluation_of_the_compensation_evaluator():
    from os_optics_kit.study import execute as ex, store

    entries = [store.plan_entry(r) for r in _slow_requests()]
    assert cm.engine_runs(entries[1]["labels"]) == PLAN["max_evaluations"] + 2
    assert ex.engine_runs_per_evaluation(entries, cm.EVALUATOR) == PLAN["max_evaluations"] + 2
    assert ex.engine_runs_per_evaluation(entries[:1], cm.EVALUATOR) == 1  # the nominal run only
    assert ex.engine_runs_per_evaluation(entries, cm.evaluators.DEFAULT_EVALUATOR) == 1


def test_worker_time_stop_allows_every_engine_run_of_a_compensated_sample(tmp_path, monkeypatch):
    """Process workers, 0.3 s per run: each of about 20 trials of 0.05 s stays within its own limit, so the sample
    is solved, although the whole sample takes longer than one run's limit plus the grace."""
    from os_optics_kit.study import execute as ex, store

    monkeypatch.setattr(ex, "TIME_LIMIT_GRACE_S", 0.2)
    study_dir = tmp_path / "s"
    store.plan_study(study_dir, _header(), _slow_requests())
    summary = ex.execute_study(study_dir, evaluator=SLOW, workers=2, limits={"time_limit_s": 0.3})
    assert summary.scheduler == "process" and summary.states == {"COMPLETED": 2}
    record = store.read_record(study_dir, "s0001")
    assert record["state"] == "COMPLETED" and record["outcome"] == "PASS"
    assert record["compensation"]["state"] == cm.SOLVED
    assert record["compensation"]["evaluations"] * 0.05 > 0.3 + 0.2  # the sample outlasted one run's stop


def test_trial_stopped_by_its_time_limit_keeps_the_as_built_result(tmp_path, monkeypatch):
    from os_optics_kit.study import execute as ex, store

    monkeypatch.setattr(ex, "TIME_LIMIT_GRACE_S", 0.2)
    study_dir = tmp_path / "s"
    store.plan_study(study_dir, _header(), _slow_requests(run_s=0.02, trial_s=5.0))
    summary = ex.execute_study(study_dir, evaluator=SLOW, workers=2, limits={"time_limit_s": 0.3})
    assert summary.states == {"COMPLETED": 2}
    record = store.read_record(study_dir, "s0001")
    assert (record["state"], record["outcome"], record["error"]) == ("COMPLETED", "PASS", None)
    assert record["targets"][0]["value"] == pytest.approx(0.26)  # the as-built value
    block = record["compensation"]
    assert block["state"] == cm.FAILED and block["reason"]["code"] == "RUN_STOPPED"
    assert block["reason"]["problems"] == ["TIME_LIMIT"] and block["compensated"] is None


def test_cancelled_compensation_in_a_worker_is_resumed(tmp_path):
    from os_optics_kit.study import execute as ex, schedulers, store

    study_dir = tmp_path / "s"
    store.plan_study(study_dir, _header(), _slow_requests(run_s=0.02, trial_s=0.2, samples=("s0001", "s0002")))
    scheduler = schedulers.ProcessScheduler(2)
    try:
        summary = ex.execute_study(study_dir, evaluator=SLOW, scheduler=scheduler,
                                   on_progress=lambda event: scheduler.cancel())  # after the nominal run
    finally:
        scheduler.close()
    assert summary.state == "CANCELLED"
    cancelled = store.read_record(study_dir, "s0001")
    assert cancelled["state"] == "CANCELLED"
    if cancelled.get("compensation"):  # stopped during its solve: the as-built result stays in the record
        assert cancelled["compensation"]["reason"]["code"] == "RUN_STOPPED"
        assert cancelled["targets"][0]["value"] == pytest.approx(0.26)
    resumed = ex.execute_study(study_dir, evaluator=SLOW, resume=True)
    assert resumed.state == "COMPLETED" and (resumed.reused, resumed.ran) == (1, 2)
    for sample_id in ("s0001", "s0002"):
        record = store.read_record(study_dir, sample_id)
        assert record["state"] == "COMPLETED" and record["compensation"]["state"] == cm.SOLVED


# --------------------------------------------------------------------------------------------- engine: resume

def _refocus(goal=0.0, **compensation):
    spec = json.loads(REFOCUS.read_text(encoding="utf-8"))
    spec["samples"], spec["max_evaluations"] = 2, 3
    spec["compensation"]["objective"][0]["goal"] = goal
    spec["compensation"].update(compensation)
    return spec


def _solved_goals(study_dir):
    """``{sample: (plan label goal, goal the solve used, state)}`` of the compensated samples."""
    out = {}
    for sample_id in ("s0001", "s0002"):
        record = json.loads((study_dir / "samples" / sample_id / "sample.json").read_text(encoding="utf-8"))
        block = record["compensation"]
        out[sample_id] = (record[cm.PLAN_LABEL]["objective"][0]["goal"], block["objective"]["terms"][0]["goal"],
                          block["state"])
    return out


@engine
def test_engine_resume_with_a_changed_compensation_plan(tmp_path):
    """refocus, objective goal 0 -> 1 mm: `tolerance --resume` and `tolerance --plan-only --resume` + `study run
    --resume` run both compensated samples again with the new plan and keep the nominal run; the same plan, or one
    that differs in its description only, reuses every sample."""
    from os_optics_kit import study_tool as st
    from os_optics_kit import tolerance_study as ts

    study_dir = tmp_path / "s"
    first = ts.run_tolerance(_refocus(0.0), study_dir, spec_dir=REFOCUS.parent, plots=False, report=False)
    assert first["compensation"]["states"][cm.SOLVED] == 2
    nominal = (study_dir / "samples/s0000/sample.json").read_bytes()
    # path 1: tolerance --resume with the changed plan
    changed = ts.run_tolerance(_refocus(1.0), study_dir, spec_dir=REFOCUS.parent, plots=False, report=False,
                               resume=True)
    brief = ts.compact(changed)["execution"]
    assert (brief["ran"], brief["reused"], brief["recovered"]) == (2, 1, 0)
    assert _solved_goals(study_dir) == {"s0001": (1.0, 1.0, cm.FAILED), "s0002": (1.0, 1.0, cm.FAILED)}
    assert changed["compensation"]["objective"][0]["goal"] == 1.0
    assert changed["compensation"]["failure_reasons"] == {"BOUNDS_HIT": 2}  # 1 mm spot: defocus to the bound
    assert (study_dir / "samples/s0000/sample.json").read_bytes() == nominal
    # path 2: plan only with the changed plan, then study run --resume
    ts.run_tolerance(_refocus(0.0), study_dir, spec_dir=REFOCUS.parent, plots=False, report=False, resume=True,
                     plan_only=True)
    code, ran = _invoke(st.main, ["run", str(study_dir), "--resume"])
    assert code in (0, 1) and (ran["execution"]["ran"], ran["execution"]["reused"]) == (2, 1)
    assert _solved_goals(study_dir) == {"s0001": (0.0, 0.0, cm.SOLVED), "s0002": (0.0, 0.0, cm.SOLVED)}
    records = _sample_records(study_dir)
    assert {k: json.loads(v)["compensation"]["compensated"]["identity"]["result_hash"]
            for k, v in records.items() if k != "s0000"} == {
        r["sample_id"]: r["compensated:result_hash"] for r in first["rows"]}  # the first solve again
    # the same plan, then one that differs only in its description and a nominal read instead of written: reused
    described = _refocus(0.0, description="refocus, described again")
    described["compensation"]["compensators"][0].pop("nominal")  # read from the base Project: 0 as written before
    for spec in (_refocus(0.0), described):
        again =ts.run_tolerance(spec, study_dir, spec_dir=REFOCUS.parent, plots=False, report=False, resume=True)
        brief = ts.compact(again)["execution"]
        assert (brief["ran"], brief["reused"]) == (0, 3)
        assert again["compensation"]["states"] == ran["compensation"]["states"]
    record = json.loads((study_dir / "samples/s0001/sample.json").read_text(encoding="utf-8"))
    assert record[cm.PLAN_LABEL]["compensators"][0]["nominal_source"] == "default"  # relabelled
    assert {k: json.loads(v)["compensation"] for k, v in _sample_records(study_dir).items()} == {
        k: json.loads(v)["compensation"] for k, v in records.items()}


# --------------------------------------------------------------------------------------------- engine: interrupted

def _interrupt(study_dir, sample_id, evaluator):
    """What an evaluation interrupted after its as-built Engine run leaves: the run directory, the attempt marker,
    no sample.json."""
    from os_optics_kit.study import store

    request = store.read_request(study_dir, sample_id)
    store.sample_path(study_dir, sample_id).unlink()
    store.write_attempt(study_dir, sample_id, request["request_hash"], 1, evaluator=evaluator, execution="gone")
    assert (store.sample_dir(study_dir, sample_id) / "run" / "manifest.json").is_file()


@engine
def test_engine_interrupted_compensation_runs_again_on_resume(tmp_path):
    """An attempt marker and a verified as-built run directory without sample.json: the resume runs the sample
    again and records the full compensated result (twice in a row); the next resume reuses it."""
    from os_optics_kit import tolerance_study as ts

    spec = _refocus(0.0)
    study_dir = tmp_path / "s"
    first = ts.run_tolerance(spec, study_dir, spec_dir=REFOCUS.parent, plots=False, report=False)
    expected = {r["sample_id"]: (r["result_hash"], r["compensation:state"], r["compensated:result_hash"])
                for r in first["rows"]}
    assert all(state == cm.SOLVED for _h, state, _c in expected.values())
    for _round in range(2):
        _interrupt(study_dir, "s0001", cm.EVALUATOR)
        resumed = ts.run_tolerance(spec, study_dir, spec_dir=REFOCUS.parent, plots=False, report=False,
                                   resume=True)
        brief = ts.compact(resumed)["execution"]
        assert (brief["ran"], brief["reused"], brief["recovered"]) == (1, 2, 0)
        record = json.loads((study_dir / "samples/s0001/sample.json").read_text(encoding="utf-8"))
        assert record["worker"] != "recovered" and record["compensation"]["state"] == cm.SOLVED
        assert {r["sample_id"]: (r["result_hash"], r["compensation:state"], r["compensated:result_hash"])
                for r in resumed["rows"]} == expected
        assert resumed["compensation"]["states"] == {cm.SOLVED: 2, cm.FAILED: 0, cm.NOT_EVALUATED: 0}
        again = ts.run_tolerance(spec, study_dir, spec_dir=REFOCUS.parent, plots=False, report=False, resume=True)
        assert (ts.compact(again)["execution"]["ran"], ts.compact(again)["execution"]["reused"]) == (0, 3)
        assert again["compensation"]["states"] == resumed["compensation"]["states"]


@engine
def test_engine_interrupted_plain_tolerance_sample_is_still_read_back(tmp_path):
    from os_optics_kit import tolerance_study as ts

    spec = _refocus(0.0)
    study_dir = tmp_path / "s"
    first = ts.run_tolerance(spec, study_dir, spec_dir=REFOCUS.parent, plots=False, report=False,
                             compensation=False)
    _interrupt(study_dir, "s0001", cm.evaluators.DEFAULT_EVALUATOR)
    resumed = ts.run_tolerance(spec, study_dir, spec_dir=REFOCUS.parent, plots=False, report=False,
                               compensation=False, resume=True)
    brief = ts.compact(resumed)["execution"]
    assert (brief["ran"], brief["reused"], brief["recovered"]) == (0, 2, 1)
    record = json.loads((study_dir / "samples/s0001/sample.json").read_text(encoding="utf-8"))
    assert record["worker"] == "recovered"
    assert [r["result_hash"] for r in resumed["rows"]] == [r["result_hash"] for r in first["rows"]]


# --------------------------------------------------------------------------------------------- engine: time limit

@engine
def test_engine_worker_time_stop_counts_every_engine_run_of_a_sample(tmp_path, monkeypatch):
    """Two workers and a 1 s limit per Engine run (each run of this design takes a fraction of it): about 34 trials
    make one sample longer than a single run's limit, and the sample is still solved, not terminated."""
    from os_optics_kit import tolerance_study as ts
    from os_optics_kit.study import execute as ex

    monkeypatch.setattr(ex, "TIME_LIMIT_GRACE_S", 0.0)
    spec = _refocus(0.0)
    spec["samples"], spec["max_evaluations"] = 1, 2
    spec["compensation"]["compensators"][0]["tolerance"] = 1e-6
    summary = ts.run_tolerance(spec, tmp_path / "s", spec_dir=REFOCUS.parent, plots=False, report=False,
                               workers=2, limits={"time_limit_s": 1.0})
    brief = ts.compact(summary)
    assert brief["execution"]["scheduler"] == "process" and brief["execution"]["ran"] == 2
    record = json.loads((tmp_path / "s/samples/s0001/sample.json").read_text(encoding="utf-8"))
    assert record["state"] == "COMPLETED" and record["error"] is None
    assert record["compensation"]["state"] == cm.SOLVED and record["compensation"]["evaluations"] > 20
    assert record["timing"]["run_s"] < 1.0  # the as-built run alone is well within its limit
