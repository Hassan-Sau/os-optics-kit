"""Zoom study: positions of two moving groups that give each requested focal length with the focus held at a fixed
image plane, from the Engine's paraxial analysis.

Usage:
  os-optics-kit zoom SPEC.json --out DIR [--base PROJECT.json] [--overwrite]
                     [--study [--workers N|auto] [--resume] [--time-limit-s S] [--max-memory-mib M]]

SPEC.json (lengths in mm, design frame):
  {
    "schema": "os-optics-kit.zoom-spec", "schema_version": 1,
    "base_project": "project.json",              # relative to SPEC.json; --base overrides
    "wavelength_nm": 633,
    "groups": [                                  # every element of the path, in the order light meets them
      {"group_id": "front", "components": ["G1"], "envelope_mm": [0, 3]},
      {"group_id": "variator", "components": ["G2"], "moving": true, "travel_mm": [10, 70],
       "envelope_mm": [3, 3]},
      {"group_id": "compensator", "components": ["G3"], "moving": true, "travel_mm": [100, 150],
       "envelope_mm": [3, 3]}
    ],
    "image_plane": {"observation_id": "image"},   # or {"position_mm": [200, 0, 0]}
    "efl_mm": {"start": 35, "stop": 105, "step": 2.5},   # or {"values": [35, 50, 70]}
    "anchors": [                                 # known solutions: seeds of the continuation and branch checks
      {"efl_mm": 35, "configuration_id": "wide"},          # positions from a configuration of the base Project
      {"efl_mm": 70, "positions_mm": {"variator": 47.9331, "compensator": 124.2553}, "label": "mid"}
    ],
    "tolerances": {"efl_mm": 0.01, "focus_mm": 0.005},   # acceptance of each sample
    "clearance": {"min_mm": 1.0},                # least axial gap between envelopes, and last group to image
    "solver": {"tolerance_mm": 1e-7, "max_iterations": 30, "max_refinements": 8, "branch_jump_mm": 1.0,
               "anchor_tolerance_mm": 0.01, "derivative_step_mm": 1e-4}     # optional; these are the defaults
  }

Exactly two groups move: two conditions per sample (EFL equal to the request, paraxial image on the fixed plane) fix
two positions. A group's position is the design-frame coordinate along the optical axis (p . axis) of its first
component's pose origin; a moving group moves along the axis, all its components together. ``envelope_mm`` [before,
after] is the axial extent of a group's mount before its first entrance vertex and after its last exit vertex
(default [0, 0]); clearances are the gaps between consecutive envelopes and from the last envelope to the image plane.

Solver: Newton iteration on the two positions with the Engine's paraxial analysis
(``os_optics.local.json_api.paraxial_analysis``) as the model and a central-difference Jacobian. Each anchor is
solved from its declared positions. The continuation marches from the anchors over the sorted samples (below the
first anchor downward, between anchors upward from the lower one, above the last upward), each solve starting
from a secant predictor of the last two solutions. A solve that does not converge, or whose solution lies more
than ``branch_jump_mm`` from the predictor (another branch), is retried through a halved step, up to
``max_refinements`` halvings. Where the march reaches the next anchor's focal length its solution must lie within
``anchor_tolerance_mm`` of that anchor's (else BRANCH_MISMATCH). Travel and clearance are checked on the solved
positions, never imposed on them: a violation is reported with the unclipped position.

Every solved sample becomes a named configuration of DIR/project.json, the base Project with the moving groups'
poses per configuration (anchors keep their configuration id; base targets naming other configurations are
dropped and listed). The reported EFL, focus error and clearances are then read from the Engine's paraxial analysis
of DIR/project.json, one configuration at a time. A sample is PASS when |EFL error| <= tolerances.efl_mm,
|focus error| <= tolerances.focus_mm and travel and clearance hold; INFEASIBLE when a travel or clearance
constraint is violated; FAIL when a tolerance or the branch check is not met; UNSOLVED when no solution was found
(the reason code says why: NOT_CONVERGED, SINGULAR_JACOBIAN, BRANCH_JUMP, AFOCAL, ENGINE_REFUSED, NO_SEED).

Writes DIR/project.json (Engine-normalized), DIR/trajectory.json (every sample with requested and achieved values,
positions, focus error, clearances, status and reasons; coverage and resolution; basis and Engine identity),
DIR/trajectory.csv and DIR/report.md. --study also runs every configuration as a sweep study in DIR/study
(``os-optics-kit sweep`` machinery, parameter efl_mm; an unsolved sample is a refused sample) for compare, plot,
report and present. Stdout: one compact JSON document. Exit 0 every sample PASS, 1 a sample is not PASS,
4 refused spec or input, 130 cancelled study.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
from pathlib import Path
import shutil

from . import workflow_common as wc
from .study import options as opts

PROG = "os-optics-kit zoom"
SPEC_SCHEMA = "os-optics-kit.zoom-spec"
TRAJECTORY_SCHEMA = "os-optics-kit.zoom-trajectory"
SCHEMA_VERSION = 1
STATUSES = ("PASS", "FAIL", "INFEASIBLE", "UNSOLVED")
EXIT_CODES = {**wc.WORKFLOW_EXIT_CODES, **opts.EXIT_CODES, "PASS": 0, "FAIL": 1}
OUTPUT_FILES = ("project.json", "trajectory.json", "trajectory.csv", "report.md")
STUDY_DIR = "study"
SOLVER_DEFAULTS = {"tolerance_mm": 1e-7, "max_iterations": 30, "max_refinements": 8, "branch_jump_mm": 1.0,
                   "anchor_tolerance_mm": 0.01, "derivative_step_mm": 1e-4}
#: Most samples a spec may request.
MAX_SAMPLES = 2001
#: How far (mm) the image plane and the elements may lie off the optical axis.
AXIS_TOLERANCE_MM = 1e-6
BASIS = {
    "efl_mm": "Engine paraxial analysis (os_optics.local.json_api.paraxial_analysis) of DIR/project.json at the "
              "configuration: system efl_mm = -1/C of the reduced ABCD matrix",
    "focus_error_mm": "Engine paraxial analysis: rear focal point (design frame) along the optical axis minus the "
                      "image plane's position along the axis; positive behind the plane; the paraxial image of an "
                      "object at infinity",
    "positions_mm": "design-frame coordinate along the optical axis (p . axis) of each group's first component "
                    "pose origin",
    "clearance_mm": "axial gap between consecutive group envelopes (entrance / exit vertices from the Engine "
                    "paraxial analysis, extended by envelope_mm), and from the last envelope to the image plane",
}


class ModelFailure(Exception):
    """The model could not evaluate a set of positions (an Engine refusal, an afocal system)."""

    def __init__(self, code, message, problems=None):
        super().__init__(message)
        self.code = code
        self.problems = problems or []


# --------------------------------------------------------------------------------------------- spec

def _number(value, where, *, positive=False, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise wc.WorkflowError("SPEC_INVALID", f"{where} must be a finite number")
    if positive and value <= 0:
        raise wc.WorkflowError("SPEC_INVALID", f"{where} must be positive")
    if minimum is not None and value < minimum:
        raise wc.WorkflowError("SPEC_INVALID", f"{where} must be at least {minimum}")
    return float(value)


def _pair(value, where):
    if not isinstance(value, list) or len(value) != 2:
        raise wc.WorkflowError("SPEC_INVALID", f"{where} is a list of two numbers")
    return [_number(value[0], f"{where}[0]"), _number(value[1], f"{where}[1]")]


def sample_efls(spec_efl):
    """The requested focal lengths in ascending order: ``values`` or ``{start, stop, step}`` (inclusive)."""
    if not isinstance(spec_efl, dict):
        raise wc.WorkflowError("SPEC_INVALID", "efl_mm is {start, stop, step} or {values}")
    if "values" in spec_efl:
        values = spec_efl["values"]
        if not isinstance(values, list) or not values:
            raise wc.WorkflowError("SPEC_INVALID", "efl_mm.values must be a non-empty list")
        efls = [_number(v, "efl_mm.values[]") for v in values]
    else:
        try:
            start, stop, step = (_number(spec_efl[k], f"efl_mm.{k}") for k in ("start", "stop", "step"))
        except KeyError:
            raise wc.WorkflowError("SPEC_INVALID", "efl_mm needs start, stop and step (or values)") from None
        if step <= 0:
            raise wc.WorkflowError("SPEC_INVALID", "efl_mm.step must be positive")
        low, high = min(start, stop), max(start, stop)
        count = int(math.floor((high - low) / step + 1e-9)) + 1
        if count > MAX_SAMPLES:
            raise wc.WorkflowError("SPEC_INVALID", f"efl_mm gives {count} samples; at most {MAX_SAMPLES}")
        efls = [low + i * step for i in range(count)]
        if high - efls[-1] > 1e-9 * max(1.0, abs(high)):
            efls.append(high)
    if any(v == 0 for v in efls):
        raise wc.WorkflowError("SPEC_INVALID", "efl_mm values must be non-zero")
    if len({math.copysign(1.0, v) for v in efls}) > 1:
        raise wc.WorkflowError("SPEC_INVALID", "efl_mm values must all have the same sign")
    if len(efls) > MAX_SAMPLES:
        raise wc.WorkflowError("SPEC_INVALID", f"efl_mm gives {len(efls)} samples; at most {MAX_SAMPLES}")
    return sorted(set(round(v, 9) for v in efls))


def _component_index(base):
    return {c.get("instance_id"): c for c in base.get("components") or [] if isinstance(c, dict)}


def normalize_spec(spec, base):
    """Validated spec with defaults; raises WorkflowError (SPEC_INVALID, UNKNOWN_ID) before any Engine call."""
    if not isinstance(spec, dict):
        raise wc.WorkflowError("SPEC_INVALID", "the spec is a JSON object")
    if spec.get("schema", SPEC_SCHEMA) != SPEC_SCHEMA or spec.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise wc.WorkflowError("SPEC_INVALID", f"schema must be {SPEC_SCHEMA} version {SCHEMA_VERSION}")
    components = _component_index(base)
    wavelength = _number(spec.get("wavelength_nm"), "wavelength_nm", positive=True)
    groups_in = spec.get("groups")
    if not isinstance(groups_in, list) or len(groups_in) < 2:
        raise wc.WorkflowError("SPEC_INVALID", "groups lists every group of the path, in light order")
    groups, seen_groups, seen_components = [], set(), set()
    for index, group in enumerate(groups_in):
        where = f"groups[{index}]"
        if not isinstance(group, dict):
            raise wc.WorkflowError("SPEC_INVALID", f"{where} is an object")
        gid = group.get("group_id")
        if not isinstance(gid, str) or not gid or gid in seen_groups:
            raise wc.WorkflowError("SPEC_INVALID", f"{where}: group_id must be a unique non-empty string")
        seen_groups.add(gid)
        members = group.get("components")
        if not isinstance(members, list) or not members or not all(isinstance(m, str) for m in members):
            raise wc.WorkflowError("SPEC_INVALID", f"group {gid!r}: components is a non-empty list of instance ids")
        for member in members:
            if member not in components:
                raise wc.WorkflowError("UNKNOWN_ID", f"group {gid!r}: component {member!r} is not in the Project",
                                       known=sorted(k for k in components if k))
            if components[member].get("optical_role") == "source":
                raise wc.WorkflowError("SPEC_INVALID", f"group {gid!r}: {member!r} is a source")
            if member in seen_components:
                raise wc.WorkflowError("SPEC_INVALID", f"component {member!r} is in more than one group")
            seen_components.add(member)
        moving = group.get("moving", False)
        if not isinstance(moving, bool):
            raise wc.WorkflowError("SPEC_INVALID", f"group {gid!r}: moving is true or false")
        travel = None
        if moving:
            if "travel_mm" not in group:
                raise wc.WorkflowError("SPEC_INVALID", f"group {gid!r}: a moving group declares travel_mm [min, max]")
            travel = sorted(_pair(group["travel_mm"], f"group {gid!r} travel_mm"))
        elif "travel_mm" in group:
            raise wc.WorkflowError("SPEC_INVALID", f"group {gid!r}: travel_mm belongs to a moving group")
        envelope = _pair(group.get("envelope_mm", [0, 0]), f"group {gid!r} envelope_mm")
        if min(envelope) < 0:
            raise wc.WorkflowError("SPEC_INVALID", f"group {gid!r}: envelope_mm values are not negative")
        groups.append({"group_id": gid, "components": list(members), "moving": moving, "travel_mm": travel,
                       "envelope_mm": envelope})
    moving_groups = [g["group_id"] for g in groups if g["moving"]]
    if len(moving_groups) != 2:
        raise wc.WorkflowError("SPEC_INVALID", f"exactly two groups move (EFL and focus fix two positions); "
                               f"{len(moving_groups)} declared", moving=moving_groups)
    image = spec.get("image_plane")
    if not isinstance(image, dict) or len(set(image) & {"observation_id", "position_mm"}) != 1:
        raise wc.WorkflowError("SPEC_INVALID", "image_plane is {observation_id} or {position_mm}")
    if "observation_id" in image:
        oid = image["observation_id"]
        plane = next((o for o in base.get("observations") or [] if o.get("observation_id") == oid), None)
        if plane is None:
            raise wc.WorkflowError("UNKNOWN_ID", f"image_plane: observation {oid!r} is not in the Project")
        if plane.get("device_instance_id"):
            raise wc.WorkflowError("SPEC_INVALID", f"image_plane: observation {oid!r} is attached to a component; "
                                   "the image plane is fixed in the design frame")
        image_point = [float(v) for v in plane["pose"]["position_mm"]]
        image_norm = {"observation_id": oid, "position_mm": image_point}
    else:
        point = image["position_mm"]
        if not isinstance(point, list) or len(point) != 3:
            raise wc.WorkflowError("SPEC_INVALID", "image_plane.position_mm is [x, y, z]")
        image_point = [_number(v, "image_plane.position_mm[]") for v in point]
        image_norm = {"observation_id": None, "position_mm": image_point}
    efls = sample_efls(spec.get("efl_mm"))
    tolerances = spec.get("tolerances") or {}
    tol = {"efl_mm": _number(tolerances.get("efl_mm"), "tolerances.efl_mm", positive=True),
           "focus_mm": _number(tolerances.get("focus_mm"), "tolerances.focus_mm", positive=True)}
    clearance = spec.get("clearance") or {}
    min_clearance = _number(clearance.get("min_mm", 0.0), "clearance.min_mm", minimum=0.0)
    solver_in = spec.get("solver") or {}
    unknown = sorted(set(solver_in) - set(SOLVER_DEFAULTS))
    if unknown:
        raise wc.WorkflowError("SPEC_INVALID", f"solver: unknown keys {unknown}; known: {sorted(SOLVER_DEFAULTS)}")
    solver = dict(SOLVER_DEFAULTS)
    for key, value in solver_in.items():
        solver[key] = _number(value, f"solver.{key}", positive=key != "max_refinements", minimum=0)
    solver["max_iterations"] = int(solver["max_iterations"])
    solver["max_refinements"] = int(solver["max_refinements"])
    if solver["tolerance_mm"] >= min(tol.values()):
        raise wc.WorkflowError("SPEC_INVALID", "solver.tolerance_mm must be smaller than the acceptance tolerances")
    anchors_in = spec.get("anchors")
    if not isinstance(anchors_in, list) or not anchors_in:
        raise wc.WorkflowError("SPEC_INVALID", "anchors lists at least one known solution (efl_mm with "
                               "configuration_id or positions_mm)")
    configurations = {c.get("configuration_id"): c for c in base.get("configurations") or [] if isinstance(c, dict)}
    anchors = []
    for index, anchor in enumerate(anchors_in):
        where = f"anchors[{index}]"
        if not isinstance(anchor, dict):
            raise wc.WorkflowError("SPEC_INVALID", f"{where} is an object")
        efl = _number(anchor.get("efl_mm"), f"{where}.efl_mm")
        cid = anchor.get("configuration_id")
        if "positions_mm" in anchor:
            given = anchor["positions_mm"]
            if not isinstance(given, dict) or set(given) != set(moving_groups):
                raise wc.WorkflowError("SPEC_INVALID", f"{where}.positions_mm names each moving group: "
                                       f"{moving_groups}")
            positions = [_number(given[g], f"{where}.positions_mm.{g}") for g in moving_groups]
            source = "positions_mm"
        elif cid is not None:
            if cid not in configurations:
                raise wc.WorkflowError("UNKNOWN_ID", f"{where}: configuration {cid!r} is not in the base Project",
                                       known=sorted(k for k in configurations if k))
            positions = None  # read along the axis once the axis is known
            source = f"configuration {cid}"
        else:
            raise wc.WorkflowError("SPEC_INVALID", f"{where}: give configuration_id or positions_mm")
        label = anchor.get("label", cid)
        anchors.append({"efl_mm": efl, "configuration_id": cid, "label": label, "declared_mm": positions,
                        "source": source})
    if len({round(a["efl_mm"], 9) for a in anchors}) != len(anchors):
        raise wc.WorkflowError("SPEC_INVALID", "two anchors have the same efl_mm")
    anchor_ids = [a["configuration_id"] for a in anchors if a["configuration_id"]]
    if len({i.lower() for i in anchor_ids}) != len(anchor_ids):
        raise wc.WorkflowError("SPEC_INVALID", "two anchors have the same configuration_id")
    anchors.sort(key=lambda a: a["efl_mm"])
    return {"wavelength_nm": wavelength, "groups": groups, "moving_groups": moving_groups, "image_plane": image_norm,
            "efl_mm": efls, "anchors": anchors, "tolerances": tol, "clearance": {"min_mm": min_clearance},
            "solver": solver}


# --------------------------------------------------------------------------------------------- vectors

def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _sub(a, b):
    return [x - y for x, y in zip(a, b)]


def _off_axis(point, origin, axis):
    d = _sub(point, origin)
    along = _dot(d, axis)
    return math.sqrt(max(0.0, _dot(d, d) - along * along))


# --------------------------------------------------------------------------------------------- Engine model

def solver_base(base):
    """The base Project without configurations and without the targets that name configurations."""
    document = copy.deepcopy(base)
    document.pop("configurations", None)
    document["targets"] = [t for t in document.get("targets") or [] if not t.get("configuration_ids")]
    return document


class EngineModel:
    """The Engine's paraxial analysis of the base Project with the two moving groups placed along the axis."""

    def __init__(self, base, normalized):
        self.base = solver_base(base)
        self.normalized = normalized
        self.groups = normalized["groups"]
        self.moving = [g for g in self.groups if g["moving"]]
        self.components = [c for g in self.groups for c in g["components"]]
        self.index = _component_index(self.base)
        self.evaluations = 0
        reply = self.analyse(self.base)
        if len(reply.get("axis_path") or []) != 1:
            raise wc.WorkflowError("AXIS_FOLDED", "the path is folded (mirrors): a zoom study needs one straight axis")
        self.axis = [float(v) for v in reply["optical_axis"]]
        self.origin = [float(v) for v in reply["elements"][0]["entrance_vertex_mm"]]
        image = normalized["image_plane"]["position_mm"]
        offset = _off_axis(image, self.origin, self.axis)
        if offset > AXIS_TOLERANCE_MM:
            raise wc.WorkflowError("IMAGE_PLANE_OFF_AXIS", f"the image plane origin lies {offset:.6g} mm off the "
                                   "optical axis", offset_mm=offset)
        self.image_s = _dot(image, self.axis)
        self.nominal = {g["group_id"]: self.group_position(g, self.base) for g in self.moving}

    def analyse(self, document, configuration_id=None):
        from os_optics.local import json_api

        request = {"components": self.components, "wavelength_nm": self.normalized["wavelength_nm"]}
        if configuration_id is not None:
            request["configuration_id"] = configuration_id
        self.evaluations += 1
        reply = json_api.paraxial_analysis(document, request)
        if not reply.get("ok"):
            error = reply.get("error") or {}
            raise ModelFailure("ENGINE_REFUSED", error.get("message") or "the Engine refused the paraxial analysis",
                               error.get("problems") or [error])
        return reply

    def group_position(self, group, document):
        component = _component_index(document)[group["components"][0]]
        return _dot([float(v) for v in component["nominal_pose"]["position_mm"]], self.axis)

    def poses(self, positions):
        """``{instance_id: nominal_pose}`` of the moving components at ``positions`` (moving-group order)."""
        out = {}
        for group, s in zip(self.moving, positions):
            shift = s - self.nominal[group["group_id"]]
            for member in group["components"]:
                pose = copy.deepcopy(self.index[member]["nominal_pose"])
                pose["position_mm"] = [float(p) + shift * a for p, a in zip(pose["position_mm"], self.axis)]
                out[member] = pose
        return out

    def document(self, positions):
        document = copy.deepcopy(self.base)
        poses = self.poses(positions)
        for component in document["components"]:
            if component.get("instance_id") in poses:
                component["nominal_pose"] = poses[component["instance_id"]]
        return document

    def system(self, reply):
        """``(efl_mm, focus position along the axis)`` of a paraxial reply."""
        system = reply["system"]
        if system.get("afocal") or system.get("efl_mm") is None or system.get("rear_focal_point_mm") is None:
            raise ModelFailure("AFOCAL", "the system is afocal: no focal length and no image of an object at "
                               "infinity")
        return float(system["efl_mm"]), _dot([float(v) for v in system["rear_focal_point_mm"]], self.axis)

    def __call__(self, positions):
        return self.system(self.analyse(self.document(positions)))

    def anchor_positions(self, base, configuration_id):
        """Moving-group positions along the axis in a configuration of the base Project."""
        configuration = next(c for c in base.get("configurations") or [] if c.get("configuration_id") ==
                             configuration_id)
        poses = {p["instance_id"]: p["nominal_pose"] for p in configuration.get("component_poses") or []}
        out = []
        for group in self.moving:
            member = group["components"][0]
            pose = poses.get(member, self.index[member]["nominal_pose"])
            out.append(_dot([float(v) for v in pose["position_mm"]], self.axis))
        return out


