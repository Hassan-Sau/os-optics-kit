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

The objective is ``sum(((value - goal) / scale) ** 2)`` over the objective targets of one run; only exact values
(``workflow_common.target_value_use`` kind ``EXACT``) enter it. A compensator's ops are applied to the as-built
Project with ``"$value"`` = nominal + offset (as tolerance parameters), so a transform op moves or tilts the as-built
component further; its offset is searched within ``bounds`` (offsets, compensator unit) to within ``tolerance``
(default 1e-3 of the bounds width). Solver: cyclic coordinate descent, one golden-section line search over the full
bounds per compensator per sweep, starting at offset 0; converged when a single compensator's line search finished,
or when a full sweep improved the objective by at most ``cost_tolerance``. A trial whose objective target has no
exact value (bounded, outside the model, unknown or ungraded) is infeasible: it is kept in the trace with ``cost``
null and never selected.

Before any run the compensators are checked against the base Project and every drawn sample, so that offset 0 is
the as-built state and an offset is the move from it: the ops must apply at offset 0 and at both bounds
(``SPEC_INVALID``); at offset 0 they must leave the base Project unchanged (``NOMINAL_MISMATCH``: state the base
value as ``nominal`` or omit it); and at offset 0 they must leave every as-built sample unchanged
(``COMPENSATOR_OVERLAP``: a set_* compensator that writes a field a tolerance parameter perturbs would overwrite the
sampled error; use a transform op, which moves further, or another field).

A solve that fails keeps the as-built result and records a structured reason (``reason.code``):
``AS_BUILT_ERROR`` (the as-built run gave no exact objective value, or its request was refused),
``EVALUATION_ERROR`` (a trial run failed, no trial gave exact objective values, or the final compensated run gave no
exact objective value), ``NOT_CONVERGED`` (``max_evaluations`` or ``max_sweeps`` reached first), ``BOUNDS_HIT``
(the best offset lies at a bound: the optimum may lie outside), ``RUN_STOPPED`` (a trial was stopped: the time
limit or a cancellation), ``WORSE_THAN_AS_BUILT`` (the final compensated run, with the study's full outputs, has a
higher objective than as-built beyond a relative 1e-9). The sample is never dropped: its outcome stays the as-built
outcome. A SOLVED sample records ``objective.improvement`` (as-built cost minus compensated cost); the summary counts
the solved samples without a measurable improvement separately (``solved_without_improvement``). A sample record written
by another evaluator than the compensation evaluator (``EVALUATOR``, which the study records in ``study.json``; for
example a record from before the study recorded it) has state ``NOT_EVALUATED`` (reason ``COMPENSATION_NOT_RUN``),
never ``FAILED``.

Runs: the tolerance spec's top-level ``max_evaluations`` caps the planned runs (nominal + one per sample); each
sample then runs at most ``compensation.max_evaluations`` trials plus one final compensated run, so a study runs at
most ``1 + samples * (compensation.max_evaluations + 2)`` Engine runs (``runs_max``). ``--time-limit-s`` and
``--max-memory-mib`` apply to each Engine run (the as-built run, each trial, the compensated run); a worker process
is terminated only when one sample's evaluation passes ``engine_runs`` times the time limit plus the executor's grace
(``execute.TIME_LIMIT_GRACE_S``).

