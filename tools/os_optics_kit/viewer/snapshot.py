"""Read-only atomic snapshot builder for native OS Optics Projects and saved run directories.

Every optical calculation, traced ray, power ledger entry, execution status, quantity state,
target assessment and freshness judgement comes directly from the published ``os-optics``
engine APIs. Nothing here re-implements optical physics or modifies any input file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .._engine import engine_version, require_engine
from .._version import __version__ as KIT_VERSION
from ..interop import json_safe
from ..result_summary import summarize_results

SNAPSHOT_SCHEMA = "os-optics-kit.local_viewer.snapshot"
SNAPSHOT_SCHEMA_VERSION = 1

#: Run directory files fingerprinted for atomic read bracketing and live-follow change detection.
WATCHED_RUN_FILES = (
    "manifest.json",
    "project.json",
    "engine_request.json",
    "simulation_result.json",
    "local_validation.json",
    "optical_scene.json",
    "optical_scene.glb",
    "ray_data.json",
)

ROLE_TO_PART_CLASS = {
    "source": "SOURCE",
    "lens": "LENS",
    "mirror": "MIRROR",
    "splitter": "BEAMSPLITTER",
    "beamsplitter": "BEAMSPLITTER",
    "filter": "FILTER",
    "polarizer": "POLARIZER",
    "waveplate": "WAVEPLATE",
    "aperture": "APERTURE",
    "prism": "PRISM",
    "detector": "DETECTOR",
    "dump": "DETECTOR",
}


class SnapshotError(RuntimeError):
    """A structured, non-transient refusal or invalid-input error."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), **self.details}


class SnapshotTransientError(SnapshotError):
    """A transient read/parse/consistency error (e.g. mid-write truncation or torn update).

    During live watching the server preserves ``last_valid_snapshot`` and enters ``waiting``
    until the target files settle into a valid, self-consistent state.
    """


@dataclass(frozen=True)
class TargetSpec:
    """Resolved read-only input specification for the local viewer."""

    kind: str  # "run" | "project"
    input_path: Path
    project_path: Path | None = None
    run_dir: Path | None = None
    configuration_id: str | None = None
    evaluation_mode: str = "none"  # "none" | "explicit_local_temp"
    evaluated_project_hash: str | None = None


@dataclass(frozen=True)
class BuiltSnapshot:
    """One immutable, self-consistent snapshot plus its optional Three.js scene document."""

    snapshot: dict[str, Any]
    scene_document: dict[str, Any] | None
    fingerprint: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def resolve_target(
    path: str | Path,
    *,
    project: str | Path | None = None,
    run: str | Path | None = None,
    configuration: str | None = None,
) -> TargetSpec:
    """Classify ``path`` (Project JSON file or saved run directory) and validate flag pairings."""
    target = Path(path).resolve()
    if not target.exists():
        raise SnapshotError("INPUT_NOT_FOUND", f"input path does not exist: {target.as_posix()}", path=target.as_posix())
    if target.is_dir():
        if run is not None:
            raise SnapshotError("USAGE", "--run can only be used when PATH is a Project JSON file")
        project_path = Path(project).resolve() if project is not None else None
        if project_path is not None and not project_path.is_file():
            raise SnapshotError("PROJECT_NOT_FOUND", f"--project file not found: {project_path.as_posix()}",
                                path=project_path.as_posix())
        return TargetSpec(
            kind="run",
            input_path=target,
            project_path=project_path,
            run_dir=target,
            configuration_id=configuration,
        )
    if target.is_file():
        if project is not None:
            raise SnapshotError("USAGE", "--project can only be used when PATH is a run directory")
        run_dir = Path(run).resolve() if run is not None else None
        if run_dir is not None and not run_dir.exists():
            raise SnapshotError("RUN_NOT_FOUND", f"--run directory not found: {run_dir.as_posix()}",
                                path=run_dir.as_posix())
        if run_dir is not None and not run_dir.is_dir():
            raise SnapshotError("RUN_NOT_DIRECTORY", f"--run must be a saved run directory: {run_dir.as_posix()}",
                                path=run_dir.as_posix())
        return TargetSpec(
            kind="project",
            input_path=target,
            project_path=target,
            run_dir=run_dir,
            configuration_id=configuration,
        )
    raise SnapshotError("INPUT_UNSUPPORTED", f"input path is neither a file nor a directory: {target.as_posix()}",
                        path=target.as_posix())


def _resolve_configuration_dir(run_dir: Path, configuration_id: str | None) -> tuple[Path, str | None, list[str]]:
    """Resolve an ordinary run directory or one configuration inside a configured run directory."""
    configs_root = run_dir / "configurations"
    if not configs_root.is_dir():
        if configuration_id is not None:
            raise SnapshotError("CONFIGURATION_NOT_FOUND",
                                f"run directory has no configurations/: {run_dir.as_posix()}",
                                configuration_id=configuration_id)
        return run_dir, None, []
    available = sorted(child.name for child in configs_root.iterdir() if child.is_dir())
    if not available:
        raise SnapshotTransientError("RUN_INCOMPLETE", f"configurations/ has no completed runs yet: {run_dir.as_posix()}")
    selected = configuration_id if configuration_id is not None else available[0]
    if selected not in available:
        raise SnapshotError("CONFIGURATION_NOT_FOUND",
                            f"unknown configuration {selected!r}; available: {', '.join(available)}",
                            configuration_id=selected, available_configurations=available)
    return configs_root / selected, selected, available


def _hash_file_entry(hasher: Any, label: str, path: Path) -> None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        hasher.update(f"MISSING:{label}\0".encode("utf-8"))
        return
    except OSError as exc:
        raise SnapshotTransientError("FILE_READ_FAILED", f"could not read {path.name}: {exc}", path=path.as_posix()) from None
    digest = hashlib.sha256(data).hexdigest()
    hasher.update(f"FILE:{label}:{len(data)}:{digest}\0".encode("utf-8"))