# --------------------------------------------------------------------------------------------- solver

def _residual(model, x, efl, image_s):
    value, focus = model(x)
    return [value - efl, focus - image_s]


def _norm(r):
    return math.hypot(r[0], r[1])


def _jacobian(model, x, efl, image_s, h):
    """Central-difference Jacobian [[dEFL/dx0, dEFL/dx1], [dfocus/dx0, dfocus/dx1]]."""
    columns = []
    for k in range(2):
        plus, minus = list(x), list(x)
        plus[k] += h
        minus[k] -= h
        rp, rm = _residual(model, plus, efl, image_s), _residual(model, minus, efl, image_s)
        columns.append([(rp[i] - rm[i]) / (2 * h) for i in range(2)])
    return [[columns[0][0], columns[1][0]], [columns[0][1], columns[1][1]]]


def newton(model, efl, image_s, guess, solver):
    """Positions where the model's EFL equals ``efl`` and its focus lies at ``image_s``.

    Returns ``{"ok", "positions", "iterations", "residual", "code", "message"}``; never raises for a model failure."""
    x = [float(v) for v in guess]
    tol, h = solver["tolerance_mm"], solver["derivative_step_mm"]
    iterations = 0
    try:
        r = _residual(model, x, efl, image_s)
    except ModelFailure as exc:
        return {"ok": False, "positions": x, "iterations": 0, "residual": None, "code": exc.code, "message": str(exc)}
    jacobian = None
    while True:
        if abs(r[0]) <= tol and abs(r[1]) <= tol:
            return {"ok": True, "positions": x, "iterations": iterations, "residual": r, "code": None,
                    "message": None, "jacobian": jacobian}
        if iterations >= solver["max_iterations"]:
            return {"ok": False, "positions": x, "iterations": iterations, "residual": r, "code": "NOT_CONVERGED",
                    "message": f"no convergence in {iterations} iterations"}
        iterations += 1
        try:
            jacobian = _jacobian(model, x, efl, image_s, h)
        except ModelFailure as exc:
            return {"ok": False, "positions": x, "iterations": iterations, "residual": r, "code": exc.code,
                    "message": str(exc)}
        (j00, j01), (j10, j11) = jacobian
        det = j00 * j11 - j01 * j10
        scale = j00 * j00 + j01 * j01 + j10 * j10 + j11 * j11
        if not math.isfinite(det) or abs(det) <= 1e-12 * scale or scale == 0:
            return {"ok": False, "positions": x, "iterations": iterations, "residual": r,
                    "code": "SINGULAR_JACOBIAN", "message": "the EFL and focus conditions do not fix both positions "
                                                            "here (singular Jacobian)"}
        step = [-(j11 * r[0] - j01 * r[1]) / det, -(-j10 * r[0] + j00 * r[1]) / det]
        t, accepted = 1.0, False
        for _ in range(12):
            trial = [x[0] + t * step[0], x[1] + t * step[1]]
            try:
                rt = _residual(model, trial, efl, image_s)
            except ModelFailure:
                t *= 0.5
                continue
            if _norm(rt) < _norm(r) or (abs(rt[0]) <= tol and abs(rt[1]) <= tol):
                x, r, accepted = trial, rt, True
                break
            t *= 0.5
        if not accepted:
            return {"ok": False, "positions": x, "iterations": iterations, "residual": r, "code": "NOT_CONVERGED",
                    "message": "the damped Newton step does not reduce the residual"}


