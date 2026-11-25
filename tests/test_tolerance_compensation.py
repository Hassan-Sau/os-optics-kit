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
