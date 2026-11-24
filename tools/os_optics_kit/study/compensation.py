"""Caller-declared compensation of tolerance samples: as-built run, bounded solve of the compensators, compensated run.

Compensation is optional and off by default: a tolerance spec without a ``compensation`` object (or with
``"enabled": false``, or run with ``--no-compensation``) plans, runs and aggregates exactly as before. With it, every
saved sample is evaluated three ways, each result labelled by its ``result_stage``:

* ``nominal``: the s0000 run of the base Project (never compensated);
* ``as_built``: the sample's Project with its sampled perturbations applied (the sample record itself, unchanged);
* ``compensated``: the as-built Project with the declared compensators applied at the solved values
  (``record["compensation"]["compensated"]``).

Spec (keys; quantities are a bare number in the stated unit or ``{"value": x, "unit": "u"}``)::

    "compensation": {
      "enabled": true,                                   # default true when the object is given
      "objective": [{"target": "spot_radius", "goal": 0, "scale": 0.02}],   # goal, scale in the target unit
      "compensators": [{"name": "refocus", "unit": "mm", "ops": [... "$value" ...],
                        "nominal": 0, "bounds": [-1, 1], "tolerance": 0.001}],
      "max_evaluations": 100, "max_sweeps": 4, "cost_tolerance": 1e-6
    }

The objective is ``sum(((value - goal) / scale) ** 2)`` over the objective targets of one run. A compensator's ops
are applied to the as-built Project with ``"$value"`` = nominal + offset (as tolerance parameters), so a transform
op moves or tilts the as-built component further; its offset is searched within ``bounds`` (offsets, compensator
unit) to within ``tolerance`` (default 1e-3 of the bounds width). Solver: cyclic coordinate descent, one golden-section
line search over the full bounds per compensator per sweep, starting at offset 0 (the as-built state, so the
solution is never worse than as-built); converged when a single compensator's line search finished, or when a full
sweep improved the objective by at most ``cost_tolerance``.

A solve that fails keeps the as-built result and records a structured reason (``reason.code``):
``AS_BUILT_ERROR`` (the as-built run gave no usable objective), ``EVALUATION_ERROR`` (a trial run failed or gave no
finite objective value), ``NOT_CONVERGED`` (``max_evaluations`` or ``max_sweeps`` reached first), ``BOUNDS_HIT``
(the best offset lies at a bound: the optimum may lie outside), ``RUN_STOPPED`` (a trial was stopped: the time
limit or a cancellation). The sample is never dropped: its outcome stays the as-built outcome.
"""

from __future__ import annotations

import math
from pathlib import Path
import shutil

from .. import workflow_common as wc
from . import evaluators, store

EVALUATOR = "os_optics_kit.study.compensation:run_compensated"
PLAN_LABEL = "compensation_plan"
STAGES = ("nominal", "as_built", "compensated")
SOLVED, FAILED = "SOLVED", "FAILED"
REASONS = ("AS_BUILT_ERROR", "EVALUATION_ERROR", "NOT_CONVERGED", "BOUNDS_HIT", "RUN_STOPPED")
DEFAULTS = {"max_evaluations": 100, "max_sweeps": 4, "cost_tolerance": 1e-6}
TRACE_LIMIT = 200
_INVPHI = (math.sqrt(5.0) - 1.0) / 2.0


# --------------------------------------------------------------------------------------------- spec

def _positive_int(raw, key):
    value = raw.get(key, DEFAULTS[key])
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise wc.WorkflowError("SPEC_INVALID", f"compensation.{key} must be a positive integer")
    return value