def tangent(jacobian):
    """d(positions)/d(EFL) along the solution curve (focus held): J^-1 [1, 0], or None without a usable J."""
    if not jacobian:
        return None
    (j00, j01), (j10, j11) = jacobian
    det = j00 * j11 - j01 * j10
    if not math.isfinite(det) or det == 0:
        return None
    return [j11 / det, -j10 / det]


def _predict(history, efl, slope=None):
    """Secant predictor from the last two solutions; from a single solution its tangent ``slope``, else the
    solution itself."""
    if len(history) < 2:
        e1, x1 = history[-1]
        if slope is None:
            return list(x1)
        return [x + d * (efl - e1) for x, d in zip(x1, slope)]
    (e0, x0), (e1, x1) = history[-2], history[-1]
    if e1 == e0:
        return list(x1)
    t = (efl - e1) / (e1 - e0)
    return [b + (b - a) * t for a, b in zip(x0, x1)]


def _advance(model, history, efl, image_s, solver, depth=0, slope=None):
    """Solve at ``efl`` continuing ``history`` [(efl, positions)], halving the step on failure; ``slope``: the
    tangent at a single starting solution.

    Returns ``(result, substeps)``; on success ``history`` ends with the intermediate steps it took."""
    predicted = _predict(history, efl, slope)
    result = newton(model, efl, image_s, predicted, solver)
    if result["ok"]:
        jump = max(abs(a - b) for a, b in zip(result["positions"], predicted))
        result["predictor_mm"], result["correction_mm"] = predicted, jump
        if jump <= solver["branch_jump_mm"]:
            return result, 0
        result = {**result, "ok": False, "code": "BRANCH_JUMP",
                  "message": f"the solution lies {jump:.6g} mm from the predictor (another branch)"}
    if depth >= solver["max_refinements"]:
        return result, 0
    middle = 0.5 * (history[-1][0] + efl)
    half, used = _advance(model, history, middle, image_s, solver, depth + 1, slope)
    if not half["ok"]:
        return result if depth == 0 else half, used
    history.append((middle, half["positions"]))
    rest, more = _advance(model, history, efl, image_s, solver, depth + 1)
    return rest, used + more + 1


