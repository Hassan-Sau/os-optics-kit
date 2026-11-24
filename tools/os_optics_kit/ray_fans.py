"""Ray fans from a saved run: transverse and longitudinal aberration and OPD across the pupil, per field and wavelength.

os-optics-kit fans RUN --monitor PLANE [--path ID ...] [--zones N] [--out fans.json]

Everything is computed from published engine outputs ('os-optics results rays' and 'results wavefront'); nothing is
traced here. Each path of the run is one field point at one wavelength. For each path:

* pupil coordinates are the rays' launch offsets, normalized by the outermost ring (rho = 1 at the pupil edge);
* the tangential direction is the unit vector in the plane's (u, v) from the axis towards the chief ray's image point
  (+v for an on-axis field); the sagittal direction is perpendicular to it. Launch offsets, image displacements and
  slopes are projected onto both; a ray belongs to a fan when its other pupil component is within 1e-9 of zero. This
  assumes the source's launch frame is not rotated against the plane's (u, v) (no image rotation in the path);
* transverse aberration is a ray's image-plane displacement from the chief ray along the fan direction, in micrometres;
* longitudinal aberration is the distance along the plane normal, from the image plane, at which a fan ray crosses the
  chief ray in that direction: -(transverse) / (difference of their slopes du/dn or dv/dn), in millimetres; positive
  means the crossing lies behind the plane (further along the light). A ray parallel to the chief ray has none (null);
* OPD is the engine's own per-ray OPD ('results wavefront', reference plane through the chief ray, piston removed), in
  waves.

Cross-checks recorded in every result: the RMS spot radius of all pupil rays about their centroid against the engine's
'geometric_spot_rms_radius' when an imaging document is given (--imaging FILE, from 'results imaging'), and the RMS of
the per-ray OPD against the engine's 'rms_waves' of the same path. Pupil zones are the engine's rings; --zones keeps
every k-th ring so a fan lists N zones. Output is JSON.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys

AXIS_TOLERANCE = 1e-9


class FanError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _engine_json(argv: list[str], runner=subprocess.run):
    engine = shutil.which("os-optics")
    command = [engine] if engine else [sys.executable, "-m", "os_optics"]
    done = runner(command + argv, capture_output=True, text=True, encoding="utf-8")
    if done.returncode not in (0, 1):
        raise FanError("ENGINE_RESULTS_FAILED", (done.stderr or done.stdout or "").strip()[-500:])
    try:
        return json.loads(done.stdout)
    except (TypeError, ValueError):
        raise FanError("ENGINE_RESULTS_FAILED", "the engine did not return one JSON document: "
                       + (done.stderr or done.stdout or "").strip()[-500:]) from None


def load_rays(run_dir: Path, monitor: str, runner=subprocess.run) -> list[dict]:
    rows = _engine_json(["results", "rays", str(run_dir), "--monitor", monitor], runner)
    return rows["rows"] if isinstance(rows, dict) else rows


def load_wavefront(run_dir: Path, monitor: str, path_id: str, runner=subprocess.run) -> dict:
    """The engine's wavefront of one path; a refusal (e.g. an idealized element) is returned as status/message only."""
    try:
        return _engine_json(["results", "wavefront", str(run_dir), "--monitor", monitor, "--path", path_id], runner)
    except FanError as exc:
        try:
            document = json.loads(str(exc)[str(exc).find("{"):])
        except ValueError:
            document = {}
        return {"status": document.get("status") or "UNAVAILABLE",
                "message": document.get("message") or str(exc)[-300:], "rays": []}


def _pupil(rays: list[dict]) -> tuple[dict, float]:
    chief = next((r for r in rays if r.get("chief")), None)
    if chief is None:
        raise FanError("CHIEF_RAY_MISSING", f"path {rays[0].get('path_id')} has no chief ray")
    radius = max(math.hypot(r["launch_offset_x"], r["launch_offset_y"]) for r in rays)
    if radius <= 0:
        raise FanError("PUPIL_RAYS_MISSING", f"path {chief.get('path_id')} has no pupil rays (run with --ray-data full)")
    return chief, radius


def tangential_axis(u_mm: float, v_mm: float) -> tuple[float, float]:
    """Unit vector in the plane's (u, v) from the axis towards the chief ray's image point; +v on axis."""
    length = math.hypot(u_mm, v_mm)
    if length < 1e-9:
        return 0.0, 1.0
    return u_mm / length, v_mm / length


def _longitudinal_mm(transverse_mm: float, slope: float) -> float | None:
    return None if abs(slope) < 1e-12 else -transverse_mm / slope