def normalize(raw, *, document, targets, units, parameter_names, enabled=True):
    """The validated compensation of a tolerance spec, or None when it is off (absent, ``enabled: false`` or
    ``enabled`` False here). ``document``: the base Project (nominals of set_* ops); ``targets``: target ids of the
    base Project; ``units``: ``{target id: unit}``; ``parameter_names``: tolerance parameter names (no clash)."""
    from .. import sensitivity_analysis as sa

    if raw is None or not enabled:
        return None
    if not isinstance(raw, dict):
        raise wc.WorkflowError("SPEC_INVALID", "compensation must be an object")
    known = {"enabled", "objective", "compensators", "max_evaluations", "max_sweeps", "cost_tolerance",
             "description"}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise wc.WorkflowError("SPEC_INVALID", f"compensation: unknown keys {unknown}; known: {sorted(known)}")
    flag = raw.get("enabled", True)
    if not isinstance(flag, bool):
        raise wc.WorkflowError("SPEC_INVALID", "compensation.enabled must be true or false")
    if not flag:
        return None
    objective = raw.get("objective")
    if not isinstance(objective, list) or not objective:
        raise wc.WorkflowError("SPEC_INVALID", "compensation.objective must list at least one target term")
    terms = []
    for index, term in enumerate(objective):
        if not isinstance(term, dict) or not isinstance(term.get("target"), str):
            raise wc.WorkflowError("SPEC_INVALID", f"compensation.objective[{index}] needs a target id")
        target_id = term["target"]
        if target_id not in targets:
            raise wc.WorkflowError("TARGET_UNKNOWN", f"compensation objective target {target_id!r} is not in the "
                                   f"base Project; known: {list(targets)}", unknown=[target_id], known=list(targets))
        unit = units.get(target_id)
        goal = sa.quantity(term.get("goal", 0.0), unit, f"compensation.objective[{index}].goal")
        if "scale" not in term:
            raise wc.WorkflowError("SPEC_INVALID", f"compensation.objective[{index}] needs a scale (target unit)")
        scale = sa.quantity(term["scale"], unit, f"compensation.objective[{index}].scale")
        if not scale > 0 or not math.isfinite(scale):
            raise wc.WorkflowError("SPEC_INVALID", f"compensation.objective[{index}].scale must be positive")
        terms.append({"target": target_id, "unit": unit, "goal": goal, "scale": scale})
    compensators = raw.get("compensators")
    if not isinstance(compensators, list) or not compensators:
        raise wc.WorkflowError("SPEC_INVALID", "compensation.compensators must list at least one compensator")
    out, names = [], set(parameter_names)
    for index, comp in enumerate(compensators):
        if not isinstance(comp, dict):
            raise wc.WorkflowError("SPEC_INVALID", f"compensation.compensators[{index}] must be an object")
        common = sa.common_parameter({k: v for k, v in comp.items() if k not in ("bounds", "tolerance")}, index,
                                     base=document)
        if common["name"] in names:
            raise wc.WorkflowError("SPEC_INVALID", f"compensator name {common['name']!r} is already used by a "
                                   "parameter or compensator")
        names.add(common["name"])
        bounds = comp.get("bounds")
        if not isinstance(bounds, list) or len(bounds) != 2:
            raise wc.WorkflowError("SPEC_INVALID", f"compensator {common['name']!r}: bounds must be [low, high]")
        low = sa.quantity(bounds[0], common["unit"], f"compensator {common['name']} bounds[0]")
        high = sa.quantity(bounds[1], common["unit"], f"compensator {common['name']} bounds[1]")
        if not low <= 0.0 <= high or not low < high:
            raise wc.WorkflowError("SPEC_INVALID", f"compensator {common['name']!r}: bounds need low <= 0 <= high "
                                   "and low < high (offsets from the as-built state)")
        tolerance = (sa.quantity(comp["tolerance"], common["unit"], f"compensator {common['name']} tolerance")
                     if "tolerance" in comp else (high - low) * 1e-3)
        if not tolerance > 0 or not math.isfinite(tolerance):
            raise wc.WorkflowError("SPEC_INVALID", f"compensator {common['name']!r}: tolerance must be positive")
        out.append({**common, "bounds": [low, high], "tolerance": tolerance})
    cost_tolerance = raw.get("cost_tolerance", DEFAULTS["cost_tolerance"])
    if isinstance(cost_tolerance, bool) or not isinstance(cost_tolerance, (int, float)) or not cost_tolerance >= 0:
        raise wc.WorkflowError("SPEC_INVALID", "compensation.cost_tolerance must be a number >= 0")
    return {"objective": terms, "compensators": out, "max_evaluations": _positive_int(raw, "max_evaluations"),
            "max_sweeps": _positive_int(raw, "max_sweeps"), "cost_tolerance": float(cost_tolerance),
            "objective_definition": "sum over objective targets of ((value - goal) / scale)^2",
            "solver": "cyclic coordinate descent, golden-section line search within bounds, start at offset 0"}