def _march(model, start, targets, image_s, solver, results, seed, slope=None):
    history = [start]
    for efl in targets:
        local = list(history)
        result, substeps = _advance(model, local, efl, image_s, solver, slope=slope)
        result["substeps"], result["seed"] = substeps, seed
        if result["ok"]:
            history = local + [(efl, result["positions"])]
        results[efl] = result


def continuation(model, efls, anchors, image_s, solver):
    """Solve every requested focal length (module docstring); returns ``{efl: result}`` and the anchor records.

    ``anchors``: ``[{"efl_mm", "declared_mm", ...}]`` sorted by EFL; ``model(positions) -> (efl, focus)``."""
    anchor_results = []
    for anchor in anchors:
        solved = newton(model, anchor["efl_mm"], image_s, anchor["declared_mm"], solver)
        solved["seed"] = f"anchor {anchor['label'] or anchor['efl_mm']}"
        if solved["ok"] and solved.get("jacobian") is None:  # the declared positions solve it: slope from here
            try:
                solved["jacobian"] = _jacobian(model, solved["positions"], anchor["efl_mm"], image_s,
                                               solver["derivative_step_mm"])
            except ModelFailure:
                pass
        solved["substeps"] = 0
        anchor_results.append(solved)
    results = {}
    efls = sorted(set(efls) | {a["efl_mm"] for a in anchors})
    solved = [(a, r) for a, r in zip(anchors, anchor_results) if r["ok"]]
    if not solved:
        for efl in efls:
            results[efl] = {"ok": False, "positions": None, "code": "NO_SEED", "seed": None, "substeps": 0,
                            "iterations": 0, "residual": None,
                            "message": "no anchor converged: the continuation has no starting point"}
        for anchor, result in zip(anchors, anchor_results):
            results[anchor["efl_mm"]] = result
        return results, anchor_results
    first, last = solved[0], solved[-1]
    below = [e for e in efls if e < first[0]["efl_mm"]]
    _march(model, (first[0]["efl_mm"], first[1]["positions"]), sorted(below, reverse=True), image_s, solver, results,
           f"continuation from {first[1]['seed']}", tangent(first[1].get("jacobian")))
    for (a, ra), (b, rb) in zip(solved, solved[1:]):
        inside = [e for e in efls if a["efl_mm"] < e <= b["efl_mm"]]
        _march(model, (a["efl_mm"], ra["positions"]), inside, image_s, solver, results,
               f"continuation from {ra['seed']}", tangent(ra.get("jacobian")))
        arrival = results.get(b["efl_mm"])
        check = {"from": ra["seed"], "arrival_ok": bool(arrival and arrival["ok"]), "deviation_mm": None,
                 "ok": False}
        if arrival and arrival["ok"]:
            check["deviation_mm"] = max(abs(p - q) for p, q in zip(arrival["positions"], rb["positions"]))
            check["ok"] = check["deviation_mm"] <= solver["anchor_tolerance_mm"]
        rb["branch_check"] = check
    above = [e for e in efls if e > last[0]["efl_mm"]]
    _march(model, (last[0]["efl_mm"], last[1]["positions"]), above, image_s, solver, results,
           f"continuation from {last[1]['seed']}", tangent(last[1].get("jacobian")))
    for anchor, result in zip(anchors, anchor_results):  # a converged anchor's own solve stands for its sample
        if result["ok"] or not results.get(anchor["efl_mm"], {}).get("ok"):
            results[anchor["efl_mm"]] = result
    return results, anchor_results