def compute_fingerprint(spec: TargetSpec) -> str:
    """Compute a deterministic content fingerprint across all watched input files."""
    hasher = hashlib.sha256()
    hasher.update(f"KIND:{spec.kind}:{spec.configuration_id or ''}\0".encode("utf-8"))
    if spec.project_path is not None:
        if not spec.project_path.exists():
            raise SnapshotTransientError("PROJECT_MISSING", f"waiting for project file: {spec.project_path.as_posix()}",
                                         path=spec.project_path.as_posix())
        _hash_file_entry(hasher, "project", spec.project_path)
    if spec.run_dir is not None:
        if not spec.run_dir.exists():
            raise SnapshotTransientError("RUN_MISSING", f"waiting for run directory: {spec.run_dir.as_posix()}",
                                         path=spec.run_dir.as_posix())
        effective_run, selected_cfg, available = _resolve_configuration_dir(spec.run_dir, spec.configuration_id)
        hasher.update(f"CFG:{selected_cfg or ''}:{','.join(available)}\0".encode("utf-8"))
        if selected_cfg is not None:
            _hash_file_entry(hasher, "root/manifest.json", spec.run_dir / "manifest.json")
        for filename in WATCHED_RUN_FILES:
            _hash_file_entry(hasher, f"run/{filename}", effective_run / filename)
    return hasher.hexdigest()


def _read_project_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise SnapshotTransientError("PROJECT_MISSING", f"project file is not present: {path.as_posix()}",
                                     path=path.as_posix()) from None
    except OSError as exc:
        raise SnapshotTransientError("PROJECT_UNREADABLE", f"could not read project file: {exc}",
                                     path=path.as_posix()) from None
    if not raw.strip():
        raise SnapshotTransientError("PROJECT_EMPTY", f"project file is currently empty (write in progress?): {path.as_posix()}",
                                     path=path.as_posix())
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SnapshotTransientError("PROJECT_JSON_INVALID", f"project file is not valid JSON (write in progress?): {exc}",
                                     path=path.as_posix()) from None
    if not isinstance(document, dict):
        raise SnapshotTransientError("PROJECT_INVALID", "project JSON root must be an object", path=path.as_posix())
    if document.get("schema") != "opensci.project":
        raise SnapshotTransientError(
            "PROJECT_SCHEMA_UNSUPPORTED",
            f"expected opensci.project JSON document, got schema={document.get('schema')!r}",
            path=path.as_posix(),
        )
    return document


def _normalize_quaternion(q: Any) -> tuple[float, float, float, float]:
    if not isinstance(q, (list, tuple)) or len(q) != 4:
        return (1.0, 0.0, 0.0, 0.0)
    vals = [float(v) for v in q]
    norm = math.sqrt(sum(v * v for v in vals))
    if norm < 1e-12:
        return (1.0, 0.0, 0.0, 0.0)
    return (vals[0] / norm, vals[1] / norm, vals[2] / norm, vals[3] / norm)