def describe(normalized):
    """One line per compensator for reports."""
    return [f"{c['name']} [{c['unit'] or '1'}] within [{c['bounds'][0]:.6g}, {c['bounds'][1]:.6g}]"
            for c in normalized["compensators"]]


# --------------------------------------------------------------------------------------------- objective / solver

class SolveFailure(Exception):
    """A trial evaluation that gave no objective (``reason``: structured)."""

    def __init__(self, reason):
        super().__init__(reason.get("message"))
        self.reason = reason


class _Budget(Exception):
    pass


def objective_value(record, terms):
    """``(cost, {target: value})`` of a run record, or raise SolveFailure (code EVALUATION_ERROR)."""
    if record.get("error"):
        error = record["error"]
        code = "RUN_STOPPED" if error.get("code") == "RUN_STOPPED" else "EVALUATION_ERROR"
        raise SolveFailure({"code": code, "message": f"the run failed ({error.get('code')})",
                            "run_error": {k: error.get(k) for k in ("stage", "code")},
                            "problems": sorted({p.get("code") for p in error.get("problems") or []} - {None})})
    rows = {t.get("id"): t for t in record.get("targets") or []}
    cost, values = 0.0, {}
    for term in terms:
        target = rows.get(term["target"])
        value = None if target is None else target.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise SolveFailure({"code": "EVALUATION_ERROR", "message": f"objective target {term['target']!r} has "
                                "no finite value", "target": term["target"],
                                "value_kind": None if target is None else wc.target_value_use(target)["kind"]})
        values[term["target"]] = value
        cost += ((value - term["goal"]) / term["scale"]) ** 2
    return cost, values


def solve(evaluate, compensators, *, max_evaluations, max_sweeps, cost_tolerance, start_cost=None):
    """Minimise ``evaluate(offsets) -> cost`` over the compensator offsets within their bounds (module docstring).

    ``evaluate`` raises SolveFailure for a failed trial. ``start_cost``: the known cost at offset 0 (the as-built
    run; not evaluated again). Returns ``{state, reason, offsets, cost, start_cost, evaluations, sweeps, converged,
    at_bounds, trace}``; never raises for a failed trial."""
    names = [c["name"] for c in compensators]
    trace, cache = [], {}
    best = {"offsets": {n: 0.0 for n in names}, "cost": start_cost}
    count = {"n": 0}

    def f(offsets):
        key = tuple(offsets[n] for n in names)
        if key in cache:
            return cache[key]
        if count["n"] >= max_evaluations:
            raise _Budget()
        count["n"] += 1
        cost = evaluate(dict(offsets))
        cache[key] = cost
        if len(trace) < TRACE_LIMIT:
            trace.append({"offsets": dict(offsets), "cost": cost})
        if best["cost"] is None or cost < best["cost"]:
            best.update(offsets=dict(offsets), cost=cost)
        return cost

    def result(state, reason, converged, sweeps):
        at_bounds = [c["name"] for c in compensators
                     if min(abs(best["offsets"][c["name"]] - b) for b in c["bounds"]) <= c["tolerance"]]
        return {"state": state, "reason": reason, "offsets": best["offsets"], "cost": best["cost"],
                "start_cost": start_cost, "evaluations": count["n"], "sweeps": sweeps, "converged": converged,
                "at_bounds": at_bounds, "trace": trace}

    sweeps, converged = 0, False
    try:
        if start_cost is None:
            f(best["offsets"])
        else:
            cache[tuple(0.0 for _ in names)] = start_cost
        while sweeps < max_sweeps:
            sweeps += 1
            before = best["cost"]
            for comp in compensators:
                _line_search(f, best, comp)
            if len(compensators) == 1 or before - best["cost"] <= cost_tolerance:
                converged = True
                break
    except _Budget:
        pass
    except SolveFailure as exc:
        return result(FAILED, exc.reason, False, sweeps)
    if not converged:
        return result(FAILED, {"code": "NOT_CONVERGED", "message": f"no convergence within {max_evaluations} "
                               f"evaluations and {max_sweeps} sweeps", "evaluations": count["n"],
                               "sweeps": sweeps}, False, sweeps)
    out = result(SOLVED, None, True, sweeps)
    if out["at_bounds"]:
        out.update(state=FAILED, reason={"code": "BOUNDS_HIT", "message": "the best offset lies at a bound of "
                                         f"{out['at_bounds']}: the optimum may lie outside the bounds",
                                         "compensators": out["at_bounds"]})
    return out