Identity: each sample request carries the plan (label ``compensation_plan``) and the SHA-256 of its result-affecting
settings (label ``evaluation_sha256``, ``plan_identity``: objective terms, compensators with their ops, nominal,
bounds and tolerance, ``max_evaluations``, ``max_sweeps``, ``cost_tolerance``), which enters the request hash. A
resume with a changed plan therefore runs the compensated samples again (the nominal run is kept); a change that
leaves these settings as they are (a description, whether a nominal was given or read from the base Project) keeps
them. An evaluation interrupted after its as-built run is run again on resume: the saved as-built run directory alone
is not the evaluator's result.
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
SOLVED, FAILED, NOT_EVALUATED = "SOLVED", "FAILED", "NOT_EVALUATED"
REASONS = ("AS_BUILT_ERROR", "EVALUATION_ERROR", "NOT_CONVERGED", "BOUNDS_HIT", "RUN_STOPPED", "WORSE_THAN_AS_BUILT")
NOT_RUN_REASON = "COMPENSATION_NOT_RUN"
#: Relative objective change against as-built below which it counts as unchanged: a larger increase of the final
#: compensated run is refused (WORSE_THAN_AS_BUILT), a smaller decrease is no measurable improvement.
WORSE_TOLERANCE = 1e-9
#: Relative difference of a written number from the field's value that still counts as the same document (unit
#: conversion round-off of a nominal read from the base Project).
SAME_FIELD_TOLERANCE = 1e-12
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
        if not math.isfinite(goal):
            raise wc.WorkflowError("SPEC_INVALID", f"compensation.objective[{index}].goal must be a finite number",
                                   target=target_id)
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
        if not low < 0.0 < high:
            raise wc.WorkflowError("SPEC_INVALID", f"compensator {common['name']!r}: bounds need low < 0 < high "
                                   "(offsets from the as-built state, which must lie strictly inside)")
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


def runs_max(plan, samples):
    """Most Engine runs of a compensated study: nominal, then per sample the as-built run, the trials and the
    compensated run."""
    return 1 + samples * (plan["max_evaluations"] + 2)


#: Keys of a normalized plan that decide a compensated result (``plan_identity``); the others are descriptions.
IDENTITY_KEYS = {"objective": ("target", "unit", "goal", "scale"),
                 "compensators": ("name", "unit", "nominal", "ops", "bounds", "tolerance")}


def plan_identity(plan):
    """SHA-256 of the settings of a normalized plan that decide a compensated result: the objective terms, the
    compensators (name, unit, nominal, ops, bounds, tolerance) and the solver settings ``max_evaluations``,
    ``max_sweeps`` and ``cost_tolerance``. Descriptions (``nominal_source``, ``objective_definition``, ``solver``)
    are left out."""
    basis = {key: [{field: entry.get(field) for field in fields} for entry in plan[key]]
             for key, fields in IDENTITY_KEYS.items()}
    basis.update({key: plan[key] for key in ("max_evaluations", "max_sweeps", "cost_tolerance")})
    return wc.canonical_digest(basis)


def engine_runs(labels):
    """Most Engine runs one evaluation by ``run_compensated`` makes for a request with ``labels``: the as-built run,
    then with a plan at most ``max_evaluations`` trials and the compensated run (the executor scales the worker's
    hard time stop by it)."""
    plan = (labels or {}).get(PLAN_LABEL)
    return 1 if not isinstance(plan, dict) else 1 + int(plan.get("max_evaluations") or DEFAULTS["max_evaluations"]) + 1


# --------------------------------------------------------------------------------------------- plan-time checks

def _changed_fields(before, after):
    """``{field: (before, after)}`` of the numbers ``after`` changes beyond round-off (``SAME_FIELD_TOLERANCE``);
    ``{"document": ...}`` when the documents differ otherwise; ``{}`` for the same document."""
    from .. import sensitivity_analysis as sa

    if wc.canonical_digest(before) == wc.canonical_digest(after):
        return {}
    fields = sa._written_fields(before, after, before)
    if not fields:
        return {"document": ("differs", "differs")}
    out = {}
    for key, (old, new, _unit) in fields.items():
        if isinstance(old, (int, float)) and not isinstance(old, bool) and math.isclose(
                old, new, rel_tol=SAME_FIELD_TOLERANCE, abs_tol=0.0):
            continue
        out[".".join(str(part) for part in key if part is not None)] = (old, new)
    return out


