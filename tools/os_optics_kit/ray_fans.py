"""Ray fans from a saved run: transverse and longitudinal aberration and OPD across the pupil, per field and wavelength.

os-optics-kit fans RUN --monitor PLANE [--path ID ...] [--zones N] [--imaging FILE] [--out fans.json]

Everything is computed from published engine outputs ('os-optics results rays', 'results monitor', 'results paths',
'results wavefront') and the run's project.json; nothing is traced here. Each path of the run is one field point at one
wavelength. For each path:

* pupil coordinates are the rays' launch offsets, in units of the source's 1/e^2 radii along its own x and y (the
  engine's labels: rho = 1 at the 1/e^2 radius, whatever is vignetted later; rho_max names the outermost ring that
  reached the plane);
* the pupil frame is carried to the plane: the source's x and y axes (its pose in project.json) are transported along
  the chief ray and projected onto the plane's (u, v); the orientation is then taken from the engine's first-order
  transfer of the arrival (the polar factor of its position or direction block), whose sign the transported axes fix.
  Paths with a reflection before the plane, or whose first-order map and source pose disagree, are refused
  (PUPIL_FRAME_UNRESOLVED), and so are paths whose chief ray was clipped and replaced by a pupil ray
  (REFERENCE_RAY_PROMOTED);
* the tangential direction is the unit vector in the plane's (u, v) from the plane origin towards the chief ray's image
  point (+v when the chief ray lands on the origin); the sagittal direction is the tangential one turned by -90 deg
  (+u when the tangential one is +v). Both are given per path (tangential_axis_uv, sagittal_axis_uv);
* fan points: each complete pupil ring (its rays equally spaced in azimuth) is fitted, per component, with a Fourier
  series in the pupil azimuth up to the highest harmonic its samples resolve (5 for 12 rays a ring), and evaluated on
  the four fan azimuths (+-tangential, +-sagittal). A ray lying on a fan azimuth is used as it is (sampled: true); an
  evaluated point states the ring's fit residual (fit_residual_um). A ring with missing rays gives only the rays that
  lie on a fan azimuth, and fan_rho_max states how far out each fan reaches. A ring is complete when all its rays
  arrived: the rays per ring come from the engine's pupil numbering (ray_index = 1 + ring_index * rays_per_ring +
  ring_position_index) and the rings launched from the arrival's pupil_ray_count. A path one of whose fans has no
  point (no complete ring, and no arrived ray on that fan's azimuths) is refused (FAN_AXIS_NOT_SAMPLED, with the fan
  azimuths and the arrived rays' pupil azimuths per ring);
* transverse aberration is the image-plane displacement from the chief ray along the fan direction, in micrometres;
* longitudinal aberration is the distance from the plane, along its normal counted in the direction the light travels,
  at which a fan ray crosses the chief ray in that direction: -(transverse) / (difference of their slopes du/dn or dv/dn), signed so that positive means the
  crossing lies behind the plane (further along the light), whichever way the plane's normal points; in millimetres.
  A ray parallel to the chief ray has none (null);
* OPD is the engine's own per-ray OPD ('results wavefront', reference plane through the chief ray, piston removed), in
  waves; when the engine refuses the wavefront, its status, reason and message are kept and OPD is null.

Every result also records: the RMS spot radius of all pupil rays about their centroid, beside the engine's
'geometric_spot_rms_radius' when an imaging document is given (--imaging FILE, from 'results imaging', matched by source,
wavelength and plane); the RMS of the per-ray OPD beside the engine's 'rms_waves' of the same path (a consistency check:
both come from the same per-ray data); the pupil rays launched and arrived, and the rings present and partial.
Paths that do not reach the plane with counted rays are listed in 'unreached' (with the engine's termination), paths
whose fans cannot be built in 'refused' (with the code, the reason and, where there are any, details); when no path
gives fans the result is the error NO_FANS. --zones N keeps, per fan, N of the rings that carry its points (its
outermost always). Output is JSON.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

# A sampled ray lies on a fan azimuth when its pupil azimuth is within this of it (rad).
AZIMUTH_TOLERANCE_RAD = 1e-9
# Highest harmonic of the ring fit; 12 rays a ring resolve up to 5.
MAX_HARMONIC = 5
# |direction_n| below this: the ray grazes the plane and has no slope du/dn.
GRAZING_DIRECTION_N = 1e-6
# A first-order block is used for the pupil orientation when its smaller singular value is at least this fraction of
# the larger (a nearly singular block has no orientation).
FIRST_ORDER_MIN_CONDITION = 0.2
# The first-order orientation and the transported source axes must agree within this (deg), modulo 180 deg.
FRAME_AGREEMENT_DEG = 45.0

REFLECTION_EVENTS = ("reflect", "reflection", "tir")


class FanError(RuntimeError):
    def __init__(self, code: str, message: str, engine_code: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.engine_code = engine_code
        self.details = details


# ----------------------------------------------------------------------------------------------------- engine calls


def _engine_command() -> list[str]:
    engine = shutil.which("os-optics")
    return [engine] if engine else [sys.executable, "-m", "os_optics"]


def _tail(text: str | None, limit: int = 500) -> str:
    return (text or "").strip()[-limit:]


def _engine_document(argv: list[str], runner=subprocess.run) -> tuple[int, Any]:
    """The engine's exit code and its JSON document (stdout, whole); an engine error document is a FanError."""
    done = runner(_engine_command() + argv, capture_output=True, text=True, encoding="utf-8")
    try:
        document = json.loads(done.stdout)
    except (TypeError, ValueError):
        raise FanError("ENGINE_RESULTS_FAILED",
                       f"os-optics {' '.join(argv[:2])} did not return one JSON document (exit {done.returncode}): "
                       + _tail(done.stderr or done.stdout)) from None
    if isinstance(document, dict) and (document.get("status") == "ERROR" or isinstance(document.get("error"), dict)):
        error: dict = document["error"] if isinstance(document.get("error"), dict) else {}
        code, message = error.get("code"), error.get("message") or document.get("message") or ""
        raise FanError("ENGINE_RESULTS_FAILED", f"os-optics {' '.join(argv[:2])}: {code}: {message}", engine_code=code)
    return done.returncode, document


