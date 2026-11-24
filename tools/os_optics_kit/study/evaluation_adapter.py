"""Evaluation adapter: parameters -> requested analyses -> numerical results, on the Engine's ``EvaluationSession``.

A :class:`Problem` declares

* ``variables``: named parameters with ``bounds`` and a ``scale``, each applied to the base Project by op templates
  (``"$value"`` = the variable's value, in its ``unit``; the edit ops of ``os-optics-kit edit``);
* ``objectives``: quantities to drive toward a ``goal``, each scaled, weighted and combined by the problem's
  ``aggregation`` (``sum_squares``, ``sum_abs`` or ``max_abs`` of the scaled errors);
* ``constraints``: quantities with a ``minimum`` and/or ``maximum``; each reports its ``margin`` (scaled distance to
  the nearest bound, positive when satisfied). MTF is read as a constraint only (``{"mtf": ...}`` sources are refused
  in an objective): an MTF floor at a stated frequency, not a value to optimize.

A quantity ``source`` is ``{"target": TARGET_ID}`` (a declared target's value, evaluated through the Engine's
``requirements``) or ``{"mtf": {"observation": ID, "frequency_cyc_per_mm": F, "direction": "tangential" |
"sagittal", "field": N}}`` (the imaging analysis of that plane; ``field`` is the ``SRC#N`` index of the analysis,
default 0). The adapter asks the session only for what the sources need (selective evaluation) and carries the
Engine's scope in every result: ``requested``, ``executed``, ``not_computed[]`` and ``targets_not_evaluated[]`` of
``ValidationResult.evaluation``, with each quantity's ``state`` (``QuantityState``, ``NOT_COMPUTED`` included) and its
``value_basis``.

:meth:`Adapter.evaluate` never raises for a candidate and never drops one: it returns a :class:`CandidateResult` whose
``residuals`` always has ``len(objectives) + len(constraints)`` entries (objective errors, then constraint
violations ``max(0, -margin)``). An invalid candidate carries ``reason`` = ``{"code", "stage", "message", ...}`` and
residuals filled with ``invalid_residual`` (finite, so a least-squares solver can still step away from it). Codes:
``OUT_OF_BOUNDS``, ``EDIT_REFUSED``, ``PROJECT_REFUSED``, ``PLAN_REFUSED``, ``RUN_STOPPED``, ``ANALYSIS_REFUSED``,
``QUANTITY_UNAVAILABLE`` (a quantity was not computed or has no usable value), ``ENGINE_ERROR``.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import math
from typing import Any, Callable, Sequence

AGGREGATIONS = ("sum_squares", "sum_abs", "max_abs")
REASON_CODES = ("OUT_OF_BOUNDS", "EDIT_REFUSED", "PROJECT_REFUSED", "PLAN_REFUSED", "RUN_STOPPED",
                "ANALYSIS_REFUSED", "QUANTITY_UNAVAILABLE", "ENGINE_ERROR")
USABLE_STATES = ("KNOWN", "KNOWN_ZERO")
DEFAULT_INVALID_RESIDUAL = 1.0e3
DEFAULT_IMAGING = {"launch_grid": 24, "include_psf_map": False}


class ProblemError(ValueError):
    """A problem declaration that cannot be evaluated (raised when the problem is built, never per candidate)."""


@dataclass(frozen=True)
class Variable:
    name: str
    ops: Sequence[dict[str, Any]]
    bounds: tuple[float, float]
    nominal: float = 0.0
    scale: float = 1.0
    unit: str | None = None

    def __post_init__(self) -> None:
        low, high = self.bounds
        if not (math.isfinite(low) and math.isfinite(high) and low < high):
            raise ProblemError(f"variable {self.name!r}: bounds must be finite with low < high")
        if not (math.isfinite(self.scale) and self.scale > 0):
            raise ProblemError(f"variable {self.name!r}: scale must be positive")
        if not low <= self.nominal <= high:
            raise ProblemError(f"variable {self.name!r}: nominal lies outside its bounds")


@dataclass(frozen=True)
class Objective:
    name: str
    source: dict[str, Any]
    goal: float = 0.0
    scale: float = 1.0
    weight: float = 1.0


@dataclass(frozen=True)
class Constraint:
    name: str
    source: dict[str, Any]
    minimum: float | None = None
    maximum: float | None = None
    scale: float = 1.0


def _check_source(owner: str, source: dict[str, Any], *, allow_mtf: bool) -> None:
    if not isinstance(source, dict) or len(source) != 1 or next(iter(source)) not in ("target", "mtf"):
        raise ProblemError(f"{owner}: source is {{'target': ID}} or {{'mtf': {{...}}}}")
    if "mtf" in source:
        if not allow_mtf:
            raise ProblemError(f"{owner}: MTF is a constraint, not an objective; declare it under constraints")
        spec = source["mtf"]
        if not isinstance(spec, dict) or "observation" not in spec or "frequency_cyc_per_mm" not in spec:
            raise ProblemError(f"{owner}: mtf needs observation and frequency_cyc_per_mm")
        if spec.get("direction", "tangential") not in ("tangential", "sagittal"):
            raise ProblemError(f"{owner}: mtf direction is tangential or sagittal")


@dataclass(frozen=True)
class Problem:
    project: dict[str, Any]
    variables: Sequence[Variable]
    objectives: Sequence[Objective] = ()
    constraints: Sequence[Constraint] = ()
    aggregation: str = "sum_squares"
    imaging: dict[str, Any] = field(default_factory=dict)
    invalid_residual: float = DEFAULT_INVALID_RESIDUAL

    def __post_init__(self) -> None:
        if self.aggregation not in AGGREGATIONS:
            raise ProblemError(f"aggregation is one of {', '.join(AGGREGATIONS)}")
        if not self.objectives and not self.constraints:
            raise ProblemError("a problem needs at least one objective or constraint")
        names = [v.name for v in self.variables]
        if len(set(names)) != len(names):
            raise ProblemError("variable names must be unique")
        for o in self.objectives:
            _check_source(f"objective {o.name!r}", o.source, allow_mtf=False)
            if not o.scale > 0 or not o.weight >= 0:
                raise ProblemError(f"objective {o.name!r}: scale > 0 and weight >= 0")
        for c in self.constraints:
            _check_source(f"constraint {c.name!r}", c.source, allow_mtf=True)
            if c.minimum is None and c.maximum is None:
                raise ProblemError(f"constraint {c.name!r}: give minimum and/or maximum")
            if not c.scale > 0:
                raise ProblemError(f"constraint {c.name!r}: scale must be positive")
        declared = {t.get("target_id") for t in self.project.get("targets") or []}
        for item in self.sourced():
            if "target" in item.source and item.source["target"] not in declared:
                raise ProblemError(f"{item.name!r}: target {item.source['target']!r} is not declared in the Project")
        if not (math.isfinite(self.invalid_residual) and self.invalid_residual > 0):
            raise ProblemError("invalid_residual must be a positive finite number")

    def sourced(self) -> list[Objective | Constraint]:
        return [*self.objectives, *self.constraints]

    @property
    def residual_names(self) -> list[str]:
        return [f"objective:{o.name}" for o in self.objectives] + [f"constraint:{c.name}" for c in self.constraints]

    def target_ids(self) -> list[str]:
        out: list[str] = []
        for item in self.sourced():
            tid = item.source.get("target")
            if tid is not None and tid not in out:
                out.append(tid)
        return out

    def mtf_observations(self) -> list[str]:
        out: list[str] = []
        for c in self.constraints:
            if "mtf" in c.source and c.source["mtf"]["observation"] not in out:
                out.append(c.source["mtf"]["observation"])
        return out

    def outputs(self) -> tuple[str, ...]:
        """What the session is asked for: ``requirements`` for target sources, ``plane_readings`` for MTF sources."""
        out = ["requirements"] if self.target_ids() else []
        if self.mtf_observations():
            out.append("plane_readings")
        return tuple(out)


@dataclass
class CandidateResult:
    x: dict[str, float]
    valid: bool
    residuals: list[float]
    residual_names: list[str]
    objective: float | None = None
    objectives: list[dict[str, Any]] = field(default_factory=list)
    constraints: list[dict[str, Any]] = field(default_factory=list)
    feasible: bool | None = None
    reason: dict[str, Any] | None = None
    evaluation: dict[str, Any] | None = None
    identity: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"x": dict(self.x), "valid": self.valid, "feasible": self.feasible, "objective": self.objective,
                "residuals": list(self.residuals), "residual_names": list(self.residual_names),
                "objectives": self.objectives, "constraints": self.constraints, "reason": self.reason,
                "evaluation": self.evaluation, "identity": self.identity}


class _Refused(Exception):
    def __init__(self, code: str, stage: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.reason = {"code": code, "stage": stage, "message": message, **details}


def aggregate(errors: Sequence[float], weights: Sequence[float], how: str) -> float:
    if not errors:
        return 0.0
    if how == "sum_squares":
        return float(sum(w * e * e for e, w in zip(errors, weights)))
    if how == "sum_abs":
        return float(sum(w * abs(e) for e, w in zip(errors, weights)))
    return float(max(w * abs(e) for e, w in zip(errors, weights)))


def margin_of(value: float, constraint: Constraint) -> float:
    """Scaled distance to the nearest bound: positive inside, negative outside."""
    sides = []
    if constraint.minimum is not None:
        sides.append((value - constraint.minimum) / constraint.scale)
    if constraint.maximum is not None:
        sides.append((constraint.maximum - value) / constraint.scale)
    return min(sides)


def _scope_of(ev: Any) -> dict[str, Any]:
    """The Engine's selective-evaluation scope (``ValidationResult.evaluation``) as plain JSON; a complete evaluation
    has none and reports ``complete: true``."""
    scope = ev.result.evaluation
    if scope is None:
        return {"complete": True, "requested": [], "executed": [], "not_computed": [], "targets_not_evaluated": []}
    doc = scope.model_dump(mode="json")
    return {"complete": False, "requested": doc["requested"], "executed": doc["executed"],
            "not_computed": doc["not_computed"], "targets_not_evaluated": doc["targets_not_evaluated"]}


class Adapter:
    """Evaluates candidates of one :class:`Problem` through one ``EvaluationSession`` (reused across candidates).

    ``session``: an ``os_optics.local.EvaluationSession`` (default: a new one). ``edit``: the function applying ops to a
    Project document (default: ``os_optics_kit.edit_project.apply_edits`` without the final check; the Engine
    evaluation checks the edited Project)."""

    def __init__(self, problem: Problem, session: Any = None,
                 edit: Callable[[dict[str, Any], list[dict[str, Any]]], dict[str, Any]] | None = None) -> None:
        self.problem = problem
        if session is None:
            from os_optics.local import EvaluationSession

            session = EvaluationSession()
        self.session = session
        self._edit = edit or _apply_ops

    @property
    def size(self) -> int:
        return len(self.problem.objectives) + len(self.problem.constraints)

    def nominal(self) -> dict[str, float]:
        return {v.name: v.nominal for v in self.problem.variables}

    def bounds(self) -> list[tuple[float, float]]:
        return [v.bounds for v in self.problem.variables]

    def scales(self) -> list[float]:
        return [v.scale for v in self.problem.variables]

    def vector(self, x: dict[str, float]) -> list[float]:
        return [float(x[v.name]) for v in self.problem.variables]

    def mapping(self, values: Sequence[float]) -> dict[str, float]:
        if len(values) != len(self.problem.variables):
            raise ProblemError(f"expected {len(self.problem.variables)} values, got {len(values)}")
        return {v.name: float(value) for v, value in zip(self.problem.variables, values)}

    def candidate(self, x: dict[str, float]) -> dict[str, Any]:
        """The Project of ``x`` (raises ``_Refused`` for an out-of-bounds value or a refused edit)."""
        ops: list[dict[str, Any]] = []
        for v in self.problem.variables:
            value = x.get(v.name, v.nominal)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise _Refused("OUT_OF_BOUNDS", "parameters", f"{v.name}: value must be a finite number",
                               variable=v.name)
            low, high = v.bounds
            if not low <= value <= high:
                raise _Refused("OUT_OF_BOUNDS", "parameters", f"{v.name}={value:g} lies outside [{low:g}, {high:g}]",
                               variable=v.name, value=float(value), bounds=[low, high])
            ops.extend(_substitute(copy.deepcopy(list(v.ops)), float(value), v.unit))
        if not ops:
            return copy.deepcopy(self.problem.project)
        try:
            return self._edit(self.problem.project, ops)
        except Exception as exc:  # an edit refused for this candidate is a structured reason
            raise _Refused("EDIT_REFUSED", "parameters", str(exc), error_code=getattr(exc, "code", type(exc).__name__),
                           details=_details(exc)) from None

    def evaluate(self, x: dict[str, float] | Sequence[float]) -> CandidateResult:
        """One candidate: never raises, never drops it (module docstring)."""
        problem = self.problem
        values = dict(x) if isinstance(x, dict) else self.mapping(list(x))
        values = {v.name: values.get(v.name, v.nominal) for v in problem.variables}
        result = CandidateResult(x=values, valid=False, residuals=[problem.invalid_residual] * self.size,
                                 residual_names=problem.residual_names)
        try:
            document = self.candidate(values)
            ev = self._run(document)
            result.evaluation = _scope_of(ev)
            result.identity = ev.identity
            readings = self._readings(ev)
            self._score(result, readings)
        except _Refused as refused:
            result.reason = refused.reason
            result.valid = False
            result.residuals = [problem.invalid_residual] * self.size
            result.objective = None
            result.feasible = None
        except Exception as exc:  # anything else the Engine raises is still a reason, never an escape
            result.reason = {"code": "ENGINE_ERROR", "stage": "evaluation", "message": str(exc),
                             "error_code": getattr(exc, "code", type(exc).__name__)}
            result.valid = False
            result.residuals = [problem.invalid_residual] * self.size
            result.objective = None
            result.feasible = None
        assert len(result.residuals) == self.size
        return result

    # ------------------------------------------------------------------------------------------------ internals
    def _run(self, document: dict[str, Any]) -> Any:
        targets = self.problem.target_ids()
        try:
            return self.session.evaluate(document, outputs=self.problem.outputs(), targets=targets or None)
        except Exception as exc:  # classified by the Engine's exception names (the session may be any object)
            names = {cls.__name__ for cls in type(exc).__mro__}
            if "LocalRunError" in names:
                raise _Refused("PROJECT_REFUSED", "evaluation", str(exc), details=_details(exc)) from None
            if "EvaluationPlanError" in names:
                raise _Refused("PLAN_REFUSED", "evaluation", str(exc), error_code=getattr(exc, "code", None)) from None
            if "RunStopped" in names:
                raise _Refused("RUN_STOPPED", "evaluation", str(exc)) from None
            raise

    def _readings(self, ev: Any) -> dict[str, dict[str, Any]]:
        """Every source's reading: ``value``, ``state``, ``value_basis`` and where it came from."""
        out: dict[str, dict[str, Any]] = {}
        by_target = {t.target_id: t for t in ev.result.targets}
        not_evaluated = {t["target_id"] for t in _scope_of(ev)["targets_not_evaluated"]}
        for tid in self.problem.target_ids():
            t = by_target.get(tid)
            if t is None:
                out[f"target:{tid}"] = {"value": None, "state": "NOT_COMPUTED", "value_basis": None,
                                        "not_evaluated": tid in not_evaluated,
                                        "message": "the target was not evaluated"}
                continue
            usable = t.value is not None and math.isfinite(t.value)
            out[f"target:{tid}"] = {
                "value": t.value, "unit": t.unit, "status": str(t.status.value), "metric": t.metric,
                "state": "KNOWN" if usable else "UNKNOWN",
                "value_basis": None if t.value_basis is None else _jsonable(t.value_basis),
                "issue_codes": list(t.issue_codes), "message": t.message}
        for observation in self.problem.mtf_observations():
            out.update(self._mtf(ev, observation))
        return out

    def _mtf(self, ev: Any, observation: str) -> dict[str, dict[str, Any]]:
        request = {**DEFAULT_IMAGING, **self.problem.imaging}
        wanted = sorted({float(c.source["mtf"]["frequency_cyc_per_mm"]) for c in self.problem.constraints
                         if "mtf" in c.source and c.source["mtf"]["observation"] == observation})
        freqs = sorted({0.0, *[float(f) for f in request.get("frequencies_cyc_per_mm") or []], *wanted})
        request["frequencies_cyc_per_mm"] = freqs
        try:
            analysis = ev.results().imaging(observation, request)
        except Exception as exc:
            raise _Refused("ANALYSIS_REFUSED", "imaging", str(exc), observation=observation,
                           error_code=getattr(exc, "code", type(exc).__name__)) from None
        out: dict[str, dict[str, Any]] = {}
        for c in self.problem.constraints:
            spec = c.source.get("mtf")
            if spec is None or spec["observation"] != observation:
                continue
            key = f"{int(spec.get('field', 0))}"
            qid = f"SRC#{key}.mtf_{spec.get('direction', 'tangential')}"
            q = next((q for q in analysis.quantities
                      if q.quantity_id.endswith(f"#{key}.mtf_{spec.get('direction', 'tangential')}")), None)
            if q is None:
                out[f"mtf:{c.name}"] = {"value": None, "state": "NOT_COMPUTED", "value_basis": None,
                                        "message": f"the imaging analysis states no {qid}"}
                continue
            prefix = q.quantity_id.rsplit(".", 1)[0]
            reading: dict[str, Any] = {"quantity_id": q.quantity_id, "state": str(q.state.value),
                                       "value_basis": None if q.value_basis is None else _jsonable(q.value_basis),
                                       "wavelength_nm": q.scope.wavelength_nm, "reason": q.reason, "message": q.message,
                                       "converged": None if q.convergence is None else q.convergence.converged,
                                       "value": None}
            curve = analysis.arrays.get(q.values_ref) if q.values_ref else None
            axis = analysis.arrays.get(f"{prefix}.frequencies_cyc_per_mm")
            if isinstance(curve, list) and isinstance(axis, list):
                target = float(spec["frequency_cyc_per_mm"])
                index = next((i for i, f in enumerate(axis) if abs(float(f) - target) <= 1e-9 * max(1.0, target)), None)
                if index is not None and curve[index] is not None:
                    reading["value"] = float(curve[index])
            out[f"mtf:{c.name}"] = reading
        return out

    def _reading(self, readings: dict[str, dict[str, Any]], name: str, source: dict[str, Any]) -> dict[str, Any]:
        key = f"target:{source['target']}" if "target" in source else f"mtf:{name}"
        reading = readings[key]
        if reading["state"] not in USABLE_STATES or reading.get("value") is None or not math.isfinite(reading["value"]):
            raise _Refused("QUANTITY_UNAVAILABLE", "results", f"{name}: {key} has no usable value "
                           f"(state {reading['state']})", quantity=key, reading=reading)
        return reading

    def _score(self, result: CandidateResult, readings: dict[str, dict[str, Any]]) -> None:
        problem = self.problem
        errors, weights, residuals = [], [], []
        for o in problem.objectives:
            reading = self._reading(readings, o.name, o.source)
            error = (reading["value"] - o.goal) / o.scale
            errors.append(error)
            weights.append(o.weight)
            residuals.append(math.sqrt(o.weight) * error)
            result.objectives.append({"name": o.name, "source": o.source, "value": reading["value"], "goal": o.goal,
                                      "scaled_error": error, "state": reading["state"],
                                      "value_basis": reading.get("value_basis")})
        feasible = True
        for c in problem.constraints:
            reading = self._reading(readings, c.name, c.source)
            margin = margin_of(reading["value"], c)
            feasible = feasible and margin >= 0
            residuals.append(max(0.0, -margin))
            result.constraints.append({"name": c.name, "source": c.source, "value": reading["value"],
                                       "minimum": c.minimum, "maximum": c.maximum, "margin": margin,
                                       "satisfied": margin >= 0, "state": reading["state"],
                                       "value_basis": reading.get("value_basis")})
        result.objective = aggregate(errors, weights, problem.aggregation)
        result.residuals = residuals
        result.feasible = feasible
        result.valid = True


def _apply_ops(document: dict[str, Any], ops: list[dict[str, Any]]) -> dict[str, Any]:
    from ..edit_project import apply_edits

    return apply_edits(document, ops, validate=False)["project"]


def _substitute(template: Any, value: float, unit: str | None) -> Any:
    from ..sweep_parameters import substitute

    return substitute(template, value, unit)


def _details(exc: Exception) -> Any:
    as_dict = getattr(exc, "as_dict", None)
    if callable(as_dict):
        try:
            return _jsonable(as_dict())
        except Exception:
            pass
    problems = getattr(exc, "problems", None)
    return _jsonable(problems) if problems is not None else None


def _jsonable(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


__all__ = ["AGGREGATIONS", "Adapter", "CandidateResult", "Constraint", "Objective", "Problem", "ProblemError",
           "REASON_CODES", "Variable", "aggregate", "margin_of"]