def check_documents(plan, base, samples, parameters=()):
    """Refuse, before any run, compensators whose offset 0 is not the as-built state (module docstring).

    ``base``: the base Project; ``samples``: ``[(sample_id, as-built Project)]`` (refused samples left out);
    ``parameters``: the normalized tolerance parameters (named in a ``COMPENSATOR_OVERLAP`` refusal). Raises
    ``SPEC_INVALID`` (ops refused at offset 0 or a bound), ``NOMINAL_MISMATCH`` or ``COMPENSATOR_OVERLAP``."""
    from .. import sensitivity_analysis as sa

    compensators = plan["compensators"]
    for comp in compensators:
        for offset in (0.0, *comp["bounds"]):
            try:
                compensated_document(base, [comp], {comp["name"]: offset})
            except wc.WorkflowError as exc:
                raise wc.WorkflowError("SPEC_INVALID", f"compensator {comp['name']!r}: its ops are refused on the "
                                       f"base Project at offset {offset:.6g}: {exc}", compensator=comp["name"],
                                       offset=offset, error=exc.as_dict()) from None
    mismatched, changed = [], {}
    for comp in compensators:
        fields = _changed_fields(base, compensated_document(base, [comp], {comp["name"]: 0.0}))
        if fields:
            mismatched.append(comp["name"])
            changed[comp["name"]] = {k: {"base": v[0], "written": v[1]} for k, v in fields.items()}
    if mismatched:
        base_values = {}
        for comp in compensators:
            if comp["name"] in mismatched:
                try:
                    base_values[comp["name"]] = sa.base_nominal(base, comp)
                except wc.WorkflowError as exc:
                    base_values[comp["name"]] = exc.details.get("base_values")
        raise wc.WorkflowError("NOMINAL_MISMATCH", "at offset 0 the ops of these compensators change the base "
                               "Project, so offset 0 would not be the as-built state; set each compensator's nominal "
                               "to the base value of the field it edits, or omit it to use the base value",
                               compensators=mismatched, base_values=base_values, fields=changed)
    writers = None
    zeros = {c["name"]: 0.0 for c in compensators}
    for sample_id, document in samples:
        if not _changed_fields(document, compensated_document(document, compensators, zeros)):
            continue
        overlapping, fields = [], {}
        for comp in compensators:
            found = _changed_fields(document, compensated_document(document, [comp], zeros))
            if found:
                overlapping.append(comp["name"])
                fields.update(found)
        if writers is None:
            writers = _parameter_fields(base, parameters)
        named = sorted(name for name, written in writers.items() if set(written) & set(fields))
        raise wc.WorkflowError(
            "COMPENSATOR_OVERLAP", f"sample {sample_id}: at offset 0 the ops of compensators {overlapping} change the "
            f"as-built Project (fields {sorted(fields)}), overwriting the sampled perturbation of parameters "
            f"{named or [p['name'] for p in parameters]}; a compensator must not set a field a tolerance parameter "
            "perturbs (a transform op moves the as-built component further instead)",
            sample_id=sample_id, compensators=overlapping or [c["name"] for c in compensators],
            parameters=named, fields=sorted(fields))


def _parameter_fields(base, parameters):
    """``{parameter: [field]}`` of the fields each ops parameter writes (two probe offsets), for naming overlaps."""
    from .. import sensitivity_analysis as sa

    out = {}
    for parameter in parameters:
        if not parameter.get("ops"):
            continue
        written = set()
        for probe in sa._PROBES:
            try:
                written |= set(_changed_fields(base, sa.offset_document(base, [parameter],
                                                                        {parameter["name"]: probe})))
            except wc.WorkflowError:
                continue
        out[parameter["name"]] = sorted(written)
    return out


# --------------------------------------------------------------------------------------------- objective / solver

class SolveFailure(Exception):
    """A trial evaluation that gave no objective (``reason``: structured)."""

    def __init__(self, reason):
        super().__init__(reason.get("message"))
        self.reason = reason


class NotExact(SolveFailure):
    """A run whose objective target value is not exact (bounded, outside the model, unknown or ungraded): an
    infeasible trial, never a selected one."""


class _Budget(Exception):
    pass


def objective_value(record, terms):
    """``(cost, {target: value})`` of a run record from exact target values only, or raise SolveFailure (code
    EVALUATION_ERROR; NotExact for a value that is not exact, with its ``value_kind`` and ``codes``)."""
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
        use = wc.target_value_use(target)
        if not wc.value_usable(use["kind"], "exact"):
            raise NotExact({"code": "EVALUATION_ERROR", "message": f"objective target {term['target']!r} value is "
                            f"{use['kind']}, not an exact value", "target": term["target"],
                            "value_kind": use["kind"], "codes": use["codes"]})
        values[term["target"]] = value
        cost += ((value - term["goal"]) / term["scale"]) ** 2
    if not math.isfinite(cost):
        raise SolveFailure({"code": "EVALUATION_ERROR", "message": "the objective is not finite", "values": values})
    return cost, values