def _engine_json(argv: list[str], runner=subprocess.run) -> Any:
    returncode, document = _engine_document(argv, runner)
    if returncode != 0:
        raise FanError("ENGINE_RESULTS_FAILED", f"os-optics {' '.join(argv[:2])} exited {returncode}: "
                       + _tail(json.dumps(document)))
    return document


def load_rays(run_dir: Path, monitor: str, runner=subprocess.run) -> list[dict]:
    rows = _engine_json(["results", "rays", str(run_dir), "--monitor", monitor], runner)
    rows = rows.get("rows") if isinstance(rows, dict) else rows
    if not isinstance(rows, list):
        raise FanError("ENGINE_RESULTS_FAILED", "os-optics results rays returned no list of rows")
    return rows


def load_monitor(run_dir: Path, monitor: str, runner=subprocess.run) -> dict:
    document = _engine_json(["results", "monitor", str(run_dir), monitor], runner)
    if not isinstance(document, dict) or not isinstance(document.get("plane"), dict):
        raise FanError("ENGINE_RESULTS_FAILED", f"os-optics results monitor returned no plane for {monitor}")
    return document


def load_paths(run_dir: Path, runner=subprocess.run) -> list[dict]:
    document = _engine_json(["results", "paths", str(run_dir)], runner)
    return document if isinstance(document, list) else []


def load_wavefront(run_dir: Path, monitor: str, path_id: str, runner=subprocess.run) -> dict:
    """The engine's wavefront of one path; a refusal (status UNAVAILABLE, e.g. behind an idealized element) is returned
    with the engine's status, reason and message, and no rays. An engine error (bad run, path or plane) is a FanError."""
    returncode, document = _engine_document(
        ["results", "wavefront", str(run_dir), "--monitor", monitor, "--path", path_id], runner)
    if not isinstance(document, dict):
        raise FanError("ENGINE_RESULTS_FAILED", "os-optics results wavefront returned no JSON object")
    if returncode == 0 and document.get("status") == "OK":
        return document
    if returncode in (0, 2) and document.get("status"):
        return {"status": document["status"], "reason": document.get("reason"), "message": document.get("message"),
                "rays": []}
    raise FanError("ENGINE_RESULTS_FAILED", f"os-optics results wavefront exited {returncode}: "
                   + _tail(json.dumps(document)))


# ------------------------------------------------------------------------------------------------------ pupil frame


def _rotate(q: list[float], v: tuple[float, float, float]) -> tuple[float, float, float]:
    w, x, y, z = q
    norm = math.sqrt(w * w + x * x + y * y + z * z)
    w, x, y, z = w / norm, x / norm, y / norm, z / norm
    vx, vy, vz = v
    tx, ty, tz = 2 * (y * vz - z * vy), 2 * (z * vx - x * vz), 2 * (x * vy - y * vx)
    return (vx + w * tx + (y * tz - z * ty), vy + w * ty + (z * tx - x * tz), vz + w * tz + (x * ty - y * tx))


def _dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


def _cross(a, b) -> tuple[float, float, float]:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _transport(vector, d_from, d_to):
    """Rotation-minimizing transport of ``vector`` from direction ``d_from`` to ``d_to`` (Rodrigues)."""
    axis = _cross(d_from, d_to)
    s, c = math.sqrt(_dot(axis, axis)), _dot(d_from, d_to)
    if s < 1e-15:
        return tuple(vector)
    k = tuple(a / s for a in axis)
    kv, kxv = _dot(k, vector), _cross(k, vector)
    return tuple(vector[i] * c + kxv[i] * s + k[i] * kv * (1 - c) for i in range(3))


def _orthogonal_factor(m: list[list[float]]) -> tuple[bool, float, float]:
    """The orthogonal polar factor of a 2x2 matrix: (proper rotation?, its angle in rad, conditioning s_min/s_max).

    A rotation [[c, -s], [s, c]] has angle phi; a reflection [[c, s], [s, -c]] is reported by its phi as well."""
    a, b, c, d = m[0][0], m[0][1], m[1][0], m[1][1]
    alpha, beta = 0.5 * math.hypot(a + d, c - b), 0.5 * math.hypot(a - d, b + c)
    largest = alpha + beta
    condition = abs(alpha - beta) / largest if largest > 0 else 0.0
    if alpha >= beta:
        return True, math.atan2(c - b, a + d), condition
    return False, math.atan2(b + c, a - d), condition


def _frame_matrix(proper: bool, phi: float) -> list[list[float]]:
    c, s = math.cos(phi), math.sin(phi)
    return [[c, -s], [s, c]] if proper else [[c, s], [s, -c]]