def _line_search(f, best, comp):
    """Golden-section search of one compensator's offset over its bounds, the others at the best point."""
    name, (a, b), tol = comp["name"], comp["bounds"], comp["tolerance"]
    base = dict(best["offsets"])

    def g(x):
        return f({**base, name: x})

    c, d = b - _INVPHI * (b - a), a + _INVPHI * (b - a)
    fc, fd = g(c), g(d)
    while b - a > tol:
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - _INVPHI * (b - a)
            fc = g(c)
        else:
            a, c, fc = c, d, fd
            d = a + _INVPHI * (b - a)
            fd = g(d)
    for edge in comp["bounds"]:  # a minimum at a bound is evaluated there, so BOUNDS_HIT can be told
        if abs(edge - (a + b) / 2.0) <= tol:
            g(edge)


# --------------------------------------------------------------------------------------------- documents

def compensated_document(document, compensators, offsets):
    """The as-built ``document`` with every compensator's ops applied at nominal + offset."""
    from .. import sensitivity_analysis as sa
    from ..edit_project import apply_edits

    ops = []
    for comp in compensators:
        ops.extend(sa.parameter_ops(comp, offsets.get(comp["name"], 0.0)))
    return apply_edits(document, ops, validate=False)["project"]


def _sub_request(request, project, outputs):
    return {"sample_id": request["sample_id"], "project": project, "selected_targets": request["selected_targets"],
            "outputs": outputs}


def _stage_record(record, stage, sample_id, folder_name):
    kept = {k: record.get(k) for k in ("status", "engine_status", "execution", "in_model_scope", "model_scope_issues",
                                       "out_of_model_issues", "targets", "selected_targets", "warnings", "error",
                                       "identity", "seconds")}
    kept["outcome"] = wc.record_outcome(record)
    kept["result_stage"] = stage
    relative = f"{store.SAMPLES_DIR}/{sample_id}/{folder_name}"
    kept["project_file"] = f"{relative}/project.json"
    kept["run_dir"] = f"{relative}/run" if record.get("run_dir") else None
    return kept


