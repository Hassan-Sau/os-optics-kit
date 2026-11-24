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
WATCHED_ROOT_CONFIG_FILES = (
    "manifest.json",
    "project.json",
    "comparison.json",
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
            for root_file in WATCHED_ROOT_CONFIG_FILES:
                _hash_file_entry(hasher, f"root/{root_file}", spec.run_dir / root_file)
            for cfg_id in available:
                cfg_dir = spec.run_dir / "configurations" / cfg_id
                for filename in WATCHED_RUN_FILES:
                    _hash_file_entry(hasher, f"cfg/{cfg_id}/{filename}", cfg_dir / filename)
        else:
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


def _finite_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _linear_to_srgb(linear: float) -> float:
    c = max(0.0, min(1.0, float(linear)))
    return c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1.0 / 2.4)) - 0.055


def _glb_node_meshes(gltf: dict[str, Any], binary: bytes, node: dict[str, Any]) -> list[Any]:
    """Extract world-space ``ViewMesh`` objects from a glTF element/observation node and its children."""
    import numpy as np
    from os_optics.optical_view import read_accessor
    from os_optics.viewer.model import ViewMesh, place

    pos = node.get("translation", (0.0, 0.0, 0.0))
    rx, ry, rz, rw = node.get("rotation", (0.0, 0.0, 0.0, 1.0))
    q_wxyz = (float(rw), float(rx), float(ry), float(rz))
    no_trans = (0.0, 0.0, 0.0)

    meshes: list[Any] = []
    for child_idx in node.get("children", ()):
        for child_mesh in _glb_node_meshes(gltf, binary, gltf["nodes"][child_idx]):
            rotated_normals = (
                place(child_mesh.normals, no_trans, q_wxyz)
                if child_mesh.normals is not None
                else None
            )
            meshes.append(
                ViewMesh(
                    positions=place(child_mesh.positions, pos, q_wxyz),
                    triangles=child_mesh.triangles,
                    rgb=child_mesh.rgb,
                    alpha=child_mesh.alpha,
                    material=child_mesh.material,
                    surface_id=child_mesh.surface_id,
                    normals=rotated_normals,
                    metallic=child_mesh.metallic,
                    roughness=child_mesh.roughness,
                    transmission=child_mesh.transmission,
                    ior=child_mesh.ior,
                    thickness_mm=child_mesh.thickness_mm,
                    tint=child_mesh.tint,
                )
            )

    if "mesh" not in node:
        return meshes

    extras = node.get("extras") or {}
    surface_id = (
        (extras.get("engine_surface_id") or extras.get("surface_id"))
        if extras.get("kind") == "OPTICAL_SURFACE"
        else None
    )
    coated_names = {
        "filter_face", "splitter_face", "polarization_plane", "polarizer_body",
        "waveplate_body", "thin_lens", "attenuator_plane", "faraday_plane",
        "faraday_body", "grating_face",
    }
    for prim in gltf["meshes"][node["mesh"]]["primitives"]:
        local = np.asarray(read_accessor(gltf, binary, prim["attributes"]["POSITION"]), dtype=float)
        indices = np.asarray(read_accessor(gltf, binary, prim["indices"]), dtype=np.int64).reshape(-1, 3)
        material = gltf["materials"][prim["material"]]
        pbr = material.get("pbrMetallicRoughness") or {}
        factor = pbr.get("baseColorFactor") or (0.7, 0.7, 0.7, 1.0)
        rgb = (
            round(_linear_to_srgb(factor[0]), 4),
            round(_linear_to_srgb(factor[1]), 4),
            round(_linear_to_srgb(factor[2]), 4),
        )
        normals = None
        if "NORMAL" in prim["attributes"]:
            raw_normals = np.asarray(read_accessor(gltf, binary, prim["attributes"]["NORMAL"]), dtype=float)
            normals = place(raw_normals, no_trans, q_wxyz)
        exts = material.get("extensions") or {}
        mat_name = str(material.get("name") or "preview")
        alpha = float(factor[3])
        meshes.append(
            ViewMesh(
                positions=place(local, pos, q_wxyz),
                triangles=indices,
                rgb=rgb,
                alpha=alpha,
                material=mat_name,
                surface_id=surface_id,
                normals=normals,
                metallic=float(pbr.get("metallicFactor", 0.0)),
                roughness=float(pbr.get("roughnessFactor", 0.35)),
                transmission=float((exts.get("KHR_materials_transmission") or {}).get("transmissionFactor", 0.0)),
                ior=(exts.get("KHR_materials_ior") or {}).get("ior"),
                thickness_mm=(exts.get("KHR_materials_volume") or {}).get("thicknessFactor"),
                tint=(0.45 * alpha) if mat_name in coated_names else 0.0,
            )
        )
    return meshes