# --------------------------------------------------------------------------------------------- configurations

def configuration_id(efl, used):
    """``efl_042p50`` (two decimals), unique ignoring case within ``used``."""
    text = f"efl_{'m' if efl < 0 else ''}{abs(efl):06.2f}".replace(".", "p")
    candidate, n = text, 2
    while candidate.lower() in used:
        candidate, n = f"{text}_{n}", n + 1
    used.add(candidate.lower())
    return candidate


def generated_project(base, model, samples):
    """The base Project with one configuration per solved sample; returns (document, dropped target ids)."""
    document = copy.deepcopy(base)
    document.pop("configurations", None)
    ids = {s["configuration_id"] for s in samples if s["configuration_id"]}
    kept, dropped = [], []
    for target in document.get("targets") or []:
        names = target.get("configuration_ids")
        if names and not set(names) <= ids:
            dropped.append(target.get("target_id"))
        else:
            kept.append(target)
    document["targets"] = kept
    configurations = []
    for sample in samples:
        if sample["configuration_id"] is None:
            continue
        poses = model.poses(sample["solved_positions_mm"])
        label = f"EFL {sample['efl_requested_mm']:g} mm" + (f" ({sample['label']})" if sample["label"] else "")
        configurations.append({"configuration_id": sample["configuration_id"], "label": label[:200],
                               "component_poses": [{"instance_id": k, "nominal_pose": v}
                                                   for k, v in sorted(poses.items())]})
    if configurations:
        document["configurations"] = configurations
    return document, dropped


# --------------------------------------------------------------------------------------------- evaluation

def _clearances(reply, model, groups, image_s):
    """Axial gaps between consecutive group envelopes and from the last envelope to the image plane."""
    by_id = {}
    for element in reply["elements"]:
        by_id.setdefault(element["instance_id"], []).append(element)
    spans = []
    for group in groups:
        fronts, backs = [], []
        for member in group["components"]:
            for element in by_id.get(member, []):
                fronts.append(_dot(element["entrance_vertex_mm"], model.axis))
                backs.append(_dot(element["exit_vertex_mm"], model.axis))
        before, after = group["envelope_mm"]
        spans.append((group["group_id"], min(fronts) - before, max(backs) + after))
    gaps = []
    for (ga, _fa, ba), (gb, fb, _bb) in zip(spans, spans[1:]):
        gaps.append({"between": [ga, gb], "clearance_mm": fb - ba})
    gaps.append({"between": [spans[-1][0], "image_plane"], "clearance_mm": image_s - spans[-1][2]})
    return gaps


def constraint_reasons(normalized, positions, gaps, efl_error, focus_error):
    """The reasons a solved sample is not PASS: travel, clearance and tolerance checks, in that order."""
    tol = normalized["tolerances"]
    minimum = normalized["clearance"]["min_mm"]
    reasons = []
    for group in normalized["groups"]:
        if not group["moving"]:
            continue
        low, high = group["travel_mm"]
        s = positions[group["group_id"]]
        if s < low or s > high:
            reasons.append({"code": "TRAVEL_EXCEEDED", "group_id": group["group_id"], "position_mm": s,
                            "travel_mm": [low, high], "excess_mm": (low - s) if s < low else (s - high)})
    for gap in gaps:
        if gap["clearance_mm"] < minimum:
            reasons.append({"code": "CLEARANCE_VIOLATED", "between": gap["between"],
                            "clearance_mm": gap["clearance_mm"], "min_mm": minimum,
                            "shortfall_mm": minimum - gap["clearance_mm"]})
    if abs(efl_error) > tol["efl_mm"]:
        reasons.append({"code": "EFL_OUT_OF_TOLERANCE", "efl_error_mm": efl_error, "tolerance_mm": tol["efl_mm"]})
    if abs(focus_error) > tol["focus_mm"]:
        reasons.append({"code": "FOCUS_OUT_OF_TOLERANCE", "focus_error_mm": focus_error,
                        "tolerance_mm": tol["focus_mm"]})
    return reasons


def sample_status(solved, reasons):
    """UNSOLVED without a solution; INFEASIBLE on a travel or clearance violation; FAIL on any other reason."""
    codes = {r["code"] for r in reasons}
    if not solved:
        return "UNSOLVED"
    if codes & {"TRAVEL_EXCEEDED", "CLEARANCE_VIOLATED"}:
        return "INFEASIBLE"
    return "FAIL" if codes else "PASS"