def path_fans(rays: list[dict], wavefront: dict | None = None, zones: int | None = None) -> dict:
    """Tangential and sagittal fans of one path (one field point at one wavelength)."""
    counted = [r for r in rays if r.get("counted", True)]
    chief, radius = _pupil(counted)
    rings = sorted({r["ring_index"] for r in counted if r.get("ring_index") is not None})
    kept = set(rings)
    if zones and zones < len(rings):
        step = len(rings) / zones
        kept = {rings[min(len(rings) - 1, round((i + 1) * step) - 1)] for i in range(zones)}
    opd = {item["ray_index"]: item["opd_waves"] for item in (wavefront or {}).get("rays", [])}
    tu, tv = tangential_axis(chief["u_mm"], chief["v_mm"])
    su_, sv_ = -tv, tu
    fans: dict[str, list[dict]] = {"tangential": [], "sagittal": []}
    for ray in counted:
        if ray.get("chief") or ray.get("ring_index") not in kept:
            continue
        px, py = ray["launch_offset_x"] / radius, ray["launch_offset_y"] / radius
        du_mm, dv_mm = ray["u_mm"] - chief["u_mm"], ray["v_mm"] - chief["v_mm"]
        su = ray["direction_u"] / ray["direction_n"] - chief["direction_u"] / chief["direction_n"]
        sv = ray["direction_v"] / ray["direction_n"] - chief["direction_v"] / chief["direction_n"]
        # project onto the tangential (towards the chief ray's image point) and sagittal axes
        pt, ps = px * tu + py * tv, px * su_ + py * sv_
        dt, ds = du_mm * tu + dv_mm * tv, du_mm * su_ + dv_mm * sv_
        st, ss = su * tu + sv * tv, su * su_ + sv * sv_
        for name, rho, other, transverse, slope in (("tangential", pt, ps, dt, st), ("sagittal", ps, pt, ds, ss)):
            if abs(other) <= AXIS_TOLERANCE and abs(rho) > 0:
                fans[name].append({"ray_index": ray["ray_index"], "zone": ray["ring_index"], "rho": rho,
                                   "transverse_um": transverse * 1000.0,
                                   "longitudinal_mm": _longitudinal_mm(transverse, slope),
                                   "opd_waves": opd.get(ray["ray_index"])})
    for rows in fans.values():
        rows.sort(key=lambda row: row["rho"])
    pupil = [r for r in counted if not r.get("chief")]
    cu = sum(r["u_mm"] for r in pupil) / len(pupil)
    cv = sum(r["v_mm"] for r in pupil) / len(pupil)
    spot_rms_um = 1000.0 * math.sqrt(sum((r["u_mm"] - cu) ** 2 + (r["v_mm"] - cv) ** 2 for r in pupil) / len(pupil))
    values = list(opd.values())
    opd_rms = None
    if values:
        mean = sum(values) / len(values)
        opd_rms = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
    return {"path_id": chief["path_id"], "source_id": chief.get("source_id"), "wavelength_nm": chief["wavelength_nm"],
            "chief_uv_mm": [chief["u_mm"], chief["v_mm"]], "pupil_rays": len(pupil), "zones": sorted(kept),
            "fans": fans, "spot_rms_um": spot_rms_um, "opd_rms_waves": opd_rms,
            "engine": {"opd_rms_waves": (wavefront or {}).get("rms_waves"),
                       "opd_pv_waves": (wavefront or {}).get("pv_waves"),
                       "wavefront_status": (wavefront or {}).get("status"),
                       "wavefront_message": None if (wavefront or {}).get("status") == "OK"
                       else (wavefront or {}).get("message")}}


def engine_spot_rms(imaging: dict) -> dict[str, float]:
    """source_id -> the engine's geometric RMS spot radius (um) from a 'results imaging' document."""
    out = {}
    for quantity in imaging.get("quantities", []):
        if str(quantity.get("quantity_id", "")).endswith(".geometric_spot_rms_radius") and quantity.get("state") == "KNOWN":
            out[quantity["scope"]["source_id"]] = quantity["value"]
    return out


def build(run_dir: Path, monitor: str, paths: list[str] | None = None, zones: int | None = None,
          imaging: dict | None = None, runner=subprocess.run) -> dict:
    rows = load_rays(run_dir, monitor, runner)
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("crossing_index", 0) == 0:
            grouped.setdefault(row["path_id"], []).append(row)
    selected = paths or sorted(grouped)
    missing = [p for p in selected if p not in grouped]
    if missing:
        raise FanError("PATH_NOT_FOUND", f"no rays of path(s) {', '.join(missing)} at {monitor}")
    spots = engine_spot_rms(imaging or {})
    results = []
    for path_id in selected:
        fan = path_fans(grouped[path_id], load_wavefront(run_dir, monitor, path_id, runner), zones)
        fan["engine"]["spot_rms_um"] = spots.get(fan["source_id"])
        results.append(fan)
    return {"schema": "os-optics-kit.ray-fans", "schema_version": 1, "run": str(run_dir), "monitor": monitor,
            "basis": "published engine ray data (results rays, results wavefront); geometric optics",
            "pupil": "launch offsets normalized by the outermost ring; tangential = pupil y axis, sagittal = x axis",
            "paths": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="os-optics-kit fans", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run", type=Path)
    parser.add_argument("--monitor", required=True)
    parser.add_argument("--path", action="append", dest="paths")
    parser.add_argument("--zones", type=int)
    parser.add_argument("--imaging", type=Path, help="a 'results imaging' document for the engine's spot radius")
    parser.add_argument("--out", type=Path)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help returns 0; a usage error is a JSON error with exit 4
        if exc.code in (0, None):
            return 0
        print(json.dumps({"error": {"code": "BAD_ARGUMENT", "message": "invalid arguments"}}), file=sys.stderr)
        return 4
    try:
        imaging = json.loads(args.imaging.read_text(encoding="utf-8")) if args.imaging else None
        document = build(args.run, args.monitor, args.paths, args.zones, imaging)
    except FanError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": str(exc)}}), file=sys.stderr)
        return 4
    text = json.dumps(document, indent=2)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(json.dumps({"status": "WRITTEN", "path": str(args.out), "paths": len(document["paths"])}))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