_SURFACE_FACT_KEYS = (
    "surface_id", "member_id", "role", "radius_mm", "conic", "aspheric_coefficients",
    "linear_sag_coefficient", "cylinder_axis", "vertex_z_mm", "origin_mm", "normal",
    "clear_radius_mm", "clear_half_widths_mm", "aperture_center_mm", "edge_radius_mm",
    "edge_half_widths_mm", "coating_kind", "medium_before", "medium_after",
    "solid_medium", "placement", "obstruction_radius_mm", "focal_length_mm",
)


def _glb_surface_facts(gltf: dict[str, Any], node: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    out: list[dict[str, Any]] = []
    for child_idx in node.get("children", ()):
        child = gltf["nodes"][child_idx]
        extras = child.get("extras") or {}
        if extras.get("kind") == "OPTICAL_SURFACE":
            facts = {k: extras[k] for k in _SURFACE_FACT_KEYS if k in extras}
            facts["surface_id"] = extras.get("engine_surface_id") or extras.get("surface_id")
            out.append(facts)
        out.extend(_glb_surface_facts(gltf, child))
    return tuple(out)


def _geometry_provenance_from_engine(geom: dict[str, Any] | Any) -> tuple[str, bool]:
    """Classify an engine geometry descriptor into ``(geometry_source, schematic)``."""
    if isinstance(geom, dict):
        basis = geom.get("basis")
        status = geom.get("status")
        kind = geom.get("kind")
    else:
        raw_basis = getattr(geom, "basis", None)
        basis = getattr(raw_basis, "value", raw_basis)
        raw_status = getattr(geom, "status", None)
        status = getattr(raw_status, "value", raw_status)
        raw_kind = getattr(geom, "kind", None)
        kind = getattr(raw_kind, "value", raw_kind)

    if basis == "PUBLIC_CATALOG_DESIGN_GEOMETRY" and status == "NOMINAL":
        return "catalog", False
    if basis == "PROJECT_DECLARED_MODEL" and status == "NOMINAL":
        return "engine_nominal", False
    if kind == "SOURCE_EMITTER":
        return "engine_nominal", False
    if status == "PLACEHOLDER" or basis == "VIEW_PLACEHOLDER":
        return "schematic", True
    return "engine_nominal", False


def _declared_configurations(document: dict[str, Any]) -> list[str]:
    return [
        str(cfg["configuration_id"])
        for cfg in (document.get("configurations") or [])
        if isinstance(cfg, dict) and cfg.get("configuration_id")
    ]


def _layout_document_for_configuration(document: dict[str, Any], selected_cfg: str | None) -> dict[str, Any]:
    """Return a single-configuration Project payload for ``json_api.project_layout``."""
    declared = _declared_configurations(document)
    if not declared:
        return document
    cfg_id = selected_cfg if selected_cfg in declared else declared[0]
    from os_optics.project_document import parse_project_document

    return parse_project_document(document).configured(cfg_id).payload()


def _preview_scene_content_hash(project_hash: str | None, title: str, parts: list[Any]) -> str:
    payload = {
        "project_hash": project_hash,
        "title": title,
        "parts": [
            {
                "id": p.part_id,
                "class": p.part_class,
                "label": p.label,
                "sku": p.sku,
                "facts": p.facts,
                "bounds": [
                    [round(float(v), 6) for v in p.bounds[0]],
                    [round(float(v), 6) for v in p.bounds[1]],
                ],
            }
            for p in parts
        ],
    }
    raw = json.dumps(json_safe(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _build_layout_parts_from_engine(
    document: dict[str, Any],
    *,
    selected_cfg: str | None,
    palette: dict[str, Any],
) -> list[Any] | None:
    """Build exact nominal/catalog ``ViewPart`` meshes via ``os_optics.local.json_api.project_layout`` (zero tracing)."""
    from os_optics.local import json_api
    from os_optics.optical_view import parse_glb
    from os_optics.viewer.model import ViewPart

    try:
        layout_doc = _layout_document_for_configuration(document, selected_cfg)
        reply = json_api.project_layout(layout_doc)
    except Exception:  # noqa: BLE001 - fall back to schematic/design_geometry builder when layout cannot lower
        return None
    if not reply.get("ok"):
        return None

    layout = reply.get("layout") or {}
    glb_bytes = (reply.get("files") or {}).get("optical_layout.glb")
    if not isinstance(glb_bytes, (bytes, bytearray)):
        return None

    try:
        gltf, binary = parse_glb(bytes(glb_bytes))
    except Exception:  # noqa: BLE001
        return None

    elements_by_id = {
        e["element_id"]: e
        for e in (layout.get("elements") or [])
        if isinstance(e, dict) and e.get("element_id")
    }
    planes_by_id = {
        p["observation_id"]: p
        for p in (layout.get("observation_planes") or [])
        if isinstance(p, dict) and p.get("observation_id")
    }

    parts: list[Any] = []
    for node in gltf.get("nodes") or []:
        extras = node.get("extras") or {}
        name = str(node.get("name") or "")
        if name.startswith("element:") and extras.get("element_id") in elements_by_id:
            elem = elements_by_id[extras["element_id"]]
            geom = elem.get("geometry") or {}
            geom_source, is_schematic = _geometry_provenance_from_engine(geom)
            part_class = str(extras.get("part_class") or ROLE_TO_PART_CLASS.get(str(elem.get("optical_role") or "").lower(), "OTHER_OPTICAL"))
            if part_class == "OTHER_OPTICAL":
                mk = geom.get("model_kind")
                if mk == "APERTURE_STOP" or str(elem.get("optical_role") or "").lower() == "aperture":
                    part_class = "APERTURE"
                elif mk in ("IDEAL_THIN_LENS", "TABULATED_THIN_LENS"):
                    part_class = "LENS"
                elif mk == "NEUTRAL_DENSITY":
                    part_class = "FILTER"
            if part_class not in palette:
                part_class = "OTHER_OPTICAL"

            pose = elem.get("pose") or {}
            pos_raw = pose.get("position_mm") or [0.0, 0.0, 0.0]
            facts: dict[str, Any] = {
                "optical_role": elem.get("optical_role"),
                "position_mm": [round(float(pos_raw[0]), 6), round(float(pos_raw[1]), 6), round(float(pos_raw[2]), 6)],
                "geometry_preview_only": True,
                "geometry_source": geom_source,
                "schematic": is_schematic,
            }
            if elem.get("optical_model_id") is not None:
                facts["optical_model_id"] = elem["optical_model_id"]
            if geom.get("category") is not None:
                facts["category"] = geom["category"]
            if geom.get("form") is not None:
                facts["form"] = geom["form"]
            if geom.get("diameter_mm") is not None:
                facts["diameter_mm"] = float(geom["diameter_mm"])
            if geom.get("z_min_mm") is not None and geom.get("z_max_mm") is not None:
                facts["thickness_mm"] = round(abs(float(geom["z_max_mm"]) - float(geom["z_min_mm"])), 6)
            if geom.get("half_extents_mm") is not None:
                facts["size_mm"] = [round(2.0 * float(h), 6) for h in geom["half_extents_mm"]]

            meshes = tuple(_glb_node_meshes(gltf, binary, node))
            if not meshes:
                continue
            parts.append(
                ViewPart(
                    part_id=str(elem["element_id"]),
                    part_class=part_class,
                    label=str(elem.get("label") or elem["element_id"]),
                    sku=elem.get("optical_sku"),
                    meshes=meshes,
                    facts=facts,
                    surfaces=_glb_surface_facts(gltf, node),
                )
            )
        elif name.startswith("observation:") and extras.get("observation_id") in planes_by_id:
            plane = planes_by_id[extras["observation_id"]]
            is_target = str(plane.get("kind") or "").upper() == "TARGET"
            part_class = "TARGET_PLANE" if is_target else "OBSERVATION_PLANE"
            pose = plane.get("pose") or {}
            pos_raw = pose.get("position_mm") or [0.0, 0.0, 0.0]
            facts = {
                "plane_kind": "TARGET" if is_target else "OBSERVATION",
                "absorbing_extent": plane.get("absorbing_extent"),
                "target_ids": list(plane.get("target_ids") or []),
                "position_mm": [round(float(pos_raw[0]), 6), round(float(pos_raw[1]), 6), round(float(pos_raw[2]), 6)],
                "footprints": [],
                "geometry_preview_only": True,
                "geometry_source": "engine_nominal",
                "schematic": False,
            }
            meshes = tuple(_glb_node_meshes(gltf, binary, node))
            if not meshes:
                continue
            obs_id = str(plane["observation_id"])
            parts.append(
                ViewPart(
                    part_id=obs_id,
                    part_class=part_class,
                    label=("Target plane " if is_target else "Observation plane ") + obs_id,
                    sku=None,
                    meshes=meshes,
                    facts=facts,
                )
            )
    return parts


def _build_project_preview_scene(
    document: dict[str, Any],
    *,
    project_hash: str | None,
    title: str,
    note_code: str,
    note_text: str,
    selected_cfg: str | None = None,
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    """Build a geometry-only ``ViewerScene`` directly from a Project document (zero ray tracing).

    Prefers the engine's nominal/catalog layout via ``os_optics.local.json_api.project_layout``.
    When a Project cannot be lowered (e.g. invalid Project or unavailable catalog), falls back
    to explicit ``design_geometry`` where stated and marks any placeholder/unavailable dimensions
    explicitly as ``schematic: True`` (``geometry_source: "schematic"``).
    """
    import numpy as np
    from os_optics.viewer.model import OPTICAL_VIEW, ViewPart, ViewerScene, scene_summary, view_document
    from os_optics.viewer.palette import class_table

    palette = class_table(OPTICAL_VIEW)
    parts = _build_layout_parts_from_engine(document, selected_cfg=selected_cfg, palette=palette)

    if parts is None:
        parts = []
        models = {m.get("model_id"): m for m in (document.get("optical_models") or []) if isinstance(m, dict)}
        target_obs_ids = {t.get("observation_id") for t in (document.get("targets") or []) if isinstance(t, dict)}
        component_poses: dict[str, tuple[tuple[float, float, float], tuple[float, float, float, float]]] = {}

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
            extra_dims: dict[str, Any] = {}

            box_wx = _finite_float(design_geom.get("width_x_mm") or design_geom.get("x_mm"))
            box_hy = _finite_float(design_geom.get("height_y_mm") or design_geom.get("y_mm"))
            geom_z0 = _finite_float(design_geom.get("z_min_mm"))
            geom_z1 = _finite_float(design_geom.get("z_max_mm"))
            cyl_diam = _finite_float(design_geom.get("diameter_mm"))

            if (
                geom_kind == "BOX"
                and box_wx is not None
                and box_hy is not None
                and geom_z0 is not None
                and geom_z1 is not None
            ):
                mesh = _box_mesh(0.5 * box_wx, 0.5 * box_hy, geom_z0, geom_z1, pos, quat, _rgb3(style.rgb), float(style.alpha))
                geom_source, is_schematic = "project_design_geometry", False
                extra_dims["size_mm"] = [round(box_wx, 6), round(box_hy, 6)]
                extra_dims["thickness_mm"] = round(abs(geom_z1 - geom_z0), 6)
            elif (
                geom_kind == "CYLINDER"
                and cyl_diam is not None
                and geom_z0 is not None
                and geom_z1 is not None
            ):
                mesh = _cylinder_mesh(0.5 * cyl_diam, geom_z0, geom_z1, pos, quat, _rgb3(style.rgb), float(style.alpha))
                geom_source, is_schematic = "project_design_geometry", False
                extra_dims["diameter_mm"] = round(cyl_diam, 6)
                extra_dims["thickness_mm"] = round(abs(geom_z1 - geom_z0), 6)
            elif role == "source":
                mesh = _cylinder_mesh(5.0, -8.0, 0.0, pos, quat, _rgb3(style.rgb), 0.92)
                geom_source, is_schematic = "schematic", True
            else:
                radius = _float_or(
                    model_spec.get("edge_radius_mm")
                    or (0.5 * _float_or(model_spec.get("clear_aperture_diameter_mm"), 0.0) if model_spec.get("clear_aperture_diameter_mm") else None)
                    or 12.7,
                    12.7,
                )
                mesh = _cylinder_mesh(radius, -1.5, 1.5, pos, quat, _rgb3(style.rgb), float(style.alpha))
                geom_source, is_schematic = "schematic", True

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
                "geometry_source": geom_source,
                "schematic": is_schematic,
                **extra_dims,
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
                has_dims = _finite_float(aperture.get("half_width_x_mm") or aperture.get("half_width_u_mm")) is not None and _finite_float(
                    aperture.get("half_height_y_mm") or aperture.get("half_width_v_mm")
                ) is not None
                hx = _float_or(aperture.get("half_width_x_mm") or aperture.get("half_width_u_mm"), 10.0)
                hy = _float_or(aperture.get("half_height_y_mm") or aperture.get("half_width_v_mm"), 10.0)
                mesh = _box_mesh(hx, hy, -0.2, 0.2, pos, quat, _rgb3(style.rgb), float(style.alpha))
            else:
                has_dims = _finite_float(aperture.get("radius_mm")) is not None
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
                "geometry_source": "project_design_geometry" if has_dims else "schematic",
                "schematic": not has_dims,
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

    scene_content_hash = _preview_scene_content_hash(project_hash, title, parts)
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
        identity={
            "project_hash": project_hash,
            "scene_content_hash": scene_content_hash,
            "geometry_preview_only": True,
        },
        notes=({"code": note_code, "text": note_text},),
    )
    return scene, view_document(scene), scene_summary(scene)


def _count_drawable_rays(viewer_scene: Any) -> int:
    """Count distinct drawable rays (chief + pupil) present in the 3D scene."""
    source_scene = getattr(viewer_scene, "source_scene", None)
    beam_paths = getattr(source_scene, "beam_paths", None)
    if beam_paths is not None:
        total = 0
        for path in beam_paths:
            for ray in (path.chief_ray, *path.pupil_rays):
                if len(ray.points_mm) >= 2 or ray.display_extension_to_mm is not None:
                    total += 1
        return total
    return sum(max(len(b.runs), len(b.extensions)) for b in getattr(viewer_scene, "beams", ()))


def _computed_part_provenance(viewer_scene: Any) -> dict[str, tuple[str, bool]]:
    """Map element/observation IDs in a computed ``ViewerScene`` to ``(geometry_source, schematic)``."""
    out: dict[str, tuple[str, bool]] = {}
    source_scene = getattr(viewer_scene, "source_scene", None)
    if source_scene is None:
        return out
    for elem in getattr(source_scene, "elements", ()) or ():
        out[str(elem.element_id)] = _geometry_provenance_from_engine(elem.geometry)
    for plane in getattr(source_scene, "observation_planes", ()) or ():
        out[str(plane.observation_id)] = ("engine_nominal", False)
    return out


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
    snapshot_id: str,
    geometry_only: bool,
    branch_count: int,
    drawable_ray_count: int,
    crossing_row_count: int | None,
    crossing_table_available: bool,
    ray_data_mode: str | None,
    part_provenance: dict[str, tuple[str, bool]] | None = None,
) -> dict[str, Any]:
    parts = []
    for part_class, group in (summary.get("parts_by_class") or {}).items():
        for entry in group:
            part_entry = {"class": part_class, **entry}
            if "geometry_source" not in part_entry or "schematic" not in part_entry:
                prov = (part_provenance or {}).get(str(part_entry.get("id") or ""))
                if prov is not None:
                    part_entry.setdefault("geometry_source", prov[0])
                    part_entry.setdefault("schematic", prov[1])
                else:
                    part_entry.setdefault("geometry_source", "engine_nominal")
                    part_entry.setdefault("schematic", False)
            parts.append(part_entry)
    schematic_part_ids = [str(p["id"]) for p in parts if p.get("schematic")]
    geometry_sources = sorted({str(p["geometry_source"]) for p in parts if p.get("geometry_source")})
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
        "branch_count": branch_count,
        "drawable_ray_count": drawable_ray_count,
        "ray_count": drawable_ray_count,
        "crossing_row_count": crossing_row_count,
        "crossing_table_available": crossing_table_available,
        "ray_data_mode": ray_data_mode,
        "has_schematic_geometry": bool(schematic_part_ids),
        "schematic_part_ids": schematic_part_ids,
        "geometry_sources": geometry_sources,
        "classes": summary.get("classes") or [],
        "parts": parts,
        "beams": beams,
        "notes": doc.get("notes") or [],
        "document_url": f"/api/scene-document?snapshot_id={snapshot_id}",
        "engine_view_url": f"/engine-view/{snapshot_id}/",
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
    out: dict[str, Any] = {
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
    if "configurations" in raw:
        out["configuration_ids"] = list(current_info.get("configuration_ids") or [])
        out["run_configuration_ids"] = list(run_info.get("configuration_ids") or [])
    return out


def _raise_if_integrity_failed(integrity: dict[str, Any] | None, *, run_dir: Path) -> None:
    if not isinstance(integrity, dict):
        return
    if integrity.get("checked") and not integrity.get("consistent"):
        raise SnapshotTransientError(
            "RUN_INTEGRITY_FAILED",
            "run files failed manifest integrity verification (incomplete or mixed write)",
            problems=list(integrity.get("problems") or []),
            run_dir=run_dir.as_posix(),
        )


def _build_computed_run_snapshot(
    spec: TargetSpec,
    effective_run: Path,
    *,
    root_run_dir: Path | None = None,
    selected_cfg: str | None,
    available_cfgs: list[str],
    revision: int,
    fingerprint: str,
    freshness_info: dict[str, Any],
    project_check: dict[str, Any] | None,
    expected_project_hash: str | None,
    root_project_hash: str | None = None,
) -> BuiltSnapshot:
    from os_optics.results import ResultsError, load_run
    from os_optics.viewer import ViewerInputError, optical_view_scene, scene_summary, view_document

    try:
        results = load_run(effective_run, verify=True)
    except Exception as exc:  # noqa: BLE001 - transient mid-write or unreadable run
        raise SnapshotTransientError("RUN_UNREADABLE", f"run directory is not readable yet: {exc}",
                                     run_dir=effective_run.as_posix()) from None

    if results.integrity_problems:
        raise SnapshotTransientError(
            "RUN_INTEGRITY_FAILED",
            "run files failed manifest integrity verification (incomplete or mixed write)",
            problems=list(results.integrity_problems),
            run_dir=effective_run.as_posix(),
        )

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

    describe = results.describe()
    artifacts = {"run_directory": effective_run, "manifest": effective_run / "manifest.json"}
    artifacts.update({item["role"]: effective_run / item["name"] for item in describe.get("files") or []})
    kit_summary = summarize_results(results, artifacts=artifacts, details=True)
    integrity = kit_summary["integrity"]
    _raise_if_integrity_failed(integrity, run_dir=effective_run)

    compact_targets = results.targets()
    monitors = results.monitors()
    paths = results.paths()
    power = results.power_budget()

    avail = describe.get("available") or {}
    ray_data_mode = avail.get("ray_data_recording")
    crossing_table_available = False
    crossing_row_count: int | None = None
    if results.has_ray_data and ray_data_mode == "full":
        try:
            crossing_row_count = len(results.rays())
            crossing_table_available = True
        except ResultsError as exc:
            if exc.code in ("PUPIL_RAYS_NOT_RECORDED", "RAY_DATA_NOT_AVAILABLE"):
                crossing_table_available = False
                crossing_row_count = None
            else:
                raise

    snapshot_id = f"rev-{revision}-{fingerprint[:12]}"
    drawable_ray_count = _count_drawable_rays(viewer_scene)
    part_provenance = _computed_part_provenance(viewer_scene)
    scene_doc = view_document(viewer_scene)
    scene_sum = scene_summary(viewer_scene)
    scene_payload = _compact_scene_payload(
        scene_sum,
        scene_doc,
        snapshot_id=snapshot_id,
        geometry_only=False,
        branch_count=len(paths),
        drawable_ray_count=drawable_ray_count,
        crossing_row_count=crossing_row_count,
        crossing_table_available=crossing_table_available,
        ray_data_mode=ray_data_mode,
        part_provenance=part_provenance,
    )
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

    notices: list[dict[str, str]] = []
    if execution_raw.get("outcome") == "PARTIAL":
        exhausted = (execution_raw.get("budgets") or {}).get("exhausted") or []
        suffix = f" ({', '.join(exhausted)})" if exhausted else ""
        notices.append({
            "level": "warning",
            "code": "EXECUTION_PARTIAL",
            "message": f"Simulation execution was PARTIAL: a compute budget was reached{suffix}. Some quantities or targets may be bounded or indeterminate.",
        })
    if not kit_summary["in_model_scope"]:
        notices.append({
            "level": "warning",
            "code": "OUT_OF_MODEL_SCOPE",
            "message": "One or more elements or targets fall outside the declared optical model scope.",
        })
    if ray_data_mode == "summary":
        notices.append({
            "level": "info",
            "code": "RAY_DATA_SUMMARY_ONLY",
            "message": "Run recorded summary ray data (chief rays only); the per-plane crossing table is unavailable.",
        })
    if spec.evaluation_mode == "explicit_local_temp":
        notices.append({
            "level": "info",
            "code": "EXPLICIT_LOCAL_EVALUATION",
            "message": "Computed once via explicit --evaluate into a temporary directory; the input Project file was not modified.",
        })

    identity_doc: dict[str, Any] = {
        "engine_version": identity.get("engine_version") or engine_version(),
        "engine_build_id": identity.get("engine_build_id"),
        "project_hash": identity.get("project_hash"),
        "engine_input_hash": identity.get("engine_input_hash") or identity.get("input_hash"),
        "result_hash": identity.get("result_hash"),
        "trace_hash": identity.get("trace_hash"),
        "scene_content_hash": scene_identity.get("scene_content_hash"),
        "catalog_version": identity.get("catalog_version"),
        "kit_version": KIT_VERSION,
    }
    if root_project_hash is not None:
        identity_doc["root_project_hash"] = root_project_hash

    source_doc: dict[str, Any] = {
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
    }
    if root_run_dir is not None and selected_cfg is not None:
        source_doc["root_run_dir"] = root_run_dir.as_posix()

    snapshot = json_safe({
        "schema": SNAPSHOT_SCHEMA,
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "revision": revision,
        "snapshot_id": snapshot_id,
        "generated_at": _utc_now(),
        "result_mode": "computed_run",
        "is_computed_result": True,
        "source": source_doc,
        "identity": identity_doc,
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
    root_run_dir: Path | None = None,
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

    snapshot_id = f"rev-{revision}-{fingerprint[:12]}"
    _scene, scene_doc, scene_sum = _build_project_preview_scene(
        document,
        project_hash=project_hash,
        title=title,
        note_code=note_code,
        note_text=note_text,
        selected_cfg=selected_cfg,
    )
    scene_payload = _compact_scene_payload(
        scene_sum,
        scene_doc,
        snapshot_id=snapshot_id,
        geometry_only=True,
        branch_count=0,
        drawable_ray_count=0,
        crossing_row_count=None,
        crossing_table_available=False,
        ray_data_mode=None,
    )
    targets, target_summary = _uncomputed_targets(document, status_label)
    quantities = _uncomputed_quantities(document, status_label)

    notices: list[dict[str, str]] = [
        {
            "level": "warning" if result_mode == "stale_run" else "info",
            "code": note_code,
            "message": note_text,
        }
    ]
    if scene_payload["has_schematic_geometry"]:
        schem_list = ", ".join(scene_payload["schematic_part_ids"])
        notices.append({
            "level": "warning",
            "code": "SCHEMATIC_GEOMETRY_USED",
            "message": (
                "Schematic placeholder geometry is used for part(s) without resolved catalog or declared dimensions: "
                f"{schem_list}."
            ),
        })
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

    source_doc: dict[str, Any] = {
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
    }
    if root_run_dir is not None and selected_cfg is not None:
        source_doc["root_run_dir"] = root_run_dir.as_posix()

    snapshot = json_safe({
        "schema": SNAPSHOT_SCHEMA,
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "revision": revision,
        "snapshot_id": snapshot_id,
        "generated_at": _utc_now(),
        "result_mode": result_mode,
        "is_computed_result": False,
        "source": source_doc,
        "identity": {
            "engine_version": engine_version(),
            "engine_build_id": None,
            "project_hash": project_hash,
            "engine_input_hash": check_info.get("engine_input_hash"),
            "result_hash": None,
            "trace_hash": None,
            "scene_content_hash": scene_payload["scene_content_hash"],
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
        declared_cfgs = _declared_configurations(document)
        selected_cfg: str | None = None
        if declared_cfgs:
            selected_cfg = spec.configuration_id if spec.configuration_id is not None else declared_cfgs[0]
            if selected_cfg not in declared_cfgs:
                raise SnapshotError(
                    "CONFIGURATION_NOT_FOUND",
                    f"unknown configuration {selected_cfg!r}; available: {', '.join(declared_cfgs)}",
                    configuration_id=selected_cfg,
                    available_configurations=declared_cfgs,
                )
        elif spec.configuration_id is not None:
            raise SnapshotError(
                "CONFIGURATION_NOT_FOUND",
                f"project declares no configurations: {spec.project_path.as_posix()}",
                configuration_id=spec.configuration_id,
            )

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
            selected_cfg=selected_cfg,
            available_cfgs=declared_cfgs,
        )
    elif spec.run_dir is not None and spec.project_path is None:
        # Standalone saved run directory.
        effective_run, selected_cfg, available_cfgs = _resolve_configuration_dir(spec.run_dir, spec.configuration_id)
        root_project_hash: str | None = None
        if selected_cfg is not None:
            from os_optics.local.configurations import run_reader, verify_configurations_run

            reader = run_reader(spec.run_dir)
            if reader is None:
                raise SnapshotTransientError(
                    "RUN_INCOMPLETE",
                    f"multi-configuration root manifest is missing or unreadable: {spec.run_dir.as_posix()}",
                    run_dir=spec.run_dir.as_posix(),
                )
            root_problems = list(verify_configurations_run(reader))
            if root_problems:
                raise SnapshotTransientError(
                    "RUN_INTEGRITY_FAILED",
                    "run files failed manifest integrity verification (incomplete or mixed write)",
                    problems=root_problems,
                    run_dir=spec.run_dir.as_posix(),
                )
            try:
                root_manifest = json.loads(reader("manifest.json").decode("utf-8"))
                root_project_hash = ((root_manifest.get("project") or {}).get("project_hash") or {}).get("digest")
            except Exception:  # noqa: BLE001
                root_project_hash = None

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
            root_run_dir=spec.run_dir if selected_cfg is not None else None,
            selected_cfg=selected_cfg,
            available_cfgs=available_cfgs,
            revision=revision,
            fingerprint=fp_before,
            freshness_info=freshness_info,
            project_check=None,
            expected_project_hash=None,
            root_project_hash=root_project_hash,
        )
    else:
        # Both Project file and run directory are present: ALWAYS enforce freshness binding!
        assert spec.project_path is not None and spec.run_dir is not None
        from os_optics.local import json_api
        from os_optics.local.freshness import RunNotReadable, run_freshness

        document = _read_project_json(spec.project_path)
        check_info = _project_check_summary(json_api.check_project(document))
        effective_run, selected_cfg, available_cfgs = _resolve_configuration_dir(spec.run_dir, spec.configuration_id)
        is_multi_cfg_run = selected_cfg is not None or (spec.run_dir / "configurations").is_dir()
        freshness_target = spec.run_dir if is_multi_cfg_run else effective_run
        try:
            raw_freshness = run_freshness(document, freshness_target)
        except RunNotReadable as exc:
            raise SnapshotTransientError("RUN_UNREADABLE", str(exc), run_dir=freshness_target.as_posix()) from None
        except Exception as exc:  # noqa: BLE001
            raise SnapshotTransientError("FRESHNESS_UNREADABLE", f"could not evaluate freshness: {exc}") from None

        # On-disk integrity failure must NEVER publish stale/corrupted data or overwrite last_valid_snapshot.
        _raise_if_integrity_failed(raw_freshness.get("integrity"), run_dir=freshness_target)
        selected_cfg_row: dict[str, Any] | None = None
        if is_multi_cfg_run and selected_cfg is not None:
            for row in raw_freshness.get("configurations") or []:
                if isinstance(row, dict) and row.get("configuration_id") == selected_cfg:
                    selected_cfg_row = row
                    break
            if selected_cfg_row is not None:
                _raise_if_integrity_failed(selected_cfg_row.get("integrity"), run_dir=effective_run)

        freshness_info = _normalize_freshness(raw_freshness)
        if freshness_info["state"] == "CURRENT":
            expected_hash = (
                (selected_cfg_row.get("current") or {}).get("project_hash")
                if selected_cfg_row is not None
                else freshness_info.get("current_project_hash")
            )
            built = _build_computed_run_snapshot(
                spec,
                effective_run,
                root_run_dir=spec.run_dir if is_multi_cfg_run else None,
                selected_cfg=selected_cfg,
                available_cfgs=available_cfgs,
                revision=revision,
                fingerprint=fp_before,
                freshness_info=freshness_info,
                project_check=check_info,
                expected_project_hash=expected_hash,
                root_project_hash=freshness_info.get("current_project_hash") if is_multi_cfg_run else None,
            )
        else:
            run_meta = raw_freshness.get("run") or {}
            historical_run = {
                "run_dir": freshness_target.as_posix(),
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
                root_run_dir=spec.run_dir if is_multi_cfg_run else None,
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
    """Run ``os_optics.local.run_optical_project`` (or ``run_configured_project``) once into ``temp_dir``."""
    require_engine()
    from os_optics.local import json_api, run_optical_project
    from os_optics.local.configurations import run_configured_project

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
        if _declared_configurations(document):
            run_configured_project(document, output_dir=run_dir)
        else:
            run_optical_project(document, output_dir=run_dir)
    except Exception as exc:  # noqa: BLE001
        raise SnapshotError("EVALUATION_FAILED", f"explicit local evaluation failed: {exc}") from None
    return run_dir, check.get("project_hash")