def compensate(request, record, sample_dir, plan, run=None, limits=None):
    """The ``compensation`` block of an as-built ``record`` (module docstring); ``run(sub_request, folder, limits)``
    evaluates one Project (default ``evaluators.run_project``)."""
    run = evaluators.run_project if run is None else run
    sample_dir = Path(sample_dir)
    compensators, terms = plan["compensators"], plan["objective"]
    block = {"state": FAILED, "reason": None, "objective": {"as_built": None, "compensated": None, "terms": terms},
             "compensators": {c["name"]: {"offset": None, "value": None, "unit": c["unit"], "bounds": c["bounds"]}
                              for c in compensators},
             "evaluations": 0, "sweeps": 0, "converged": False, "best_trial": None, "trace": [],
             "compensated": None}
    try:
        start_cost, start_values = objective_value(record, terms)
    except SolveFailure as exc:
        block["reason"] = {**exc.reason, "code": "RUN_STOPPED" if exc.reason["code"] == "RUN_STOPPED"
                           else "AS_BUILT_ERROR", "message": f"as-built: {exc.reason['message']}"}
        return block
    block["objective"]["as_built"] = {"cost": start_cost, "values": start_values}
    trial_dir = sample_dir / "compensation" / "trial"
    trial_outputs = {"run_directory": False, "ray_data": "summary"}

    def evaluate(offsets):
        try:
            project = compensated_document(request["project"], compensators, offsets)
        except wc.WorkflowError as exc:
            raise SolveFailure({"code": "EVALUATION_ERROR", "message": f"compensator ops refused: {exc}",
                                "offsets": offsets, "error_code": exc.code}) from None
        trial = run(_sub_request(request, project, trial_outputs), trial_dir, limits)
        try:
            return objective_value(trial, terms)[0]
        except SolveFailure as exc:
            raise SolveFailure({**exc.reason, "offsets": offsets}) from None

    solved = solve(evaluate, compensators, max_evaluations=plan["max_evaluations"], max_sweeps=plan["max_sweeps"],
                   cost_tolerance=plan["cost_tolerance"], start_cost=start_cost)
    shutil.rmtree(sample_dir / "compensation", ignore_errors=True)
    block.update({"evaluations": solved["evaluations"], "sweeps": solved["sweeps"], "converged": solved["converged"],
                  "trace": solved["trace"], "best_trial": {"offsets": solved["offsets"], "cost": solved["cost"],
                                                            "at_bounds": solved["at_bounds"]}})
    if solved["state"] != SOLVED:
        block["reason"] = solved["reason"]
        return block
    offsets = solved["offsets"]
    project = compensated_document(request["project"], compensators, offsets)
    final = run(_sub_request(request, project, request["outputs"]), sample_dir / "compensated", limits)
    try:
        cost, values = objective_value(final, terms)
    except SolveFailure as exc:
        block["reason"] = {**exc.reason, "message": f"compensated run: {exc.reason['message']}"}
        return block
    block.update(state=SOLVED, compensated=_stage_record(final, "compensated", request["sample_id"], "compensated"))
    block["objective"]["compensated"] = {"cost": cost, "values": values}
    for comp in compensators:
        block["compensators"][comp["name"]].update(offset=offsets[comp["name"]],
                                                   value=comp["nominal"] + offsets[comp["name"]])
    return block


def run_compensated(request, sample_dir, limits=None):
    """Evaluator of a compensated tolerance study: the as-built run (the sample record, as ``run_project``), then
    for a sample with a compensation plan the solve and the compensated run (``record["compensation"]``)."""
    record = evaluators.run_project(request, sample_dir, limits)
    plan = (request.get("labels") or {}).get(PLAN_LABEL)
    if plan is None:
        record["result_stage"] = "nominal" if (request.get("labels") or {}).get("role") == "nominal" else "as_built"
        record["compensation"] = None
        return record
    record["result_stage"] = "as_built"
    block = compensate(request, record, sample_dir, plan, limits=limits)
    record["compensation"] = block
    if (block.get("reason") or {}).get("code") == "RUN_STOPPED" and evaluators.cancel_requested():
        record["state"] = "CANCELLED"  # the study was cancelled during the solve: a resume evaluates it again
    return record


# --------------------------------------------------------------------------------------------- aggregation