def evaluate_samples(document, model, normalized, samples):
    """Fill each solved sample's achieved values from the Engine's paraxial analysis of ``document`` per
    configuration, and its status and reasons."""
    for sample in samples:
        reasons = list(sample["reasons"])
        if sample["configuration_id"] is not None:
            try:
                reply = model.analyse(document, sample["configuration_id"])
                efl, focus = model.system(reply)
            except ModelFailure as exc:
                reasons.append({"code": "EVALUATION_REFUSED", "message": str(exc), "problems": exc.problems})
                sample.update(status="FAIL", reasons=reasons)
                continue
            configured = _configured(document, sample["configuration_id"])
            positions = {g["group_id"]: model.group_position(g, configured) for g in model.moving}
            gaps = _clearances(reply, model, normalized["groups"], model.image_s)
            worst = min(gaps, key=lambda g: g["clearance_mm"])
            sample.update(efl_achieved_mm=efl, efl_error_mm=efl - sample["efl_requested_mm"],
                          focus_position_mm=focus, focus_error_mm=focus - model.image_s, positions_mm=positions,
                          clearances=gaps, min_clearance_mm=worst["clearance_mm"],
                          min_clearance_between=worst["between"])
            reasons += constraint_reasons(normalized, positions, gaps, sample["efl_error_mm"],
                                          sample["focus_error_mm"])
        sample.update(status=sample_status(sample["configuration_id"] is not None, reasons), reasons=reasons)
    return samples


def _configured(document, configuration_id):
    from os_optics.local.configurations import configured_project

    return configured_project(document, configuration_id).payload()


def coverage(samples, efls):
    """Sampling resolution and the share of the requested range between adjacent PASS samples."""
    ordered = sorted(samples, key=lambda s: s["efl_requested_mm"])
    low, high = min(efls), max(efls)
    span = high - low
    steps = [b["efl_requested_mm"] - a["efl_requested_mm"] for a, b in zip(ordered, ordered[1:])]
    covered, intervals, current = 0.0, [], None
    for a, b in zip(ordered, ordered[1:]):
        if a["status"] == "PASS" and b["status"] == "PASS":
            covered += b["efl_requested_mm"] - a["efl_requested_mm"]
            if current is not None and current[1] == a["efl_requested_mm"]:
                current[1] = b["efl_requested_mm"]
            else:
                current = [a["efl_requested_mm"], b["efl_requested_mm"]]
                intervals.append(current)
    singles = [s["efl_requested_mm"] for s in ordered if s["status"] == "PASS"
               and not any(i[0] <= s["efl_requested_mm"] <= i[1] for i in intervals)]
    intervals += [[v, v] for v in singles]
    intervals.sort()
    gaps: list[dict] = []
    previous = None
    for s in ordered:
        if s["status"] != "PASS":
            if gaps and gaps[-1]["last_mm"] == previous:
                gaps[-1]["last_mm"] = s["efl_requested_mm"]
                gaps[-1]["samples"] += 1
                gaps[-1]["statuses"] = sorted(set(gaps[-1]["statuses"]) | {s["status"]})
            else:
                gaps.append({"first_mm": s["efl_requested_mm"], "last_mm": s["efl_requested_mm"], "samples": 1,
                             "statuses": [s["status"]]})
        previous = s["efl_requested_mm"]
    return {"requested_range_mm": [low, high], "samples": len(ordered),
            "max_step_mm": max(steps) if steps else None, "min_step_mm": min(steps) if steps else None,
            "pass_intervals_mm": intervals, "not_pass_runs": gaps,
            "covered_fraction": (covered / span) if span > 0 else (1.0 if ordered and ordered[0]["status"] == "PASS"
                                                                   else 0.0),
            "basis": "share of the requested EFL range lying between two adjacent PASS samples"}


# --------------------------------------------------------------------------------------------- study

def sample_documents(document, samples):
    """One configured Project per sample in EFL order (a refusal for an unsolved sample), for the sweep machinery."""
    documents = []
    for sample in samples:
        if sample["configuration_id"] is None:
            documents.append({"refusal": {"stage": "zoom-solve", "code": sample["reasons"][0]["code"],
                                          "problems": sample["reasons"]}})
        else:
            documents.append(_configured(document, sample["configuration_id"]))
    return documents


def run_study(document, samples, study_dir, *, overwrite=False, workers=1, resume=False, limits=None, outputs=None):
    """Every sample's configuration as one sample of a sweep study (parameter efl_mm), run by the Engine."""
    from .sweep_parameters import run_sweep

    spec = {"parameters": [{"name": "efl_mm", "unit": "mm", "values": [s["efl_requested_mm"] for s in samples]}]}
    summary = run_sweep(spec, study_dir, documents=sample_documents(document, samples), base=None,
                        overwrite=overwrite, workers=workers, resume=resume, limits=limits, outputs=outputs)
    for sample, row in zip(samples, summary.get("rows") or []):
        sample["study_sample_id"] = row["sample_id"]
        sample["study_outcome"] = row["outcome"]
    return summary


# --------------------------------------------------------------------------------------------- outputs

CSV_COLUMNS = ["sample_id", "configuration_id", "status", "efl_requested_mm", "efl_achieved_mm", "efl_error_mm",
               "focus_position_mm", "image_plane_mm", "focus_error_mm", "min_clearance_mm", "min_clearance_between",
               "reasons", "seed", "substeps", "iterations", "study_sample_id", "study_outcome"]


def csv_rows(trajectory):
    rows = []
    moving = trajectory["moving_groups"]
    for sample in trajectory["samples"]:
        row = {key: sample.get(key) for key in CSV_COLUMNS}
        row["image_plane_mm"] = trajectory["image_plane"]["position_along_axis_mm"]
        row["reasons"] = [r["code"] for r in sample["reasons"]]
        solved = dict(zip(moving, sample.get("solved_positions_mm") or []))
        for group in moving:
            row[f"position_mm:{group}"] = (sample.get("positions_mm") or {}).get(group)
            row[f"solved_position_mm:{group}"] = solved.get(group)
        rows.append(row)
    columns = CSV_COLUMNS[:6] + [f"position_mm:{g}" for g in moving] + CSV_COLUMNS[6:] + \
        [f"solved_position_mm:{g}" for g in moving]
    return rows, columns


def _fmt(value, digits=4):
    if value is None:
        return "-"
    return f"{value:.{digits}f}"


def reason_label(reason):
    """A short text of one reason: its code and what it concerns."""
    code = reason["code"]
    if code == "TRAVEL_EXCEEDED":
        low, high = reason["travel_mm"]
        return f"{code} {reason['group_id']} {reason['position_mm']:.3f} mm outside [{low:g}, {high:g}]"
    if code == "CLEARANCE_VIOLATED":
        return (f"{code} {'/'.join(reason['between'])} {reason['clearance_mm']:.3f} mm < {reason['min_mm']:g}")
    if code in ("EFL_OUT_OF_TOLERANCE", "FOCUS_OUT_OF_TOLERANCE"):
        value = reason.get("efl_error_mm", reason.get("focus_error_mm"))
        return f"{code} {value:.3g} mm"
    return code


def _sci(value):
    return "-" if value is None else f"{value:.2e}"