def _quat_mul(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return _normalize_quaternion((
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    ))


def _rotate_vec(q: tuple[float, float, float, float], v: tuple[float, float, float]) -> tuple[float, float, float]:
    w, x, y, z = q
    vx, vy, vz = v
    tx = 2.0 * (y * vz - z * vy)
    ty = 2.0 * (z * vx - x * vz)
    tz = 2.0 * (x * vy - y * vx)
    return (
        vx + w * tx + (y * tz - z * ty),
        vy + w * ty + (z * tx - x * tz),
        vz + w * tz + (x * ty - y * tx),
    )


def _cylinder_mesh(
    radius: float,
    z_min: float,
    z_max: float,
    pos: tuple[float, float, float],
    quat: tuple[float, float, float, float],
    rgb: tuple[float, float, float],
    alpha: float,
    *,
    segments: int = 24,
) -> Any:
    import numpy as np
    from os_optics.viewer.model import ViewMesh, place

    r = max(float(radius), 0.5)
    z0, z1 = float(z_min), float(z_max)
    if abs(z1 - z0) < 0.2:
        mid = 0.5 * (z0 + z1)
        z0, z1 = mid - 0.25, mid + 0.25
    angles = np.linspace(0.0, 2.0 * np.pi, segments, endpoint=False)
    pts = []
    for z in (z0, z1):
        for angle in angles:
            pts.append([r * math.cos(float(angle)), r * math.sin(float(angle)), z])
    pts.append([0.0, 0.0, z0])
    pts.append([0.0, 0.0, z1])
    local = np.asarray(pts, dtype=float)
    c0, c1 = 2 * segments, 2 * segments + 1
    tris = []
    for i in range(segments):
        j = (i + 1) % segments
        tris.append([c0, j, i])
        tris.append([c1, segments + i, segments + j])
        tris.append([i, j, segments + j])
        tris.append([i, segments + j, segments + i])
    world = place(local, pos, quat)
    return ViewMesh(positions=world, triangles=np.asarray(tris, dtype=np.int64), rgb=rgb, alpha=alpha, material="preview")


def _box_mesh(
    hx: float,
    hy: float,
    z_min: float,
    z_max: float,
    pos: tuple[float, float, float],
    quat: tuple[float, float, float, float],
    rgb: tuple[float, float, float],
    alpha: float,
) -> Any:
    import numpy as np
    from os_optics.viewer.model import ViewMesh, place

    dx, dy = max(float(hx), 0.5), max(float(hy), 0.5)
    z0, z1 = float(z_min), float(z_max)
    if abs(z1 - z0) < 0.2:
        mid = 0.5 * (z0 + z1)
        z0, z1 = mid - 0.25, mid + 0.25
    local = np.asarray([
        [-dx, -dy, z0], [dx, -dy, z0], [dx, dy, z0], [-dx, dy, z0],
        [-dx, -dy, z1], [dx, -dy, z1], [dx, dy, z1], [-dx, dy, z1],
    ], dtype=float)
    tris = np.asarray([
        [0, 2, 1], [0, 3, 2],
        [4, 5, 6], [4, 6, 7],
        [0, 1, 5], [0, 5, 4],
        [1, 2, 6], [1, 6, 5],
        [2, 3, 7], [2, 7, 6],
        [3, 0, 4], [3, 4, 7],
    ], dtype=np.int64)
    world = place(local, pos, quat)
    return ViewMesh(positions=world, triangles=tris, rgb=rgb, alpha=alpha, material="preview")


def _float_or(value: Any, default: float) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _rgb3(values: Any) -> tuple[float, float, float]:
    return (float(values[0]), float(values[1]), float(values[2]))


def _build_project_preview_scene(
    document: dict[str, Any],
    *,
    project_hash: str | None,
    title: str,
    note_code: str,
    note_text: str,
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    """Build a geometry-only ``ViewerScene`` directly from a Project document (zero ray tracing)."""
    import numpy as np
    from os_optics.viewer.model import OPTICAL_VIEW, ViewPart, ViewerScene, scene_summary, view_document
    from os_optics.viewer.palette import class_table

    palette = class_table(OPTICAL_VIEW)
    models = {m.get("model_id"): m for m in (document.get("optical_models") or []) if isinstance(m, dict)}
    target_obs_ids = {t.get("observation_id") for t in (document.get("targets") or []) if isinstance(t, dict)}
    component_poses: dict[str, tuple[tuple[float, float, float], tuple[float, float, float, float]]] = {}
    parts: list[Any] = []

    for comp in document.get("components") or []:
        if not isinstance(comp, dict) or not comp.get("instance_id"):
            continue
        inst_id = str(comp["instance_id"])
        role = str(comp.get("optical_role") or "other_optical").lower()
        part_class = ROLE_TO_PART_CLASS.get(role, "OTHER_OPTICAL")
        style = palette.get(part_class) or palette["OTHER_OPTICAL"]
        pose = comp.get("nominal_pose") or {}
        pos_raw = pose.get("position_mm") or [0.0, 0.0, 0.0]
        pos = (float(pos_raw[0]), float(pos_raw[1]), float(pos_raw[2]))
        quat = _normalize_quaternion(pose.get("quaternion_wxyz"))
        component_poses[inst_id] = (pos, quat)

        model_entry = models.get(comp.get("optical_model_id")) if comp.get("optical_model_id") else None
        design_geom = (model_entry or {}).get("design_geometry") or {}
        model_spec = (model_entry or {}).get("model") or {}
        geom_kind = str(design_geom.get("kind") or "").upper()

        if geom_kind == "BOX":
            hx = 0.5 * _float_or(design_geom.get("width_x_mm") or design_geom.get("x_mm"), 20.0)
            hy = 0.5 * _float_or(design_geom.get("height_y_mm") or design_geom.get("y_mm"), 20.0)
            z0 = _float_or(design_geom.get("z_min_mm"), -2.0)
            z1 = _float_or(design_geom.get("z_max_mm"), 2.0)
            mesh = _box_mesh(hx, hy, z0, z1, pos, quat, _rgb3(style.rgb), float(style.alpha))
        elif geom_kind == "CYLINDER":
            radius = 0.5 * _float_or(design_geom.get("diameter_mm"), 25.4)
            z0 = _float_or(design_geom.get("z_min_mm"), -2.0)
            z1 = _float_or(design_geom.get("z_max_mm"), 2.0)
            mesh = _cylinder_mesh(radius, z0, z1, pos, quat, _rgb3(style.rgb), float(style.alpha))
        elif role == "source":
            mesh = _cylinder_mesh(5.0, -8.0, 0.0, pos, quat, _rgb3(style.rgb), 0.92)
        else:
            radius = _float_or(
                model_spec.get("edge_radius_mm")
                or (0.5 * _float_or(model_spec.get("clear_aperture_diameter_mm"), 0.0) if model_spec.get("clear_aperture_diameter_mm") else None)
                or 12.7,
                12.7,
            )
            mesh = _cylinder_mesh(radius, -1.5, 1.5, pos, quat, _rgb3(style.rgb), float(style.alpha))

        product = comp.get("product") or {}
        sku = product.get("optical_sku") if isinstance(product, dict) else None
        label = (
            (model_entry or {}).get("display_name")
            or sku
            or (comp.get("custom_part") or {}).get("description")
            or inst_id
        )
        facts: dict[str, Any] = {
            "optical_role": role,
            "position_mm": [round(pos[0], 6), round(pos[1], 6), round(pos[2], 6)],
            "geometry_preview_only": True,
        }
        if comp.get("optical_model_id"):
            facts["optical_model_id"] = comp["optical_model_id"]
        parts.append(ViewPart(part_id=inst_id, part_class=part_class, label=str(label), sku=sku, meshes=(mesh,), facts=facts))

    for obs in document.get("observations") or []:
        if not isinstance(obs, dict) or not obs.get("observation_id"):
            continue
        obs_id = str(obs["observation_id"])
        is_target = obs_id in target_obs_ids
        part_class = "TARGET_PLANE" if is_target else "OBSERVATION_PLANE"
        style = palette[part_class]
        pose = obs.get("pose") or {}
        pos_raw = pose.get("position_mm") or [0.0, 0.0, 0.0]
        local_pos = (float(pos_raw[0]), float(pos_raw[1]), float(pos_raw[2]))
        local_quat = _normalize_quaternion(pose.get("quaternion_wxyz"))
        device_id = obs.get("device_instance_id")
        if device_id and device_id in component_poses:
            dev_pos, dev_quat = component_poses[device_id]
            rot_local = _rotate_vec(dev_quat, local_pos)
            pos = (dev_pos[0] + rot_local[0], dev_pos[1] + rot_local[1], dev_pos[2] + rot_local[2])
            quat = _quat_mul(dev_quat, local_quat)
        else:
            pos, quat = local_pos, local_quat

        aperture = obs.get("aperture") or {}
        shape = str(aperture.get("shape") or "circle").lower()
        if shape == "rectangle":
            hx = _float_or(aperture.get("half_width_x_mm") or aperture.get("half_width_u_mm"), 10.0)
            hy = _float_or(aperture.get("half_height_y_mm") or aperture.get("half_width_v_mm"), 10.0)
            mesh = _box_mesh(hx, hy, -0.2, 0.2, pos, quat, _rgb3(style.rgb), float(style.alpha))
        else:
            radius = _float_or(aperture.get("radius_mm"), 12.5)
            mesh = _cylinder_mesh(radius, -0.2, 0.2, pos, quat, _rgb3(style.rgb), float(style.alpha))

        target_ids = [t.get("target_id") for t in (document.get("targets") or []) if isinstance(t, dict) and t.get("observation_id") == obs_id]
        facts = {
            "plane_kind": "TARGET" if is_target else "OBSERVATION",
            "absorbing": bool(obs.get("absorbing", False)),
            "target_ids": target_ids,
            "position_mm": [round(pos[0], 6), round(pos[1], 6), round(pos[2], 6)],
            "footprints": [],
            "geometry_preview_only": True,
        }
        parts.append(ViewPart(
            part_id=obs_id,
            part_class=part_class,
            label=("Target plane " if is_target else "Observation plane ") + obs_id,
            sku=None,
            meshes=(mesh,),
            facts=facts,
        ))

    if parts:
        all_pts = np.concatenate([m.positions for p in parts for m in p.meshes])
        lo, hi = all_pts.min(axis=0), all_pts.max(axis=0)
    else:
        lo = np.asarray([-10.0, -10.0, -10.0], dtype=float)
        hi = np.asarray([10.0, 10.0, 10.0], dtype=float)

    scene = ViewerScene(
        view_kind=OPTICAL_VIEW,
        frame="PROJECT_DESIGN_FRAME",
        title=title,
        parts=tuple(parts),
        beams=(),
        markers=(),
        focus_min=lo,
        focus_max=hi,
        bounds_min=lo,
        bounds_max=hi,
        identity={"project_hash": project_hash, "geometry_preview_only": True},
        notes=({"code": note_code, "text": note_text},),
    )
    return scene, view_document(scene), scene_summary(scene)


def _format_power_ledger(power: dict[str, Any] | None, monitors: list[dict[str, Any]]) -> dict[str, Any]:
    """Normalize ``results.power_budget()`` into structured channels, totals, per-source and per-branch entries."""
    if not power or not power.get("available"):
        return {
            "available": False,
            "reason": "POWER_BUDGET_UNAVAILABLE",
            "unit": "W",
            "total_input_W": None,
            "residual_W": None,
            "tolerance_W": None,
            "conserved": None,
            "channels": [],
            "total_sinks": {},
            "lines": [],
            "by_element": [],
            "entries": [],
            "observed_planes": [],
        }
    total_input = power.get("total_input_W")
    total_sinks = dict(power.get("total_sinks") or {})
    channels = []
    for channel, value in sorted(total_sinks.items()):
        fraction = (value / total_input) if (isinstance(total_input, (int, float)) and total_input > 0) else None
        channels.append({
            "channel": channel,
            "power_W": value,
            "unit": "W",
            "fraction_of_input": fraction,
        })
    observed_planes = [
        {
            "observation_id": row["observation_id"],
            "total_power_W": row.get("total_power_W"),
            "unit": "W",
            "quantity_state": row.get("quantity_state"),
            "power_complete": row.get("power_complete"),
            "untraced_power_W": row.get("untraced_power_W"),
            "power_upper_bound_W": row.get("power_upper_bound_W"),
        }
        for row in monitors
    ]
    return {
        "available": True,
        "reason": None,
        "unit": "W",
        "total_input_W": total_input,
        "residual_W": power.get("residual_W"),
        "tolerance_W": power.get("tolerance_W"),
        "conserved": power.get("conserved"),
        "channels": channels,
        "total_sinks": total_sinks,
        "lines": list(power.get("lines") or []),
        "by_element": list(power.get("by_element") or []),
        "entries": list(power.get("entries") or []),
        "observed_planes": observed_planes,
    }


def _unavailable_power_ledger(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "unit": "W",
        "total_input_W": None,
        "residual_W": None,
        "tolerance_W": None,
        "conserved": None,
        "channels": [],
        "total_sinks": {},
        "lines": [],
        "by_element": [],
        "entries": [],
        "observed_planes": [],
    }


def _compact_scene_payload(
    summary: dict[str, Any],
    doc: dict[str, Any],
    *,
    revision: int,
    geometry_only: bool,
    ray_count: int,
) -> dict[str, Any]:
    parts = []
    for part_class, group in (summary.get("parts_by_class") or {}).items():
        for entry in group:
            parts.append({"class": part_class, **entry})
    beams = []
    for beam in summary.get("beams") or []:
        chief_runs = [run for run in (beam.get("runs") or []) if run.get("role") in ("PRIMARY", "SECONDARY", "REFERENCE")]
        beams.append({
            "id": beam.get("id"),
            "wavelength_nm": beam.get("wavelength_nm"),
            "display_rgb": beam.get("display_rgb"),
            "source_element_id": beam.get("source_element_id"),
            "parent_path_id": beam.get("parent_path_id"),
            "termination": beam.get("termination"),
            "final_power_W": beam.get("final_power_W"),
            "display_role": beam.get("display_role"),
            "run_count": len(beam.get("runs") or []),
            "chief_runs": chief_runs,
        })
    return {
        "available": True,
        "geometry_only": geometry_only,
        "view_kind": "PROJECT_GEOMETRY_PREVIEW" if geometry_only else doc.get("view_kind", "OPTICAL_SIMULATION_VIEW"),
        "title": doc.get("title"),
        "frame": doc.get("frame"),
        "units": summary.get("units", {"length": "mm"}),
        "axes": summary.get("axes"),
        "scene_content_hash": (doc.get("identity") or {}).get("scene_content_hash"),
        "bounds": doc.get("bounds"),
        "focus_bounds": doc.get("focus_bounds"),
        "main_axis": doc.get("main_axis"),
        "camera_presets": list((doc.get("camera_presets") or {}).keys()),
        "part_count": len(parts),
        "beam_count": len(beams),
        "ray_count": ray_count,
        "classes": summary.get("classes") or [],
        "parts": parts,
        "beams": beams,
        "notes": doc.get("notes") or [],
        "document_url": f"/api/scene-document?rev={revision}",
        "engine_view_url": f"/engine-view/?rev={revision}",
    }


def _merge_branches(paths: list[dict[str, Any]], scene_beams: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {b.get("id"): b for b in scene_beams}
    branches = []
    for path in paths:
        beam = by_id.get(path.get("path_id"), {})
        branches.append({
            "path_id": path.get("path_id"),
            "source_id": path.get("source_id"),
            "spectral_index": path.get("spectral_index"),
            "wavelength_nm": path.get("wavelength_nm"),
            "parent_path_id": path.get("parent_path_id"),
            "branch_surface_id": path.get("branch_surface_id"),
            "branch_kind": path.get("branch_kind"),
            "children": list(path.get("children") or []),
            "depth": path.get("depth"),
            "segment_count": path.get("segment_count"),
            "power_start_W": path.get("power_start_W"),
            "final_power_W": path.get("final_power_W"),
            "unit": "W",
            "termination": path.get("termination"),
            "termination_surface_id": path.get("termination_surface_id"),
            "geometric_path_length_mm": path.get("geometric_path_length_mm"),
            "optical_path_length_mm": path.get("optical_path_length_mm"),
            "observations": list(path.get("observations") or []),
            "lineage": list(path.get("lineage") or []),
            "display_role": beam.get("display_role"),
            "display_rgb": beam.get("display_rgb"),
        })
    return branches


def _uncomputed_targets(document: dict[str, Any], status_label: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for raw in document.get("targets") or []:
        if not isinstance(raw, dict):
            continue
        bounds = {
            k: raw[k]
            for k in ("minimum", "maximum", "target_value", "tolerance", "tolerance_abs", "tolerance_rel")
            if raw.get(k) is not None
        }
        targets.append({
            "target_id": raw.get("target_id"),
            "metric": raw.get("metric"),
            "observation_id": raw.get("observation_id"),
            "status": status_label,
            "assessment": None,
            "value": None,
            "unit": raw.get("unit"),
            "bounds": bounds,
            "error": None,
            "message": "Not computed for this Project revision.",
            "issue_codes": [],
            "applicability_issue_codes": [],
            "quantity_state": "UNCOMPUTED",
            "observation_power_state": "UNCOMPUTED",
            "power_complete": False,
        })
    summary = {
        "declared": bool(targets),
        "counts": {status_label: len(targets)} if targets else {},
        "assessments": {},
        "met": [],
        "unmet": [],
        "indeterminate": [],
        "not_applicable": [],
        "requirement_outcome": status_label,
    }
    return targets, summary


def _uncomputed_quantities(document: dict[str, Any], status_label: str) -> list[dict[str, Any]]:
    quantities: list[dict[str, Any]] = []
    for obs in document.get("observations") or []:
        if not isinstance(obs, dict):
            continue
        quantities.append({
            "observation_id": obs.get("observation_id"),
            "status": status_label,
            "quantity_state": "UNCOMPUTED",
            "total_power_W": None,
            "power_complete": False,
            "untraced_power_W": None,
            "power_upper_bound_W": None,
            "arrival_count": 0,
            "outside_active_area_count": 0,
            "superposition_count": 0,
            "arrivals": [],
            "power_by_wavelength": [],
            "issue_codes": [],
            "plane": {
                "origin_mm": (obs.get("pose") or {}).get("position_mm"),
                "aperture": obs.get("aperture"),
                "absorbing": bool(obs.get("absorbing", False)),
                "device_id": obs.get("device_instance_id"),
                "medium": obs.get("medium"),
            },
        })
    return quantities


def _project_check_summary(check_reply: dict[str, Any]) -> dict[str, Any]:
    if check_reply.get("ok"):
        return {
            "checked": True,
            "ok": True,
            "project_hash": check_reply.get("project_hash"),
            "engine_input_hash": check_reply.get("engine_input_hash"),
            "problems": [],
            "advisories": list(check_reply.get("advisories") or []),
        }
    error = check_reply.get("error") or {}
    return {
        "checked": True,
        "ok": False,
        "project_hash": check_reply.get("project_hash"),
        "engine_input_hash": None,
        "error_code": error.get("code"),
        "problems": list(error.get("problems") or []),
        "advisories": list(check_reply.get("advisories") or []),
    }


def _normalize_freshness(raw: dict[str, Any]) -> dict[str, Any]:
    run_info = raw.get("run") or {}
    current_info = raw.get("current") or {}
    return {
        "checked": True,
        "state": raw.get("freshness", "UNDETERMINED"),
        "reasons": list(raw.get("reasons") or []),
        "engine_build_matches": raw.get("engine_build_matches"),
        "run_project_hash": run_info.get("project_hash"),
        "current_project_hash": current_info.get("project_hash"),
        "run_engine_input_hash": run_info.get("engine_input_hash"),
        "current_engine_input_hash": current_info.get("engine_input_hash"),
        "integrity": raw.get("integrity"),
    }


def _build_computed_run_snapshot(
    spec: TargetSpec,
    effective_run: Path,
    *,
    selected_cfg: str | None,
    available_cfgs: list[str],
    revision: int,
    fingerprint: str,
    freshness_info: dict[str, Any],
    project_check: dict[str, Any] | None,
    expected_project_hash: str | None,
) -> BuiltSnapshot:
    from os_optics.results import load_run
    from os_optics.viewer import ViewerInputError, optical_view_scene, scene_summary, view_document

    try:
        results = load_run(effective_run, verify=True)
    except Exception as exc:  # noqa: BLE001 - transient mid-write or unreadable run
        raise SnapshotTransientError("RUN_UNREADABLE", f"run directory is not readable yet: {exc}",
                                     run_dir=effective_run.as_posix()) from None

    try:
        viewer_scene = optical_view_scene(effective_run)
    except ViewerInputError as exc:
        raise SnapshotTransientError("SCENE_UNREADABLE", str(exc), run_dir=effective_run.as_posix()) from None
    except Exception as exc:  # noqa: BLE001
        raise SnapshotTransientError("SCENE_UNREADABLE", f"optical scene is not readable yet: {exc}",
                                     run_dir=effective_run.as_posix()) from None

    identity = results.identity()
    scene_identity = viewer_scene.identity or {}

    # Cross-bind check: never combine numbers from one run with 3D geometry from another run.
    if scene_identity.get("project_hash") and identity.get("project_hash"):
        if scene_identity["project_hash"] != identity["project_hash"]:
            raise SnapshotTransientError(
                "RUN_INCONSISTENT_SNAPSHOT",
                "optical scene project_hash does not match simulation results project_hash (mid-write or mixed run)",
            )
    if scene_identity.get("engine_input_hash") and identity.get("engine_input_hash"):
        if scene_identity["engine_input_hash"] != identity["engine_input_hash"]:
            raise SnapshotTransientError(
                "RUN_INCONSISTENT_SNAPSHOT",
                "optical scene engine_input_hash does not match simulation results engine_input_hash",
            )
    if expected_project_hash is not None and identity.get("project_hash") != expected_project_hash:
        raise SnapshotTransientError(
            "RUN_PROJECT_HASH_MISMATCH",
            "run project_hash changed relative to the checked Project during load",
        )

    artifacts = {"run_directory": effective_run, "manifest": effective_run / "manifest.json"}
    artifacts.update({item["role"]: effective_run / item["name"] for item in results.describe().get("files") or []})
    kit_summary = summarize_results(results, artifacts=artifacts, details=True)
    compact_targets = results.targets()
    monitors = results.monitors()
    paths = results.paths()
    power = results.power_budget()
    ray_count = len(results.rays()) if results.has_ray_data else 0

    scene_doc = view_document(viewer_scene)
    scene_sum = scene_summary(viewer_scene)
    scene_payload = _compact_scene_payload(scene_sum, scene_doc, revision=revision, geometry_only=False, ray_count=ray_count)
    power_ledger = _format_power_ledger(power, monitors)
    branches = _merge_branches(paths, scene_payload["beams"])

    execution_raw = (compact_targets.get("outcome") or {}).get("execution") or {}
    execution = {
        **execution_raw,
        "status": kit_summary["status"],
        "engine_status": kit_summary["engine_status"],
        "validation_status": kit_summary["validation_status"],
        "requirement_outcome": kit_summary["requirement_outcome"],
        "in_model_scope": kit_summary["in_model_scope"],
        "out_of_model_issues": kit_summary["out_of_model_issues"],
        "model_scope_issues": kit_summary["model_scope_issues"],
        "issues": kit_summary["issues"],
    }

    target_rows = [
        {"target_id": row["id"], **{k: v for k, v in row.items() if k != "id"}}
        for row in kit_summary["targets"]
    ]
    req_outcome = (compact_targets.get("outcome") or {}).get("requirements") or {}
    target_summary = {
        "declared": bool(req_outcome.get("declared", bool(target_rows))),
        "counts": dict(compact_targets.get("counts") or {}),
        "assessments": dict(compact_targets.get("assessments") or {}),
        "met": list(req_outcome.get("met") or []),
        "unmet": list(req_outcome.get("unmet") or []),
        "indeterminate": list(req_outcome.get("indeterminate") or []),
        "not_applicable": list(req_outcome.get("not_applicable") or []),
        "requirement_outcome": kit_summary["requirement_outcome"],
    }

    integrity = kit_summary["integrity"]
    notices: list[dict[str, str]] = []
    if execution_raw.get("outcome") == "PARTIAL":
        exhausted = (execution_raw.get("budgets") or {}).get("exhausted") or []
        suffix = f" ({', '.join(exhausted)})" if exhausted else ""
        notices.append({
            "level": "warning",
            "code": "EXECUTION_PARTIAL",
            "message": f"Simulation execution was PARTIAL: a compute budget was reached{suffix}. Some quantities or targets may be bounded or indeterminate.",
        })
    if integrity.get("checked") and not integrity.get("consistent"):
        notices.append({
            "level": "error",
            "code": "RUN_INTEGRITY_FAILED",
            "message": "Run files do not match their recorded manifest hashes: " + "; ".join(integrity.get("problems") or []),
        })
    if not kit_summary["in_model_scope"]:
        notices.append({
            "level": "warning",
            "code": "OUT_OF_MODEL_SCOPE",
            "message": "One or more elements or targets fall outside the declared optical model scope.",
        })
    if spec.evaluation_mode == "explicit_local_temp":
        notices.append({
            "level": "info",
            "code": "EXPLICIT_LOCAL_EVALUATION",
            "message": "Computed once via explicit --evaluate into a temporary directory; the input Project file was not modified.",
        })

    snapshot_id = f"rev-{revision}-{fingerprint[:12]}"
    snapshot = json_safe({
        "schema": SNAPSHOT_SCHEMA,
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "revision": revision,
        "snapshot_id": snapshot_id,
        "generated_at": _utc_now(),
        "result_mode": "computed_run",
        "is_computed_result": True,
        "source": {
            "kind": spec.kind,
            "input_path": spec.input_path.as_posix(),
            "project_path": spec.project_path.as_posix() if spec.project_path is not None else None,
            "run_dir": effective_run.as_posix(),
            "configuration_id": selected_cfg,
            "available_configurations": available_cfgs,
            "read_only": True,
            "evaluation": {
                "mode": spec.evaluation_mode,
                "input_modified": False,
                "stale_after_edit": False,
            },
            "fingerprint": fingerprint,
        },
        "identity": {
            "engine_version": identity.get("engine_version") or engine_version(),
            "engine_build_id": identity.get("engine_build_id"),
            "project_hash": identity.get("project_hash"),
            "engine_input_hash": identity.get("engine_input_hash") or identity.get("input_hash"),
            "result_hash": identity.get("result_hash"),
            "trace_hash": identity.get("trace_hash"),
            "scene_content_hash": scene_identity.get("scene_content_hash"),
            "catalog_version": identity.get("catalog_version"),
            "kit_version": KIT_VERSION,
        },
        "freshness": freshness_info,
        "integrity": integrity,
        "execution": execution,
        "power_ledger": power_ledger,
        "branches": branches,
        "targets": target_rows,
        "target_summary": target_summary,
        "quantities": monitors,
        "scene": scene_payload,
        "project_check": project_check,
        "historical_run": None,
        "element_models": kit_summary["element_models"],
        "notices": notices,
    })
    return BuiltSnapshot(snapshot=snapshot, scene_document=json_safe(scene_doc), fingerprint=fingerprint)


def _build_preview_or_stale_snapshot(
    spec: TargetSpec,
    document: dict[str, Any],
    *,
    check_info: dict[str, Any],
    result_mode: str,
    revision: int,
    fingerprint: str,
    freshness_info: dict[str, Any],
    historical_run: dict[str, Any] | None,
    effective_run: Path | None,
    selected_cfg: str | None,
    available_cfgs: list[str],
) -> BuiltSnapshot:
    assert result_mode in ("geometry_preview", "stale_run")
    status_label = "STALE_RUN" if result_mode == "stale_run" else "NOT_COMPUTED"
    project_hash = check_info.get("project_hash")

    if result_mode == "stale_run":
        reasons = ", ".join(freshness_info.get("reasons") or []) or "PROJECT_CHANGED"
        title = "Project Geometry Preview (Run Is Historical)"
        note_code = "STALE_RUN_MISMATCH"
        note_text = (
            f"The attached run is historical relative to the current Project ({reasons}). "
            "Showing current Project geometry only; old run numbers and rays are withheld to avoid mismatch."
        )
    else:
        title = "Project Geometry Preview (Uncomputed)"
        note_code = "GEOMETRY_PREVIEW_ONLY"
        note_text = (
            "No saved simulation run is attached. Showing nominal Project geometry only — "
            "this is NOT a computed optical result."
        )

    _scene, scene_doc, scene_sum = _build_project_preview_scene(
        document,
        project_hash=project_hash,
        title=title,
        note_code=note_code,
        note_text=note_text,
    )
    scene_payload = _compact_scene_payload(scene_sum, scene_doc, revision=revision, geometry_only=True, ray_count=0)
    targets, target_summary = _uncomputed_targets(document, status_label)
    quantities = _uncomputed_quantities(document, status_label)

    notices: list[dict[str, str]] = [
        {
            "level": "warning" if result_mode == "stale_run" else "info",
            "code": note_code,
            "message": note_text,
        }
    ]
    if not check_info.get("ok"):
        notices.append({
            "level": "warning",
            "code": check_info.get("error_code") or "PROJECT_CHECK_FAILED",
            "message": "The Project did not pass engine validation checks; inspect project_check.problems.",
        })
    if spec.evaluation_mode == "explicit_local_temp" and result_mode == "stale_run":
        notices.append({
            "level": "info",
            "code": "EVALUATE_NOT_AUTORERUN",
            "message": "Project changed after initial --evaluate run. The viewer never auto-reruns simulations on file edits.",
        })

    element_models = [
        {
            "optical_model_id": m.get("model_id"),
            "display_name": m.get("display_name"),
            "model_kind": (m.get("model") or {}).get("model_kind"),
            "fidelity": (m.get("model") or {}).get("fidelity"),
        }
        for m in (document.get("optical_models") or [])
        if isinstance(m, dict)
    ]

    snapshot_id = f"rev-{revision}-{fingerprint[:12]}"
    snapshot = json_safe({
        "schema": SNAPSHOT_SCHEMA,
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "revision": revision,
        "snapshot_id": snapshot_id,
        "generated_at": _utc_now(),
        "result_mode": result_mode,
        "is_computed_result": False,
        "source": {
            "kind": spec.kind,
            "input_path": spec.input_path.as_posix(),
            "project_path": spec.project_path.as_posix() if spec.project_path is not None else None,
            "run_dir": effective_run.as_posix() if effective_run is not None else None,
            "configuration_id": selected_cfg,
            "available_configurations": available_cfgs,
            "read_only": True,
            "evaluation": {
                "mode": spec.evaluation_mode,
                "input_modified": False,
                "stale_after_edit": spec.evaluation_mode == "explicit_local_temp" and result_mode == "stale_run",
            },
            "fingerprint": fingerprint,
        },
        "identity": {
            "engine_version": engine_version(),
            "engine_build_id": None,
            "project_hash": project_hash,
            "engine_input_hash": check_info.get("engine_input_hash"),
            "result_hash": None,
            "trace_hash": None,
            "scene_content_hash": None,
            "catalog_version": None,
            "kit_version": KIT_VERSION,
        },
        "freshness": freshness_info,
        "integrity": freshness_info.get("integrity") or {"checked": False, "consistent": None, "problems": []},
        "execution": None,
        "power_ledger": _unavailable_power_ledger("STALE_RUN" if result_mode == "stale_run" else "NO_RUN"),
        "branches": [],
        "targets": targets,
        "target_summary": target_summary,
        "quantities": quantities,
        "scene": scene_payload,
        "project_check": check_info,
        "historical_run": historical_run,
        "element_models": element_models,
        "notices": notices,
    })
    return BuiltSnapshot(snapshot=snapshot, scene_document=json_safe(scene_doc), fingerprint=fingerprint)


def build_snapshot(spec: TargetSpec, *, revision: int = 1) -> BuiltSnapshot:
    """Build one atomic, self-consistent read-only snapshot from ``spec``.

    Bracketing the load with ``compute_fingerprint(spec)`` guarantees that a mid-write change
    raises ``SnapshotTransientError`` instead of returning a torn or mixed snapshot.
    """
    require_engine()
    fp_before = compute_fingerprint(spec)

    freshness_info: dict[str, Any]
    if spec.project_path is not None and spec.run_dir is None:
        # Project JSON with no saved run attached -> geometry preview ONLY.
        from os_optics.local import json_api

        document = _read_project_json(spec.project_path)
        check_info = _project_check_summary(json_api.check_project(document))
        freshness_info = {
            "checked": False,
            "state": "NO_RUN",
            "reasons": [],
            "engine_build_matches": None,
            "run_project_hash": None,
            "current_project_hash": check_info.get("project_hash"),
            "run_engine_input_hash": None,
            "current_engine_input_hash": check_info.get("engine_input_hash"),
            "integrity": {"checked": False, "consistent": None, "problems": []},
        }
        built = _build_preview_or_stale_snapshot(
            spec,
            document,
            check_info=check_info,
            result_mode="geometry_preview",
            revision=revision,
            fingerprint=fp_before,
            freshness_info=freshness_info,
            historical_run=None,
            effective_run=None,
            selected_cfg=None,
            available_cfgs=[],
        )
    elif spec.run_dir is not None and spec.project_path is None:
        # Standalone saved run directory.
        effective_run, selected_cfg, available_cfgs = _resolve_configuration_dir(spec.run_dir, spec.configuration_id)
        freshness_info = {
            "checked": False,
            "state": "RUN_ONLY",
            "reasons": [],
            "engine_build_matches": None,
            "run_project_hash": None,
            "current_project_hash": None,
            "run_engine_input_hash": None,
            "current_engine_input_hash": None,
            "note": "Viewing saved run directory directly; pass --project to track freshness against a Project file.",
        }
        built = _build_computed_run_snapshot(
            spec,
            effective_run,
            selected_cfg=selected_cfg,
            available_cfgs=available_cfgs,
            revision=revision,
            fingerprint=fp_before,
            freshness_info=freshness_info,
            project_check=None,
            expected_project_hash=None,
        )
    else:
        # Both Project file and run directory are present: ALWAYS enforce freshness binding!
        assert spec.project_path is not None and spec.run_dir is not None
        from os_optics.local import json_api
        from os_optics.local.freshness import RunNotReadable, run_freshness

        document = _read_project_json(spec.project_path)
        check_info = _project_check_summary(json_api.check_project(document))
        effective_run, selected_cfg, available_cfgs = _resolve_configuration_dir(spec.run_dir, spec.configuration_id)
        try:
            raw_freshness = run_freshness(document, effective_run)
        except RunNotReadable as exc:
            raise SnapshotTransientError("RUN_UNREADABLE", str(exc), run_dir=effective_run.as_posix()) from None
        except Exception as exc:  # noqa: BLE001
            raise SnapshotTransientError("FRESHNESS_UNREADABLE", f"could not evaluate freshness: {exc}") from None

        freshness_info = _normalize_freshness(raw_freshness)
        if freshness_info["state"] == "CURRENT":
            built = _build_computed_run_snapshot(
                spec,
                effective_run,
                selected_cfg=selected_cfg,
                available_cfgs=available_cfgs,
                revision=revision,
                fingerprint=fp_before,
                freshness_info=freshness_info,
                project_check=check_info,
                expected_project_hash=freshness_info.get("current_project_hash"),
            )
        else:
            run_meta = raw_freshness.get("run") or {}
            historical_run = {
                "run_dir": effective_run.as_posix(),
                "project_hash": run_meta.get("project_hash"),
                "engine_input_hash": run_meta.get("engine_input_hash"),
                "engine_build_id": run_meta.get("engine_build_id"),
                "validation_status": run_meta.get("validation_status"),
                "freshness_state": freshness_info["state"],
                "freshness_reasons": freshness_info["reasons"],
            }
            built = _build_preview_or_stale_snapshot(
                spec,
                document,
                check_info=check_info,
                result_mode="stale_run",
                revision=revision,
                fingerprint=fp_before,
                freshness_info=freshness_info,
                historical_run=historical_run,
                effective_run=effective_run,
                selected_cfg=selected_cfg,
                available_cfgs=available_cfgs,
            )

    fp_after = compute_fingerprint(spec)
    if fp_after != fp_before:
        raise SnapshotTransientError(
            "CONCURRENT_MODIFICATION",
            "input files changed while reading the snapshot; waiting for write to complete",
        )
    return built


def run_explicit_local_evaluation(project_path: Path, temp_dir: Path) -> tuple[Path, str | None]:
    """Run ``os_optics.local.run_optical_project`` once into ``temp_dir`` without touching ``project_path``."""
    require_engine()
    from os_optics.local import json_api, run_optical_project

    document = _read_project_json(project_path)
    check = json_api.check_project(document)
    if not check.get("ok"):
        error = check.get("error") or {}
        raise SnapshotError(
            error.get("code") or "PROJECT_INVALID",
            "project failed validation before --evaluate",
            problems=error.get("problems") or [],
        )
    run_dir = temp_dir / "run"
    try:
        run_optical_project(document, output_dir=run_dir)
    except Exception as exc:  # noqa: BLE001
        raise SnapshotError("EVALUATION_FAILED", f"explicit local evaluation failed: {exc}") from None
    return run_dir, check.get("project_hash")