def _wrap_half_turn(angle_rad: float) -> float:
    """An angle difference modulo 180 deg, in (-90, 90] deg."""
    deg = math.degrees(angle_rad) % 180.0
    return deg - 180.0 if deg > 90.0 else deg


def _reflections(arrival: dict) -> int:
    first_order = arrival.get("first_order")
    if isinstance(first_order, dict) and isinstance(first_order.get("reflection_count"), int):
        return first_order["reflection_count"]
    count = 0
    for step in arrival.get("lineage") or []:
        event = str(step).rsplit(":", 1)[-1]
        if event == "tir" or event.startswith("tir_") or "reflect" in event:
            count += 1
    return count


def pupil_frame(arrival: dict, plane: dict, source_pose: dict | None, chief: dict) -> dict:
    """The map from the source's pupil (launch x, y) to the plane's (u, v): an orthogonal 2x2 matrix with its basis."""
    path_id = chief.get("path_id")
    reflections = _reflections(arrival)
    if reflections:
        raise FanError("PUPIL_FRAME_UNRESOLVED",
                       f"path {path_id} is reflected {reflections} time(s) before the plane; the orientation of its pupil "
                       "at the plane is not resolved for reflected paths")
    if arrival.get("reference_ray_promoted_at"):
        raise FanError("REFERENCE_RAY_PROMOTED",
                       f"path {path_id}: its chief ray was clipped at {', '.join(arrival['reference_ray_promoted_at'])} "
                       "and a pupil ray became its reference ray; fans about it are not chief-ray fans")
    if not isinstance(source_pose, dict) or len(source_pose.get("quaternion_wxyz") or []) != 4:
        raise FanError("PUPIL_FRAME_UNRESOLVED",
                       f"path {path_id}: no pose of source {chief.get('source_id')} in the run's project.json")
    q = source_pose["quaternion_wxyz"]
    e1, e2, d_source = _rotate(q, (1.0, 0.0, 0.0)), _rotate(q, (0.0, 1.0, 0.0)), _rotate(q, (0.0, 0.0, 1.0))
    d_chief = (chief["direction_x"], chief["direction_y"], chief["direction_z"])
    u_axis, v_axis = plane["u_axis"], plane["v_axis"]
    t1, t2 = _transport(e1, d_source, d_chief), _transport(e2, d_source, d_chief)
    pose_map = [[_dot(t1, u_axis), _dot(t2, u_axis)], [_dot(t1, v_axis), _dot(t2, v_axis)]]
    pose_proper, pose_phi, pose_condition = _orthogonal_factor(pose_map)
    expected_proper = chief["direction_n"] > 0  # no reflection: mirrored only when the plane faces against the light
    if pose_condition < FIRST_ORDER_MIN_CONDITION or pose_proper != expected_proper:
        raise FanError("PUPIL_FRAME_UNRESOLVED",
                       f"path {path_id}: the source's x and y axes carried to the plane do not span its (u, v) "
                       f"(conditioning {pose_condition:.3g}, {'proper' if pose_proper else 'mirrored'})")
    basis: dict[str, Any] = {"method": "source axes carried along the chief ray, projected onto (u, v)",
                             "orientation": "SOURCE_POSE"}
    phi, proper = pose_phi, pose_proper
    first_order = arrival.get("first_order")
    matrix = first_order.get("matrix") if isinstance(first_order, dict) else None
    candidates = []
    if isinstance(matrix, list) and len(matrix) == 4:
        for name, rows in (("A", (0, 1)), ("C", (2, 3))):
            block = [[float(matrix[r][0]), float(matrix[r][1])] for r in rows]
            block_proper, block_phi, condition = _orthogonal_factor(block)
            if condition >= FIRST_ORDER_MIN_CONDITION and block_proper == expected_proper:
                candidates.append((condition, name, block_phi))
    if candidates:
        _condition, name, block_phi = max(candidates)
        difference = _wrap_half_turn(block_phi - pose_phi)
        if abs(difference) > FRAME_AGREEMENT_DEG:
            raise FanError("PUPIL_FRAME_UNRESOLVED",
                           f"path {path_id}: the engine's first-order map and the source pose disagree on the pupil "
                           f"orientation by {difference:.3g} deg")
        phi = pose_phi + math.radians(difference)  # the engine's orientation, its sign from the transported axes
        basis = {"method": f"orientation of the engine's first-order {name} block; sign from the source axes carried "
                           "along the chief ray", "orientation": "FIRST_ORDER", "pose_deviation_deg": difference}
    elif isinstance(first_order, dict):
        basis["first_order"] = "no well-conditioned block"
    else:
        basis["first_order"] = arrival.get("first_order_issue") or "absent"
    return {"matrix": _frame_matrix(proper, phi), "rotation_deg": math.degrees(phi), "mirrored": not proper, **basis}


def tangential_axis(u_mm: float, v_mm: float) -> tuple[float, float]:
    """Unit vector in the plane's (u, v) from the plane origin towards the chief ray's image point; +v at the origin."""
    length = math.hypot(u_mm, v_mm)
    if length < 1e-9:
        return 0.0, 1.0
    return u_mm / length, v_mm / length


# ------------------------------------------------------------------------------------------------------- ring fits


def _fit(angles: list[float], values: list[float], harmonics: int) -> tuple[list[float], float]:
    """Least-squares Fourier coefficients [a0, a1, b1, ...] of equally spaced samples, and the RMS residual."""
    n = len(values)
    coefficients = [sum(values) / n]
    for m in range(1, harmonics + 1):
        coefficients.append(2.0 / n * sum(v * math.cos(m * a) for a, v in zip(angles, values)))
        coefficients.append(2.0 / n * sum(v * math.sin(m * a) for a, v in zip(angles, values)))
    residual = math.sqrt(sum((v - _evaluate(coefficients, a)) ** 2 for a, v in zip(angles, values)) / n)
    return coefficients, residual