def solve(evaluate, compensators, *, max_evaluations, max_sweeps, cost_tolerance, start_cost=None):
    """Minimise ``evaluate(offsets) -> cost`` over the compensator offsets within their bounds (module docstring).

    ``evaluate`` raises SolveFailure for a failed trial (the solve stops, FAILED) and NotExact for an infeasible one
    (cost +inf: never selected; trace ``cost`` null with its ``infeasible`` reason). ``start_cost``: the known cost
    at offset 0 (the as-built run, when offset 0 reproduces it; not evaluated again), else offset 0 is evaluated.
    Returns ``{state, reason, offsets, cost, start_cost, evaluations, sweeps, converged, at_bounds, trace}``; never
    raises for a failed trial."""
    names = [c["name"] for c in compensators]
    trace, cache = [], {}
    best = {"offsets": {n: 0.0 for n in names}, "cost": start_cost}
    count = {"n": 0}
    infeasible = {"last": None}

    def f(offsets):
        key = tuple(offsets[n] for n in names)
        if key in cache:
            return cache[key]
        if count["n"] >= max_evaluations:
            raise _Budget()
        count["n"] += 1
        entry = {"offsets": dict(offsets)}
        try:
            cost = evaluate(dict(offsets))
        except NotExact as exc:
            cost = math.inf
            infeasible["last"] = exc.reason
            entry["infeasible"] = {k: exc.reason.get(k) for k in ("target", "value_kind", "codes")}
        cache[key] = cost
        if len(trace) < TRACE_LIMIT:
            trace.append({**entry, "cost": cost if math.isfinite(cost) else None})
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
    if best["cost"] is None or not math.isfinite(best["cost"]):
        return result(FAILED, {**(infeasible["last"] or {}), "code": "EVALUATION_ERROR",
                               "message": "no trial gave exact objective values", "evaluations": count["n"]},
                      False, sweeps)
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
    for stale in ("compensation", "compensated"):  # files of an interrupted or superseded evaluation
        shutil.rmtree(sample_dir / stale, ignore_errors=True)
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
            raise type(exc)({**exc.reason, "offsets": offsets}) from None

    # the as-built cost stands for offset 0 only when offset 0 reproduces the as-built Project (checked for the
    # whole study before any run, check_documents); otherwise offset 0 is evaluated like any trial
    try:
        zero = compensated_document(request["project"], compensators, {c["name"]: 0.0 for c in compensators})
    except wc.WorkflowError:
        zero = None
    reproduces = zero is not None and wc.canonical_digest(zero) == wc.canonical_digest(request["project"])
    solved = solve(evaluate, compensators, max_evaluations=plan["max_evaluations"], max_sweeps=plan["max_sweeps"],
                   cost_tolerance=plan["cost_tolerance"], start_cost=start_cost if reproduces else None)
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
    if cost > start_cost + WORSE_TOLERANCE * max(1.0, abs(start_cost)):
        block["reason"] = {"code": "WORSE_THAN_AS_BUILT", "message": "the compensated run has a higher objective "
                           "than the as-built run", "as_built_cost": start_cost, "compensated_cost": cost}
        block["objective"]["compensated"] = {"cost": cost, "values": values}
        return block
    block.update(state=SOLVED, compensated=_stage_record(final, "compensated", request["sample_id"], "compensated"))
    block["objective"]["compensated"] = {"cost": cost, "values": values}
    block["objective"]["improvement"] = start_cost - cost
    for comp in compensators:
        block["compensators"][comp["name"]].update(offset=offsets[comp["name"]],
                                                   value=comp["nominal"] + offsets[comp["name"]])
    return block