def row_fields(record, plan):
    """Table columns of a sample record's compensation (``compensation:*``, ``compensated:*``)."""
    block = record.get("compensation") or {}
    reason = block.get("reason") or {}
    row = {"result_stage": record.get("result_stage") or "as_built",
           "compensation:state": block.get("state"), "compensation:reason": reason.get("code"),
           "compensation:evaluations": block.get("evaluations"),
           "compensation:objective_as_built": ((block.get("objective") or {}).get("as_built") or {}).get("cost"),
           "compensation:objective_compensated": ((block.get("objective") or {}).get("compensated") or {}).get("cost")}
    for comp in plan["compensators"]:
        entry = (block.get("compensators") or {}).get(comp["name"]) or {}
        row[f"compensator:{comp['name']}"] = entry.get("offset")
        row[f"compensator:{comp['name']}:unit"] = comp["unit"]
    compensated = block.get("compensated") or {}
    row["compensated:outcome"] = compensated.get("outcome")
    for target in compensated.get("targets") or []:
        row[f"compensated:target:{target['id']}"] = target.get("value")
        row[f"compensated:target:{target['id']}:status"] = target.get("status")
        use = wc.target_value_use(target)
        row[f"compensated:target:{target['id']}:value_kind"] = use["kind"]
    row["compensated:result_hash"] = (compensated.get("identity") or {}).get("result_hash")
    row["compensated:run_dir"] = compensated.get("run_dir")
    return row


def summarize(rows, plan, targets, units, nominal_row):
    """The ``compensation`` block of a tolerance summary: per stage outcome counts and target statistics."""
    total = len(rows)
    states = {SOLVED: 0, FAILED: 0}
    reasons = {}
    for row in rows:
        states[row.get("compensation:state") or FAILED] = states.get(row.get("compensation:state") or FAILED, 0) + 1
        if row.get("compensation:reason"):
            reasons[row["compensation:reason"]] = reasons.get(row["compensation:reason"], 0) + 1

    def outcomes(key, fallback=None):
        out = {o: 0 for o in wc.OUTCOMES}
        for row in rows:
            value = row.get(key) or (row.get(fallback) if fallback else None)
            if value is not None:
                out[value] += 1
        return out

    def stats(prefix, population):
        out = {}
        for target_id in targets:
            values = [r.get(f"{prefix}target:{target_id}") for r in population
                      if r.get(f"{prefix}target:{target_id}:value_kind") == "EXACT"]
            values = [v for v in values if isinstance(v, (int, float))]
            entry = {"unit": units.get(target_id), "count": len(values), "denominator": total}
            if values:
                entry.update({"min": float(min(values)), "max": float(max(values)),
                              "mean": float(math.fsum(values) / len(values)),
                              "max_abs": float(max(abs(v) for v in values))})
            out[target_id] = entry
        return out

    with_fallback = {o: 0 for o in wc.OUTCOMES}
    for row in rows:
        with_fallback[row.get("compensated:outcome") or row["outcome"]] += 1
    return {
        "stages": list(STAGES),
        "objective": plan["objective"], "objective_definition": plan["objective_definition"],
        "compensators": [{"name": c["name"], "unit": c["unit"], "nominal": c["nominal"], "bounds": c["bounds"],
                          "tolerance": c["tolerance"]} for c in plan["compensators"]],
        "solver": {k: plan[k] for k in ("solver", "max_evaluations", "max_sweeps", "cost_tolerance")},
        "states": states, "failure_reasons": dict(sorted(reasons.items())), "requested": total,
        "nominal": {"outcome": nominal_row["outcome"],
                    "targets": {t: nominal_row.get(f"target:{t}") for t in targets}},
        "as_built": {"by_outcome": outcomes("outcome"), "target_statistics": stats("", rows)},
        "compensated": {"by_outcome": outcomes("compensated:outcome"), "denominator": states[SOLVED],
                        "target_statistics": stats("compensated:", [r for r in rows
                                                                     if r.get("compensation:state") == SOLVED])},
        "compensated_or_as_built": {"by_outcome": with_fallback, "definition": "the compensated outcome of a solved "
                                    "sample, the as-built outcome of a failed one"},
    }