def report_markdown(trajectory):
    moving = trajectory["moving_groups"]
    tol = trajectory["spec"]["tolerances"]
    counts = trajectory["counts"]
    cov = trajectory["coverage"]
    identity = trajectory["identity"]
    lines = [
        "# Zoom study",
        "",
        f"Requested EFL {cov['requested_range_mm'][0]:g} to {cov['requested_range_mm'][1]:g} mm in "
        f"{cov['samples']} samples (step {_fmt(cov['min_step_mm'], 3)} to {_fmt(cov['max_step_mm'], 3)} mm) at "
        f"{trajectory['spec']['wavelength_nm']:g} nm; image plane at {trajectory['image_plane']['position_along_axis_mm']:g}"
        f" mm along the axis. Tolerances: EFL ±{tol['efl_mm']:g} mm, focus ±{tol['focus_mm']:g} mm; least clearance "
        f"{trajectory['spec']['clearance']['min_mm']:g} mm.",
        "",
        "| Status | Samples |",
        "| --- | ---: |",
        *[f"| {status} | {counts[status]} of {cov['samples']} |" for status in STATUSES],
        "",
        f"Covered: {100 * cov['covered_fraction']:.1f} % of the requested range lies between two adjacent PASS "
        f"samples. PASS intervals (mm): "
        + (", ".join(f"{a:g}-{b:g}" for a, b in cov["pass_intervals_mm"]) or "none") + ".",
        "",
        "| Sample | Configuration | Status | EFL requested (mm) | EFL achieved (mm) | EFL error (mm) | "
        + " | ".join(f"{g} (mm)" for g in moving) + " | Focus error (mm) | Least clearance (mm) | Between | Reasons |",
        "| --- | --- | --- | ---: | ---: | ---: | " + " | ".join("---:" for _ in moving)
        + " | ---: | ---: | --- | --- |",
    ]
    for s in trajectory["samples"]:
        positions = s.get("positions_mm") or (dict(zip(moving, s["solved_positions_mm"]))
                                              if s.get("solved_positions_mm") else {})
        lines.append(
            f"| {s['sample_id']} | {s['configuration_id'] or '-'} | {s['status']} | {s['efl_requested_mm']:g} | "
            f"{_fmt(s.get('efl_achieved_mm'), 6)} | {_sci(s.get('efl_error_mm'))} | "
            + " | ".join(_fmt(positions.get(g)) for g in moving)
            + f" | {_sci(s.get('focus_error_mm'))} | {_fmt(s.get('min_clearance_mm'), 3)} | "
            f"{' / '.join(s.get('min_clearance_between') or []) or '-'} | "
            f"{'; '.join(reason_label(r) for r in s['reasons']) or '-'} |")
    lines += ["", "## Anchors", "",
              "| Anchor | EFL (mm) | Declared (mm) | Solved (mm) | Shift (mm) | Branch check |",
              "| --- | ---: | --- | --- | ---: | --- |"]
    for anchor in trajectory["anchors"]:
        check = anchor.get("branch_check")
        verdict = "-" if check is None else (
            f"{'OK' if check['ok'] else 'MISMATCH'} ({_sci(check['deviation_mm'])} mm)")
        lines.append(f"| {anchor['label'] or '-'} | {anchor['efl_mm']:g} | "
                     f"{', '.join(_fmt(v) for v in anchor['declared_mm'] or [])} | "
                     f"{', '.join(_fmt(v) for v in anchor['solved_mm'] or []) or '-'} | {_sci(anchor['shift_mm'])} | "
                     f"{verdict} |")
    lines += ["", "## Basis", ""]
    lines += [f"- `{key}`: {text}." for key, text in trajectory["basis"].items()]
    lines += ["", f"os-optics {identity['engine_version']} (build {identity['engine_build_id']}). Solver: "
              f"{trajectory['solver']['method']}.", ""]
    if trajectory.get("dropped_targets"):
        lines += [f"Targets of the base Project naming its own configurations, dropped: "
                  f"{', '.join(trajectory['dropped_targets'])}.", ""]
    return "\n".join(lines)


def _prepare_out(out, overwrite):
    out = Path(out)
    existing = [name for name in (*OUTPUT_FILES, STUDY_DIR) if (out / name).exists()]
    if existing and not overwrite:
        raise wc.WorkflowError("OUTPUT_EXISTS", f"{out.as_posix()} holds an earlier zoom study ({', '.join(existing)});"
                               " give --overwrite to replace it", existing=existing)
    if out.exists() and not out.is_dir():
        raise wc.WorkflowError("OUTPUT_EXISTS", f"{out.as_posix()} is a file")
    out.mkdir(parents=True, exist_ok=True)
    for name in OUTPUT_FILES:
        if (out / name).exists():
            (out / name).unlink()
    return out


# --------------------------------------------------------------------------------------------- workflow