def _evaluate(coefficients: list[float], angle: float) -> float:
    value = coefficients[0]
    for m in range(1, (len(coefficients) - 1) // 2 + 1):
        value += coefficients[2 * m - 1] * math.cos(m * angle) + coefficients[2 * m] * math.sin(m * angle)
    return value


def _angle_gap(a: float, b: float) -> float:
    return abs((a - b + math.pi) % (2.0 * math.pi) - math.pi)


def _equally_spaced(angles: list[float]) -> bool:
    ordered = sorted(a % (2.0 * math.pi) for a in angles)
    step = 2.0 * math.pi / len(ordered)
    gaps = [b - a for a, b in zip(ordered, ordered[1:])] + [ordered[0] + 2.0 * math.pi - ordered[-1]]
    return all(abs(g - step) < 1e-6 for g in gaps)


def rays_per_ring(pupil: list[dict], launched: Any) -> tuple[int | None, int | None, str]:
    """(rays per ring, rings launched, basis) of a path's pupil sampling, from the engine's pupil numbering.

    The engine numbers its pupil rays ray_index = 1 + ring_index * per_ring + ring_position_index (1..pupil_ray_count),
    so any arrived ray beyond the first ring gives per_ring whatever was vignetted; the arrival's pupil_ray_count
    (rays launched) then gives the number of rings. With rays of the first ring only, a ring whose positions 0..n-1 are
    all present and equally spaced over the whole circle has every ray of its ring (n = per_ring)."""
    candidates = set()
    for ray in pupil:
        ring, position, index = ray.get("ring_index"), ray.get("ring_position_index"), ray.get("ray_index")
        if isinstance(ring, int) and ring >= 1 and isinstance(position, int) and isinstance(index, int):
            quotient, remainder = divmod(index - 1 - position, ring)
            candidates.add(quotient if remainder == 0 else -1)
    positions = [r["ring_position_index"] for r in pupil if isinstance(r.get("ring_position_index"), int)]
    launched = launched if isinstance(launched, int) and not isinstance(launched, bool) and launched > 0 else None
    if len(candidates) == 1:
        per_ring = next(iter(candidates))
        rings = [r["ring_index"] for r in pupil if isinstance(r.get("ring_index"), int)]
        if per_ring > max(positions, default=-1):
            if launched is None:
                return per_ring, None, ("ray_index = 1 + ring_index * rays_per_ring + ring_position_index (the engine's "
                                        "pupil numbering); rings launched unknown (no pupil_ray_count)")
            if launched % per_ring == 0 and launched // per_ring > max(rings, default=-1):
                return per_ring, launched // per_ring, ("ray_index = 1 + ring_index * rays_per_ring + "
                                                        "ring_position_index (the engine's pupil numbering); rings "
                                                        "launched = pupil_ray_count / rays_per_ring")
            return None, None, (f"not determined: the pupil numbering gives {per_ring} rays per ring, which does not "
                                f"fit the {launched} pupil rays launched (pupil_ray_count)")
    if candidates:
        return None, None, "the arrived rays' indices do not follow the engine's pupil numbering"
    by_ring: dict[Any, list[dict]] = {}
    for ray in pupil:
        by_ring.setdefault(ray.get("ring_index"), []).append(ray)
    for samples in by_ring.values():
        indices = [s["ring_position_index"] for s in samples if isinstance(s.get("ring_position_index"), int)]
        if (len(samples) >= 3 and len(indices) == len(samples) and sorted(indices) == list(range(len(samples)))
                and _equally_spaced([math.atan2(s["launch_offset_y"], s["launch_offset_x"]) for s in samples])):
            n = len(samples)
            rings_launched = launched // n if launched is not None and launched % n == 0 else None
            return n, rings_launched, "a complete first ring (positions 0..n-1 equally spaced over the circle)"
    return None, None, "not determined: only rays of the first ring arrived, and not all of them"


def _longitudinal_mm(transverse_mm: float, slope: float, along_light: float) -> float | None:
    return None if abs(slope) < 1e-12 else along_light * (-transverse_mm / slope)


def _chief(rays: list[dict], path_id: str | None) -> dict:
    chief = next((r for r in rays if r.get("chief")), None)
    if chief is None:
        raise FanError("CHIEF_RAY_MISSING", f"path {path_id} has no chief ray at the plane")
    return chief