def run_compensated(request, sample_dir, limits=None, run=None):
    """Evaluator of a compensated tolerance study: the as-built run (the sample record, as ``run_project``), then
    for a sample with a compensation plan the solve and the compensated run (``record["compensation"]``).
    ``run(sub_request, folder, limits)`` evaluates one Project (default ``evaluators.run_project``)."""
    run = evaluators.run_project if run is None else run
    record = run(request, sample_dir, limits)
    plan = (request.get("labels") or {}).get(PLAN_LABEL)
    if plan is None:
        record["result_stage"] = "nominal" if (request.get("labels") or {}).get("role") == "nominal" else "as_built"
        record["compensation"] = None
        return record
    record["result_stage"] = "as_built"
    block = compensate(request, record, sample_dir, plan, run=run, limits=limits)
    record["compensation"] = block
    if (block.get("reason") or {}).get("code") == "RUN_STOPPED" and evaluators.cancel_requested():
        record["state"] = "CANCELLED"  # the study was cancelled during the solve: a resume evaluates it again
    return record


run_compensated.engine_runs = engine_runs  # type: ignore[attr-defined]


# --------------------------------------------------------------------------------------------- aggregation

def row_fields(record, plan):
    """Table columns of a sample record's compensation (``compensation:*``, ``compensated:*``). A record without a
    compensation block was refused before any run (FAILED, AS_BUILT_ERROR) or evaluated without the compensation
    evaluator (NOT_EVALUATED, COMPENSATION_NOT_RUN)."""
    block = record.get("compensation")
    if block is None:
        refused = record.get("state") == "REFUSED"
        block = {"state": FAILED if refused else NOT_EVALUATED,
                 "reason": {"code": "AS_BUILT_ERROR" if refused else NOT_RUN_REASON}}
    reason = block.get("reason") or {}
    objective = block.get("objective") or {}
    row = {"result_stage": record.get("result_stage") or "as_built",
           "compensation:state": block.get("state"), "compensation:reason": reason.get("code"),
           "compensation:evaluations": block.get("evaluations"),
           "compensation:objective_as_built": (objective.get("as_built") or {}).get("cost"),
           "compensation:objective_compensated": (objective.get("compensated") or {}).get("cost"),
           "compensation:improvement": objective.get("improvement")}
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


def _fmt(value):
    return "-" if value is None else f"{value:.6g}"


def report_lines(block, table):
    """Report lines of a summary's ``compensation`` block; ``table(header, rows)`` renders a markdown table."""
    lines = ["## Compensation", "", f"Compensators: {'; '.join(describe(block))}. Objective: "
             f"{block['objective_definition']} over {', '.join(t['target'] for t in block['objective'])} (exact "
             f"values only). Solver: {block['solver']['solver']} (at most {block['solver']['max_evaluations']} "
             f"trial runs per sample; at most {block['runs_max']} Engine runs in the study).", ""]
    stages = [("nominal", None), ("as_built", block["as_built"]["by_outcome"]),
              ("compensated", block["compensated"]["by_outcome"]),
              ("compensated_or_as_built", block["compensated_or_as_built"]["by_outcome"])]
    outcomes = list(wc.OUTCOMES)
    rows = []
    for name, counts in stages:
        if counts is None:
            counts = {o: int(o == block["nominal"]["outcome"]) for o in outcomes}
            denominator = 1
        else:
            denominator = block["compensated"]["denominator"] if name == "compensated" else block["requested"]
        rows.append([name, *[counts.get(o, 0) for o in outcomes], denominator])
    lines += table(["stage", *outcomes, "of"], rows) + [""]
    state_rows = [[k, v] for k, v in block["states"].items()]
    state_rows += [[f"FAILED: {k}", v] for k, v in block["failure_reasons"].items()]
    if block["states"].get(NOT_EVALUATED):
        state_rows.append([f"NOT_EVALUATED: {NOT_RUN_REASON}", block["states"][NOT_EVALUATED]])
    state_rows.append(["SOLVED without improvement", block["solved_without_improvement"]])
    lines += table(["solve state", "samples"], state_rows) + [""]
    not_evaluated = block["not_evaluated"]
    if not_evaluated["samples"]:
        text = f"{not_evaluated['samples']} samples were evaluated without compensation: {not_evaluated['message']}."
        if not_evaluated["next"]:
            text += f" Evaluate them with `{not_evaluated['next']}`."
        lines += [text, ""]
    stat_rows = []
    for target_id, value in block["nominal"]["targets"].items():
        stat_rows.append([target_id, "nominal", 1, _fmt(value), _fmt(value), _fmt(value), _fmt(value)])
        for stage in ("as_built", "compensated"):
            s = block[stage]["target_statistics"][target_id]
            stat_rows.append([target_id, stage, f"{s['count']}/{s['denominator']}", _fmt(s.get("min")),
                              _fmt(s.get("mean")), _fmt(s.get("max")), _fmt(s.get("max_abs"))])
    lines += ["Target values (exact values only; compensated over solved samples):", ""]
    lines += table(["target", "stage", "count", "min", "mean", "max", "max abs"], stat_rows) + [""]
    return lines


