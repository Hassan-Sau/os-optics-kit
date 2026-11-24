"""Compensation of tolerance samples (study.compensation): solver, failure reasons, stages, off by default."""

from __future__ import annotations

import copy
import importlib.util
import json
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


def _record(value, error=None):
    record = wc._empty_record(None, None, ["spot"])
    if error:
        record["error"] = {"stage": "run", "code": error, "problems": [{"code": "X"}]}
        return record
    record.update(status="PASS", outcome="PASS", execution={"outcome": "COMPLETED"},
                  targets=[{"id": "spot", "value": value, "unit": "mm", "status": "PASS", "assessment": "MET",
                            "quantity_state": "EXACT"}],
                  identity={**record["identity"], "result_hash": f"h{value}"})
    return record


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
    assert summary["requested"] == 1 and summary["states"] == {cm.SOLVED: 0, cm.FAILED: 1}
    assert summary["failure_reasons"] == {"BOUNDS_HIT": 1}
    assert summary["compensated_or_as_built"]["by_outcome"]["PASS"] == 1
    assert summary["stages"] == ["nominal", "as_built", "compensated"]


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


@engine
def test_engine_invalid_compensator_op_is_evaluation_error(tmp_path):
    def invalid(comp):
        comp["ops"][0]["selector"] = {"ids": ["no-such-component"]}
    on = _engine_failure_run(tmp_path, invalid)
    assert all(r["compensation:state"] == cm.FAILED and r["compensation:reason"] == "EVALUATION_ERROR"
               for r in on["rows"])
    assert on["compensation"]["failure_reasons"] == {"EVALUATION_ERROR": 2}
    assert on["compensation"]["states"][cm.SOLVED] == 0
    report = (tmp_path / "on" / "report" / "report.md").read_text(encoding="utf-8")
    assert "## Compensation" in report and "compensated_or_as_built" in report
    assert "FAILED: EVALUATION_ERROR" in report