def _fan_axis_not_sampled(path_id: Any, empty: list[str], axes: dict[str, tuple[float, float]],
                          rings: dict[int, list[dict]], complete: list[int], partial: list[int],
                          per_ring: int | None) -> FanError:
    """The refusal of a path one of whose fans has no point: no complete ring to fit, and no arrived ray on the fan's
    azimuths. Its details give the fan azimuths, the rings and the pupil azimuths of the rays that arrived."""
    azimuths = {name: sorted(round(math.degrees(math.atan2(sign * av, sign * au)) % 360.0, 6) for sign in (1.0, -1.0))
                for name, (au, av) in axes.items()}
    ray_azimuths = {str(ring): sorted(round(math.degrees(s["angle"]) % 360.0, 3) for s in samples)
                    for ring, samples in sorted(rings.items())}
    nearest = {}
    for name in empty:
        targets = [math.radians(a) for a in azimuths[name]]
        nearest[name] = round(math.degrees(min(_angle_gap(s["angle"], t) for samples in rings.values()
                                               for s in samples for t in targets)), 6)
    counts = ", ".join(f"{ring}: {len(rings[ring])}" for ring in partial)
    message = (f"path {path_id}: the {' and '.join(empty)} fan{'s have' if len(empty) > 1 else ' has'} no point: "
               f"no ring is complete (rings partial: {partial}; rays arrived per ring {{{counts}}} of "
               f"{per_ring if per_ring is not None else 'an undetermined number'}), so no ring is fitted, and no "
               "arrived ray lies on the fan azimuths ("
               + "; ".join(f"{name} {', '.join(f'{a:g}' for a in azimuths[name])} deg, nearest ray "
                           f"{nearest[name]:g} deg away" for name in empty)
               + "); the arrived rays' pupil azimuths per ring are in details.ray_azimuths_deg")
    return FanError("FAN_AXIS_NOT_SAMPLED", message, details={
        "fans_without_points": empty, "fan_azimuths_deg": azimuths,
        "rings": {"complete": complete, "partial": partial, "rays_per_ring": per_ring},
        "nearest_ray_deg": nearest, "ray_azimuths_deg": ray_azimuths,
        "azimuth_tolerance_rad": AZIMUTH_TOLERANCE_RAD})


