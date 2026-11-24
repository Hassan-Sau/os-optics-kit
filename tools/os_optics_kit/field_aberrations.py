"""Field curvature and distortion of a saved run, per field point and wavelength, from published engine data.

os-optics-kit field RUN --monitor PLANE [--efl MM] [--out field.json]

Nothing is traced here. Inputs: the run's project.json (source poses) and 'os-optics results rays' at the plane (through
the ray fans of this kit). For each path (one field point at one wavelength):

* field angle: the angle between the source's emission axis and the optical axis, the emission axis of the source whose
  chief ray lands nearest the plane's origin (the on-axis field), in degrees;
* image height: the chief ray's distance from the plane's origin in (u, v), in mm;
* distortion: (h - h_ref) / h_ref in percent, h_ref = EFL * tan(field angle). EFL is --efl, or, when not given, the
  image height of the smallest non-zero field divided by its tan(angle) at the same wavelength (distortion relative to
  the smallest field: that field reads 0 by construction, and the result says which reference was used);
* field curvature: the tangential and sagittal focus positions along the plane normal, from the plane, in mm (positive
  behind the plane): the mean longitudinal aberration of the innermost pupil zone of each fan (the paraxial limit of
  the fan within the run's sampling).

Output is JSON.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

from . import ray_fans


class FieldError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _rotate(q: list[float], v: tuple[float, float, float]) -> tuple[float, float, float]:
    w, x, y, z = q
    norm = math.sqrt(w * w + x * x + y * y + z * z)
    w, x, y, z = w / norm, x / norm, y / norm, z / norm
    vx, vy, vz = v
    # v' = q v q*
    tx = 2 * (y * vz - z * vy)
    ty = 2 * (z * vx - x * vz)
    tz = 2 * (x * vy - y * vx)
    return (vx + w * tx + (y * tz - z * ty), vy + w * ty + (z * tx - x * tz), vz + w * tz + (x * ty - y * tx))


def source_axes(project: dict) -> dict[str, tuple[float, float, float]]:
    """instance_id -> unit emission axis (the source frame's local +z) of every source component."""
    axes = {}
    for component in project.get("components", []):
        if component.get("optical_role") == "source":
            axes[component["instance_id"]] = _rotate(component["nominal_pose"]["quaternion_wxyz"], (0.0, 0.0, 1.0))
    return axes


def _angle_deg(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    dot = max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))
    return math.degrees(math.acos(dot))


def _inner_zone_focus(rows: list[dict]) -> float | None:
    values = [row for row in rows if row["longitudinal_mm"] is not None]
    if not values:
        return None
    inner = min(abs(row["rho"]) for row in values)
    zone = [row["longitudinal_mm"] for row in values if abs(abs(row["rho"]) - inner) < 1e-9]
    return sum(zone) / len(zone)


def analyze(paths: list[dict], axes: dict[str, tuple[float, float, float]], efl_mm: float | None = None) -> dict:
    missing = sorted({p["source_id"] for p in paths} - set(axes))
    if missing:
        raise FieldError("SOURCE_POSE_MISSING", f"no source pose for {', '.join(missing)} in the run's project.json")
    axis_path = min(paths, key=lambda p: math.hypot(*p["chief_uv_mm"]))
    axis = axes[axis_path["source_id"]]
    rows = []
    for path in paths:
        angle = _angle_deg(axes[path["source_id"]], axis)
        rows.append({"path_id": path["path_id"], "source_id": path["source_id"], "wavelength_nm": path["wavelength_nm"],
                     "field_angle_deg": angle, "image_height_mm": math.hypot(*path["chief_uv_mm"]),
                     "tangential_focus_mm": _inner_zone_focus(path["fans"]["tangential"]),
                     "sagittal_focus_mm": _inner_zone_focus(path["fans"]["sagittal"])})
    references = {}
    for row in rows:
        wavelength = row["wavelength_nm"]
        if efl_mm is not None:
            references[wavelength] = (efl_mm, "stated EFL")
            continue
        field = [r for r in rows if r["wavelength_nm"] == wavelength and r["field_angle_deg"] > 1e-6]
        if field:
            smallest = min(field, key=lambda r: r["field_angle_deg"])
            efl = smallest["image_height_mm"] / math.tan(math.radians(smallest["field_angle_deg"]))
            references[wavelength] = (efl, f"smallest field {smallest['field_angle_deg']:.6g} deg ({smallest['path_id']})")
    for row in rows:
        ref = references.get(row["wavelength_nm"])
        row["astigmatism_mm"] = (None if row["tangential_focus_mm"] is None or row["sagittal_focus_mm"] is None
                                 else row["tangential_focus_mm"] - row["sagittal_focus_mm"])
        if ref is None or row["field_angle_deg"] <= 1e-6:
            row["reference_height_mm"], row["distortion_pct"] = (0.0 if ref else None), (0.0 if ref else None)
        else:
            h_ref = ref[0] * math.tan(math.radians(row["field_angle_deg"]))
            row["reference_height_mm"] = h_ref
            row["distortion_pct"] = 100.0 * (row["image_height_mm"] - h_ref) / h_ref
    rows.sort(key=lambda r: (r["wavelength_nm"], r["field_angle_deg"]))
    return {"axis_source_id": axis_path["source_id"],
            "references": {str(w): {"efl_mm": e, "basis": b} for w, (e, b) in sorted(references.items())},
            "fields": rows}


def build(run_dir: Path, monitor: str, efl_mm: float | None = None) -> dict:
    project_file = run_dir / "project.json"
    try:
        project = json.loads(project_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FieldError("PROJECT_UNREADABLE", f"{project_file}: {exc}") from None
    fans = ray_fans.build(run_dir, monitor)
    document = analyze(fans["paths"], source_axes(project), efl_mm)
    return {"schema": "os-optics-kit.field-aberrations", "schema_version": 1, "run": str(run_dir), "monitor": monitor,
            "basis": "published engine ray data; field angle from source poses; geometric optics",
            "conventions": {"distortion_pct": "(h - EFL tan(theta)) / (EFL tan(theta))",
                            "focus_mm": "along the plane normal from the plane, positive behind it; innermost pupil zone"},
            **document}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="os-optics-kit field", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run", type=Path)
    parser.add_argument("--monitor", required=True)
    parser.add_argument("--efl", type=float, help="effective focal length (mm) for the distortion reference")
    parser.add_argument("--out", type=Path)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code in (0, None):
            return 0
        print(json.dumps({"error": {"code": "BAD_ARGUMENT", "message": "invalid arguments"}}), file=sys.stderr)
        return 4
    try:
        document = build(args.run, args.monitor, args.efl)
    except (FieldError, ray_fans.FanError) as exc:
        print(json.dumps({"error": {"code": exc.code, "message": str(exc)}}), file=sys.stderr)
        return 4
    text = json.dumps(document, indent=2)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(json.dumps({"status": "WRITTEN", "path": str(args.out), "fields": len(document["fields"])}))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