def run_zoom(spec, out, *, base, base_path=None, overwrite=False, study=False, workers=1, resume=False,
             limits=None, outputs=None):
    """Solve, write and evaluate the zoom study described by ``spec`` (module docstring); returns the trajectory."""
    from ._engine import require_engine

    normalized = normalize_spec(spec, base)
    require_engine()
    model = EngineModel(base, normalized)
    out = _prepare_out(out, overwrite or (study and resume))
    if overwrite and not resume and (out / STUDY_DIR).exists():
        shutil.rmtree(out / STUDY_DIR)
    for anchor in normalized["anchors"]:
        if anchor["declared_mm"] is None:
            anchor["declared_mm"] = model.anchor_positions(base, anchor["configuration_id"])
    results, anchor_results = continuation(model, normalized["efl_mm"], normalized["anchors"], model.image_s,
                                           normalized["solver"])
    anchors_by_efl = {a["efl_mm"]: (a, r) for a, r in zip(normalized["anchors"], anchor_results)}
    used = {a["configuration_id"].lower() for a in normalized["anchors"] if a["configuration_id"]}
    samples = []
    for index, efl in enumerate(sorted(results), start=1):
        result = results[efl]
        anchor = anchors_by_efl.get(efl)
        reasons = []
        if not result["ok"]:
            reasons.append({"code": result["code"], "message": result["message"],
                            "last_positions_mm": result.get("positions"), "residual_mm": result.get("residual")})
        elif anchor and anchor[1].get("branch_check") and not anchor[1]["branch_check"]["ok"]:
            check = anchor[1]["branch_check"]
            reasons.append({"code": "BRANCH_MISMATCH", "message": "the continuation from the previous anchor does "
                            "not arrive at this anchor's solution", **check})
        cid = None
        if result["ok"]:
            cid = anchor[0]["configuration_id"] if anchor and anchor[0]["configuration_id"] else None
            cid = cid or configuration_id(efl, used)
        samples.append({
            "sample_id": f"z{index:03d}", "configuration_id": cid,
            "label": (anchor[0]["label"] if anchor else None), "anchor": anchor is not None,
            "efl_requested_mm": efl, "status": None, "reasons": reasons,
            "solved_positions_mm": result["positions"] if result["ok"] else None,
            "seed": result.get("seed"), "substeps": result.get("substeps", 0),
            "iterations": result.get("iterations"), "solver_residual_mm": result.get("residual"),
            "predictor_correction_mm": result.get("correction_mm"),
            "efl_achieved_mm": None, "efl_error_mm": None, "focus_position_mm": None, "focus_error_mm": None,
            "positions_mm": None, "clearances": None, "min_clearance_mm": None, "min_clearance_between": None,
            "study_sample_id": None, "study_outcome": None})
    document, dropped = generated_project(base, model, samples)
    checked = wc.check(document)
    if not checked.get("ok"):
        raise wc.WorkflowError("PROJECT_REFUSED", "the Engine refused the generated Project",
                               problems=(checked.get("error") or {}).get("problems"))
    document = checked["project"]
    evaluate_samples(document, model, normalized, samples)
    wc.write_text_atomic(out / "project.json", _normalized_text(document))
    counts = {status: sum(1 for s in samples if s["status"] == status) for status in STATUSES}
    anchor_records = []
    for anchor, result in zip(normalized["anchors"], anchor_results):
        solved = result["positions"] if result["ok"] else None
        anchor_records.append({
            "efl_mm": anchor["efl_mm"], "label": anchor["label"], "configuration_id": anchor["configuration_id"],
            "source": anchor["source"], "declared_mm": anchor["declared_mm"], "solved_mm": solved,
            "shift_mm": None if solved is None else max(abs(a - b) for a, b in zip(solved, anchor["declared_mm"])),
            "converged": result["ok"], "failure": None if result["ok"] else result["code"],
            "branch_check": result.get("branch_check")})
    identity = wc.engine_identity()
    trajectory = {
        "schema": TRAJECTORY_SCHEMA, "schema_version": SCHEMA_VERSION, "created_utc": wc.utc_now(),
        "status": "PASS" if counts["PASS"] == len(samples) else "FAIL",
        "spec": {key: normalized[key] for key in ("wavelength_nm", "groups", "tolerances", "clearance", "solver")},
        "moving_groups": normalized["moving_groups"],
        "image_plane": {**normalized["image_plane"], "position_along_axis_mm": model.image_s},
        "optical_axis": model.axis,
        "base_project": {"file": None if base_path is None else Path(base_path).as_posix(),
                         "document_sha256": wc.canonical_digest(base)},
        "project": {"file": "project.json", "project_hash": checked.get("project_hash"),
                    "engine_input_hash": checked.get("engine_input_hash"),
                    "configurations": sum(1 for s in samples if s["configuration_id"])},
        "dropped_targets": dropped,
        "counts": counts, "coverage": coverage(samples, normalized["efl_mm"]),
        "worst": _worst(samples),
        "anchors": anchor_records, "samples": samples, "basis": BASIS,
        "solver": {"method": "damped Newton on the two moving-group positions; model: Engine paraxial analysis; "
                             "central-difference Jacobian; continuation over EFL from the anchors with a secant "
                             "predictor, step halving and a branch-jump check",
                   "engine_evaluations": model.evaluations, **normalized["solver"]},
        "identity": identity, "study": None,
        "paths": {"out": out.as_posix(), "project": "project.json", "trajectory": "trajectory.json",
                  "table": "trajectory.csv", "report": "report.md"},
    }
    if study:
        summary = run_study(document, samples, out / STUDY_DIR, overwrite=overwrite, workers=workers,
                            resume=resume, limits=limits, outputs=outputs)
        trajectory["study"] = {"dir": STUDY_DIR, "status": summary["status"],
                               "counts": summary.get("counts"), "execution": opts.execution_brief(out / STUDY_DIR)}
        if summary["status"] == "CANCELLED":
            trajectory["status"] = "CANCELLED"
    wc.write_json(out / "trajectory.json", trajectory)
    rows, columns = csv_rows(trajectory)
    wc.write_csv(out / "trajectory.csv", rows, columns)
    wc.write_text_atomic(out / "report.md", report_markdown(trajectory))
    return trajectory


def _normalized_text(document):
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _worst(samples):
    solved = [s for s in samples if s["efl_error_mm"] is not None]
    if not solved:
        return None
    efl = max(solved, key=lambda s: abs(s["efl_error_mm"]))
    focus = max(solved, key=lambda s: abs(s["focus_error_mm"]))
    gap = min(solved, key=lambda s: s["min_clearance_mm"])
    return {"efl_error_mm": {"value": efl["efl_error_mm"], "sample_id": efl["sample_id"]},
            "focus_error_mm": {"value": focus["focus_error_mm"], "sample_id": focus["sample_id"]},
            "min_clearance_mm": {"value": gap["min_clearance_mm"], "sample_id": gap["sample_id"],
                                 "between": gap["min_clearance_between"]}}


def compact(trajectory, limit=6):
    """The bounded stdout document."""
    not_pass = [{"sample_id": s["sample_id"], "efl_requested_mm": s["efl_requested_mm"], "status": s["status"],
                 "reasons": [reason_label(r) for r in s["reasons"]]} for s in trajectory["samples"]
                if s["status"] != "PASS"]
    out = trajectory["paths"]["out"]
    return {"status": trajectory["status"], "counts": trajectory["counts"],
            "coverage": {k: trajectory["coverage"][k] for k in ("requested_range_mm", "samples", "max_step_mm",
                                                                 "covered_fraction", "pass_intervals_mm")},
            "worst": trajectory["worst"],
            "anchors": [{k: a[k] for k in ("label", "efl_mm", "shift_mm", "converged")} |
                        {"branch_ok": None if a["branch_check"] is None else a["branch_check"]["ok"]}
                        for a in trajectory["anchors"]],
            "not_pass": not_pass[:limit], "not_pass_omitted": max(0, len(not_pass) - limit),
            "configurations": trajectory["project"]["configurations"], "dropped_targets": trajectory["dropped_targets"],
            "engine": trajectory["identity"], "study": trajectory["study"], "paths": trajectory["paths"],
            "next": [f"os-optics-kit view {out}/project.json --configuration ID",
                     f"os-optics project run {out}/project.json --out-dir {out}/run"]}


def main(argv=None):
    def workflow():
        parser = wc.ArgumentParser(prog=PROG, description=__doc__,
                                   formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument("spec", type=Path, help="zoom spec JSON")
        parser.add_argument("--out", type=Path, required=True, help="output directory")
        parser.add_argument("--base", type=Path, help="base Project JSON (overrides the spec's base_project)")
        parser.add_argument("--overwrite", action="store_true", help="replace an earlier zoom study in --out")
        parser.add_argument("--study", action="store_true",
                            help="also run every configuration as a sweep study in OUT/study (Engine runs)")
        opts.add_execution_options(parser, plan_only=False)
        args = parser.parse_args(argv)
        spec = wc.read_json(args.spec)
        if not isinstance(spec, dict):
            raise wc.WorkflowError("SPEC_INVALID", "the spec is a JSON object")
        base_path = args.base or (args.spec.parent / spec["base_project"] if spec.get("base_project") else None)
        if base_path is None:
            raise wc.WorkflowError("SPEC_INVALID", "give base_project in the spec or --base")
        execution = opts.execution_arguments(args)
        trajectory = run_zoom(spec, args.out, base=wc.read_json(base_path), base_path=base_path,
                              overwrite=args.overwrite, study=args.study, workers=execution["workers"],
                              resume=execution["resume"], limits=execution["limits"], outputs=execution.get("outputs"))
        return compact(trajectory)

    return wc.emit(workflow, EXIT_CODES)


if __name__ == "__main__":
    raise SystemExit(main())