def summarize(rows, plan, targets, units, nominal_row, next_command=None):
    """The ``compensation`` block of a tolerance summary: per stage outcome counts and target statistics.
    ``next_command``: the command that evaluates the samples left NOT_EVALUATED with compensation."""
    total = len(rows)
    states = {SOLVED: 0, FAILED: 0, NOT_EVALUATED: 0}
    reasons = {}
    not_evaluated, unimproved = [], 0
    for row in rows:
        state = row.get("compensation:state") or NOT_EVALUATED
        states[state] = states.get(state, 0) + 1
        if state == FAILED and row.get("compensation:reason"):
            reasons[row["compensation:reason"]] = reasons.get(row["compensation:reason"], 0) + 1
        elif state == NOT_EVALUATED:
            not_evaluated.append(row["sample_id"])
        elif state == SOLVED:
            improvement, as_built = row.get("compensation:improvement"), row.get("compensation:objective_as_built")
            if (isinstance(improvement, (int, float)) and isinstance(as_built, (int, float))
                    and improvement <= WORSE_TOLERANCE * max(1.0, abs(as_built))):
                unimproved += 1

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
    shown = wc.bounded(not_evaluated, 10)
    return {
        "stages": list(STAGES),
        "objective": plan["objective"], "objective_definition": plan["objective_definition"],
        "compensators": [{"name": c["name"], "unit": c["unit"], "nominal": c["nominal"], "bounds": c["bounds"],
                          "tolerance": c["tolerance"]} for c in plan["compensators"]],
        "solver": {k: plan[k] for k in ("solver", "max_evaluations", "max_sweeps", "cost_tolerance")},
        "runs_max": runs_max(plan, total),
        "states": states, "failure_reasons": dict(sorted(reasons.items())), "requested": total,
        "solved_without_improvement": unimproved,
        "not_evaluated": {"samples": len(not_evaluated), "reason": NOT_RUN_REASON if not_evaluated else None,
                          "sample_ids": shown["shown"], "sample_ids_omitted": shown["omitted"],
                          "message": "their records hold the as-built run only, and the compensated stage counts "
                          "solved samples only" if not_evaluated else None,
                          "next": next_command if not_evaluated else None},
        "nominal": {"outcome": nominal_row["outcome"],
                    "targets": {t: nominal_row.get(f"target:{t}") for t in targets}},
        "as_built": {"by_outcome": outcomes("outcome"), "target_statistics": stats("", rows)},
        "compensated": {"by_outcome": outcomes("compensated:outcome"), "denominator": states[SOLVED],
                        "target_statistics": stats("compensated:", [r for r in rows
                                                                     if r.get("compensation:state") == SOLVED])},
        "compensated_or_as_built": {"by_outcome": with_fallback, "definition": "the compensated outcome of a solved "
                                    "sample, the as-built outcome of any other"},
    }


def brief(block):
    """The bounded stdout part of a summary's ``compensation`` block (no per-target statistics)."""
    return {"stages": block["stages"], "objective_definition": block["objective_definition"],
            "objective": block["objective"], "compensators": block["compensators"], "runs_max": block["runs_max"],
            "states": block["states"], "failure_reasons": block["failure_reasons"],
            "solved_without_improvement": block["solved_without_improvement"],
            "not_evaluated": block["not_evaluated"], "requested": block["requested"],
            "as_built": {"by_outcome": block["as_built"]["by_outcome"]},
            "compensated": {"by_outcome": block["compensated"]["by_outcome"],
                            "denominator": block["compensated"]["denominator"]},
            "compensated_or_as_built": block["compensated_or_as_built"]}