def path_fans(rays: list[dict], frame: dict, wavefront: dict | None = None, zones: int | None = None,
              arrival: dict | None = None) -> dict:
    """Tangential and sagittal fans of one path (one field point at one wavelength).

    ``rays``: the path's rows of 'results rays' at the plane; ``frame``: the pupil frame (pupil_frame(); its 'matrix'
    maps the launch offsets (x, y) to the plane's (u, v)); ``arrival``: the path's arrival of 'results monitor'."""
    if zones is not None and zones < 1:
        raise FanError("BAD_ARGUMENT", f"zones must be at least 1 (got {zones})")
    path_id = rays[0].get("path_id") if rays else None
    counted = [r for r in rays if r.get("counted") is True]
    if not counted:
        raise FanError("PATH_NOT_COUNTED", f"path {path_id} has no counted rays at the plane (outside its active area)")
    chief = _chief(counted, path_id)
    pupil = [r for r in counted if not r.get("chief")]
    if not pupil:
        raise FanError("PUPIL_RAYS_MISSING", f"path {chief.get('path_id')} has no pupil rays at the plane "
                       "(run with --ray-data full)")
    direction_n = chief["direction_n"]
    if abs(direction_n) < GRAZING_DIRECTION_N:
        raise FanError("RAY_GRAZING", f"path {chief['path_id']}: the chief ray runs along the plane (direction_n "
                       f"{direction_n:.3g})")
    if any(abs(r["direction_n"]) < GRAZING_DIRECTION_N or (r["direction_n"] > 0) != (direction_n > 0) for r in pupil):
        raise FanError("RAY_DIRECTION_MIXED", f"path {chief['path_id']}: pupil rays cross the plane grazing or against "
                       "the chief ray's direction")
    along_light = 1.0 if direction_n > 0 else -1.0
    (m00, m01), (m10, m11) = frame["matrix"]
    tu, tv = tangential_axis(chief["u_mm"], chief["v_mm"])
    axes = {"tangential": (tu, tv), "sagittal": (tv, -tu)}
    opd = {item["ray_index"]: item["opd_waves"] for item in (wavefront or {}).get("rays", [])}
    chief_su = chief["direction_u"] / direction_n
    chief_sv = chief["direction_v"] / direction_n

    rings: dict[int, list[dict]] = {}
    for ray in pupil:
        lx, ly = ray["launch_offset_x"], ray["launch_offset_y"]
        pu, pv = m00 * lx + m01 * ly, m10 * lx + m11 * ly
        rings.setdefault(ray["ring_index"], []).append({
            "ray_index": ray["ray_index"], "radius": math.hypot(pu, pv), "angle": math.atan2(pv, pu),
            "du": ray["u_mm"] - chief["u_mm"], "dv": ray["v_mm"] - chief["v_mm"],
            "su": ray["direction_u"] / ray["direction_n"] - chief_su,
            "sv": ray["direction_v"] / ray["direction_n"] - chief_sv,
            "opd": opd.get(ray["ray_index"]), "position": ray.get("ring_position_index")})
    arrival = arrival or {}
    launched, arrived = arrival.get("pupil_ray_count"), arrival.get("pupil_rays_arrived")
    per_ring, rings_launched, per_ring_basis = rays_per_ring(pupil, launched)
    complete = sorted(k for k, samples in rings.items()
                      if per_ring is not None and len(samples) == per_ring and per_ring >= 3
                      and _equally_spaced([s["angle"] for s in samples]))
    partial = sorted(k for k in rings if k not in complete)

    fans: dict[str, list[dict]] = {"tangential": [], "sagittal": []}
    harmonics_used = 0
    for ring, samples in sorted(rings.items()):
        radius = sum(s["radius"] for s in samples) / len(samples)
        fits = None
        if ring in complete:
            angles = [s["angle"] for s in samples]
            harmonics = min(MAX_HARMONIC, (len(samples) - 1) // 2)
            harmonics_used = max(harmonics_used, harmonics)
            fits = {key: _fit(angles, [s[key] for s in samples], harmonics) for key in ("du", "dv", "su", "sv")}
            if all(s["opd"] is not None for s in samples):
                fits["opd"] = _fit(angles, [s["opd"] for s in samples], harmonics)
            residual_um = 1000.0 * math.hypot(fits["du"][1], fits["dv"][1])
        for name, (au, av) in axes.items():
            for sign in (1.0, -1.0):
                azimuth = math.atan2(sign * av, sign * au)
                on_axis = min(samples, key=lambda s: _angle_gap(s["angle"], azimuth))
                if _angle_gap(on_axis["angle"], azimuth) <= AZIMUTH_TOLERANCE_RAD:
                    du, dv, su, sv = on_axis["du"], on_axis["dv"], on_axis["su"], on_axis["sv"]
                    row = {"ray_index": on_axis["ray_index"], "zone": ring, "rho": sign * on_axis["radius"],
                           "sampled": True, "fit_residual_um": None, "opd_waves": on_axis["opd"]}
                elif fits is not None:
                    du, dv, su, sv = (_evaluate(fits[k][0], azimuth) for k in ("du", "dv", "su", "sv"))
                    row = {"ray_index": None, "zone": ring, "rho": sign * radius, "sampled": False,
                           "fit_residual_um": residual_um,
                           "opd_waves": _evaluate(fits["opd"][0], azimuth) if "opd" in fits else None}
                else:
                    continue
                transverse, slope = du * au + dv * av, su * au + sv * av
                row["transverse_um"] = transverse * 1000.0
                row["longitudinal_mm"] = _longitudinal_mm(transverse, slope, along_light)
                fans[name].append(row)

    empty = [name for name, rows in fans.items() if not rows]
    if empty:
        raise _fan_axis_not_sampled(chief["path_id"], empty, axes, rings, complete, partial, per_ring)

    eligible = sorted({row["zone"] for rows in fans.values() for row in rows})
    for name in fans:  # per fan, so that every fan keeps its outermost ring
        own = sorted({row["zone"] for row in fans[name]})
        kept = set(own)
        if zones is not None and zones < len(own):
            step = len(own) / zones
            kept = {own[min(len(own) - 1, round((i + 1) * step) - 1)] for i in range(zones)}
        fans[name] = sorted((row for row in fans[name] if row["zone"] in kept), key=lambda row: row["rho"])

    cu = sum(r["u_mm"] for r in pupil) / len(pupil)
    cv = sum(r["v_mm"] for r in pupil) / len(pupil)
    spot_rms_um = 1000.0 * math.sqrt(sum((r["u_mm"] - cu) ** 2 + (r["v_mm"] - cv) ** 2 for r in pupil) / len(pupil))
    values = list(opd.values())
    opd_rms = None
    if values:
        mean = sum(values) / len(values)
        opd_rms = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
    wavefront = wavefront or {}
    return {"path_id": chief["path_id"], "source_id": chief.get("source_id"), "wavelength_nm": chief["wavelength_nm"],
            "chief_uv_mm": [chief["u_mm"], chief["v_mm"]],
            "tangential_axis_uv": list(axes["tangential"]), "sagittal_axis_uv": list(axes["sagittal"]),
            "light_along_normal": along_light > 0,
            "pupil_frame": dict(frame),
            "pupil_rays": len(pupil), "pupil_rays_launched": launched, "pupil_rays_arrived": arrived,
            "vignetted_rays": launched - arrived if isinstance(launched, int) and isinstance(arrived, int) else None,
            "rings": {"present": sorted(rings), "complete": complete, "partial": partial, "rays_per_ring": per_ring,
                      "rings_launched": rings_launched, "rays_per_ring_basis": per_ring_basis},
            "ring_fit": {"harmonics": harmonics_used, "method": "least-squares Fourier series in the pupil azimuth "
                                                                "per complete ring"},
            "rho_max": max(sum(s["radius"] for s in samples) / len(samples) for samples in rings.values()),
            "fan_rho_max": {name: max(abs(row["rho"]) for row in rows) for name, rows in fans.items()},
            "zones": {name: sorted({row["zone"] for row in rows}) for name, rows in fans.items()},
            "zones_requested": zones, "zones_available": len(eligible),
            "fans": fans, "spot_rms_um": spot_rms_um, "opd_rms_waves": opd_rms,
            "engine": {"opd_rms_waves": wavefront.get("rms_waves"), "opd_pv_waves": wavefront.get("pv_waves"),
                       "wavefront_status": wavefront.get("status"),
                       "wavefront_reason": None if wavefront.get("status") == "OK" else wavefront.get("reason"),
                       "wavefront_message": None if wavefront.get("status") == "OK" else wavefront.get("message")}}


# ------------------------------------------------------------------------------------------------- imaging document


def check_imaging(imaging: Any) -> None:
    """Refuse an imaging document that is not the JSON output of 'os-optics results imaging'."""
    hint = "--imaging must be the JSON output of 'os-optics results imaging'"
    if not isinstance(imaging, dict) or not isinstance(imaging.get("quantities", []), list):
        raise FanError("IMAGING_INVALID", f"{hint}: no 'quantities' list")
    for quantity in imaging.get("quantities", []):
        if not isinstance(quantity, dict):
            raise FanError("IMAGING_INVALID", f"{hint}: a quantity is not an object")
        if str(quantity.get("quantity_id", "")).endswith(".geometric_spot_rms_radius") and quantity.get("state") == "KNOWN":
            scope = quantity.get("scope")
            if (not isinstance(scope, dict) or not isinstance(scope.get("source_id"), str)
                    or not isinstance(quantity.get("value"), (int, float))):
                raise FanError("IMAGING_INVALID", f"{hint}: {quantity.get('quantity_id')} has no source scope or value")


def engine_spot_rms(imaging: dict, monitor: str | None = None) -> dict[tuple[str, float | None], float]:
    """(source_id, wavelength_nm) -> the engine's geometric RMS spot radius (um) from a 'results imaging' document,
    for the plane ``monitor`` when given."""
    check_imaging(imaging)
    out: dict[tuple[str, float | None], float] = {}
    for quantity in imaging.get("quantities", []):
        if str(quantity.get("quantity_id", "")).endswith(".geometric_spot_rms_radius") and quantity.get("state") == "KNOWN":
            scope = quantity["scope"]
            if monitor is not None and scope.get("observation_id") not in (None, monitor):
                continue
            wavelength = scope.get("wavelength_nm")
            out[(scope["source_id"], float(wavelength) if isinstance(wavelength, (int, float)) else None)] = \
                float(quantity["value"])
    return out


def _spot_for(spots: dict[tuple[str, float | None], float], source_id: str | None, wavelength_nm: float) -> float | None:
    for (source, wavelength), value in spots.items():
        if source == source_id and (wavelength is None or math.isclose(wavelength, wavelength_nm, rel_tol=1e-12)):
            return value
    return None


# ------------------------------------------------------------------------------------------------------------ build


def _source_poses(run_dir: Path) -> dict[str, dict]:
    try:
        project = json.loads((run_dir / "project.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    poses: dict[str, dict] = {}
    for component in project.get("components", []) if isinstance(project, dict) else []:
        if (isinstance(component, dict) and component.get("optical_role") == "source"
                and isinstance(component.get("instance_id"), str) and isinstance(component.get("nominal_pose"), dict)):
            poses[component["instance_id"]] = component["nominal_pose"]
    return poses


def collect(run_dir: Path, monitor: str, paths: list[str] | None = None, runner=subprocess.run) -> dict:
    """Everything the fans of ``monitor`` need from the engine and the run: rays, plane, arrivals, path tree, OPD."""
    rows = load_rays(run_dir, monitor, runner)
    monitor_document = load_monitor(run_dir, monitor, runner)
    tree = load_paths(run_dir, runner)
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("crossing_index", 0) == 0:
            grouped.setdefault(row["path_id"], []).append(row)
    reaching = {p for p, rays in grouped.items() if any(r.get("counted") is True for r in rays)}
    known = {p.get("path_id"): p for p in tree if isinstance(p, dict)}
    if paths:
        missing = [p for p in paths if p not in reaching]
        if missing:
            details = []
            for path_id in missing:
                entry = known.get(path_id)
                if entry is None:
                    details.append(f"{path_id} (no such path)")
                elif path_id in grouped:
                    details.append(f"{path_id} (outside the plane's active area)")
                else:
                    details.append(f"{path_id} (termination {entry.get('termination')}"
                                   + (f" at {entry['termination_surface_id']}" if entry.get("termination_surface_id")
                                      else "") + ")")
            raise FanError("PATH_NOT_FOUND", f"no counted rays at {monitor} of path(s) {', '.join(details)}")
        selected = list(paths)
    else:
        selected = sorted(reaching)
    unreached = []
    if not paths:
        for path_id in sorted(set(grouped) - reaching):
            entry = known.get(path_id, {})
            unreached.append({"path_id": path_id, "source_id": grouped[path_id][0].get("source_id"),
                              "wavelength_nm": grouped[path_id][0].get("wavelength_nm"),
                              "reason": "NO_COUNTED_RAYS_AT_MONITOR", "termination": entry.get("termination"),
                              "termination_surface_id": entry.get("termination_surface_id")})
        lines = {(grouped[p][0].get("source_id"), grouped[p][0].get("wavelength_nm")) for p in grouped}
        for entry in tree:
            if not isinstance(entry, dict) or entry.get("children") or entry.get("path_id") in grouped:
                continue
            if (entry.get("source_id"), entry.get("wavelength_nm")) in lines:
                continue
            unreached.append({"path_id": entry.get("path_id"), "source_id": entry.get("source_id"),
                              "wavelength_nm": entry.get("wavelength_nm"), "reason": "NO_RAYS_AT_MONITOR",
                              "termination": entry.get("termination"),
                              "termination_surface_id": entry.get("termination_surface_id")})
    arrivals = {a["beam"]["path_id"]: a for a in monitor_document.get("arrivals", [])
                if isinstance(a, dict) and a.get("crossing_index", 0) == 0 and isinstance(a.get("beam"), dict)}
    wavefronts = {path_id: load_wavefront(run_dir, monitor, path_id, runner) for path_id in selected}
    return {"run": str(run_dir), "monitor": monitor, "plane": monitor_document["plane"], "rays": grouped,
            "arrivals": arrivals, "selected": selected, "explicit": bool(paths), "unreached": unreached,
            "wavefronts": wavefronts, "source_poses": _source_poses(run_dir)}


def assemble(collected: dict, zones: int | None = None, imaging: dict | None = None) -> dict:
    """The fans document from collect()'s data (no engine call)."""
    if zones is not None and zones < 1:
        raise FanError("BAD_ARGUMENT", f"--zones must be at least 1 (got {zones})")
    spots = engine_spot_rms(imaging, collected["monitor"]) if imaging is not None else {}
    results, refused = [], []
    for path_id in collected["selected"]:
        rays = collected["rays"][path_id]
        try:
            counted = [r for r in rays if r.get("counted") is True]
            chief = _chief(counted, path_id)
            source_id = chief.get("source_id")
            frame = pupil_frame(collected["arrivals"].get(path_id, {}), collected["plane"],
                                collected["source_poses"].get(source_id), chief)
            fan = path_fans(rays, frame, collected["wavefronts"].get(path_id), zones, collected["arrivals"].get(path_id))
        except FanError as exc:
            if collected["explicit"]:
                raise
            entry = {"path_id": path_id, "source_id": rays[0].get("source_id"),
                     "wavelength_nm": rays[0].get("wavelength_nm"), "code": exc.code, "message": str(exc)}
            if exc.details is not None:
                entry["details"] = exc.details
            refused.append(entry)
            continue
        fan["engine"]["spot_rms_um"] = _spot_for(spots, fan["source_id"], fan["wavelength_nm"])
        results.append(fan)
    if not results:
        reasons = [f"{item['path_id']}: {item['code']}" for item in refused]
        reasons += [f"{item['path_id']}: {item['reason']}" for item in collected["unreached"]]
        raise FanError("NO_FANS", f"no path at {collected['monitor']} gives fans"
                       + (f" ({'; '.join(reasons)})" if reasons else " (no rays at the plane)"))
    return {"schema": "os-optics-kit.ray-fans", "schema_version": 2, "run": collected["run"],
            "monitor": collected["monitor"],
            "basis": "published engine ray data (results rays, monitor, paths, wavefront) and the run's source poses; "
                     "geometric optics",
            "conventions": {
                "rho": "launch offset in units of the source's 1/e^2 radii, projected on the fan direction",
                "pupil_frame": "source x and y carried to the plane's (u, v); orientation from the engine's first-order "
                               "transfer where it is well conditioned (pupil_frame per path)",
                "tangential": "from the plane origin towards the chief ray's image point (+v at the origin); "
                              "sagittal: tangential turned by -90 deg (tangential_axis_uv, sagittal_axis_uv per path)",
                "fan_points": "rays on a fan azimuth as sampled; other points from a Fourier fit of each complete ring "
                              "in the pupil azimuth (fit_residual_um); a path with a fan without points is refused "
                              "(FAN_AXIS_NOT_SAMPLED)",
                "transverse_um": "displacement from the chief ray along the fan direction",
                "longitudinal_mm": "where a fan ray crosses the chief ray, from the plane along its normal in the "
                                   "direction of the light; positive behind the plane",
                "opd_rms_waves": "RMS of the engine's per-ray OPD; equals engine.opd_rms_waves by construction "
                                 "(consistency check)"},
            "paths": results, "unreached": collected["unreached"], "refused": refused}


def build(run_dir: Path, monitor: str, paths: list[str] | None = None, zones: int | None = None,
          imaging: dict | None = None, runner=subprocess.run) -> dict:
    if zones is not None and zones < 1:
        raise FanError("BAD_ARGUMENT", f"--zones must be at least 1 (got {zones})")
    if imaging is not None:
        check_imaging(imaging)
    return assemble(collect(run_dir, monitor, paths, runner), zones, imaging)


def _error(code: str, message: str, engine_code: str | None = None, details: dict | None = None) -> int:
    error: dict[str, Any] = {"code": code, "message": message}
    if engine_code:
        error["engine_code"] = engine_code
    if details is not None:
        error["details"] = details
    print(json.dumps({"error": error}), file=sys.stderr)
    return 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="os-optics-kit fans", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run", type=Path)
    parser.add_argument("--monitor", required=True)
    parser.add_argument("--path", action="append", dest="paths")
    parser.add_argument("--zones", type=int, help="keep, per fan, N (>= 1) of the rings that carry its points")
    parser.add_argument("--imaging", type=Path, help="a 'results imaging' document for the engine's spot radius")
    parser.add_argument("--out", type=Path)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help returns 0; a usage error is a JSON error with exit 4
        if exc.code in (0, None):
            return 0
        return _error("BAD_ARGUMENT", "invalid arguments")
    if args.zones is not None and args.zones < 1:
        return _error("BAD_ARGUMENT", f"--zones must be at least 1 (got {args.zones})")
    imaging = None
    try:
        if args.imaging:
            try:
                imaging = json.loads(args.imaging.read_text(encoding="utf-8"))
            except OSError as exc:
                raise FanError("IMAGING_NOT_READABLE", f"cannot read {args.imaging}: {exc.strerror or exc}") from None
            except ValueError:
                raise FanError("IMAGING_INVALID",
                               f"{args.imaging} is not JSON; --imaging must be the JSON output of "
                               "'os-optics results imaging'") from None
            check_imaging(imaging)
        if args.out and not args.out.parent.is_dir():
            raise FanError("OUTPUT_NOT_WRITABLE", f"the directory of {args.out} does not exist")
        document = build(args.run, args.monitor, args.paths, args.zones, imaging)
        text = json.dumps(document, indent=2)
        if args.out:
            try:
                args.out.write_text(text, encoding="utf-8")
            except OSError as exc:
                raise FanError("OUTPUT_NOT_WRITABLE", f"cannot write {args.out}: {exc.strerror or exc}") from None
    except FanError as exc:
        return _error(exc.code, str(exc), exc.engine_code, exc.details)
    if args.out:
        print(json.dumps({"status": "WRITTEN", "path": str(args.out), "paths": len(document["paths"])}))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
