"""The chief ray's stop crossing on engine runs: a decentred aperture stop, a sampled stop and a reflected path.

An aperture stop 5 mm in front of the ideal f = 100 mm thin lens of the ideal-lens-focus case, opening radius 3 mm.
Its opening centre is stated in the stop plane's local X, Y (opening_center_mm); the stop pose turns local X into
design +Y, so [1.5, 0] puts the opening centre 1.5 mm above the axis. Collimated sources parallel to the axis at
design heights y = 0, 1.5 and 2.5 mm all focus on the plane origin, 100 mm behind the lens (field angle 0). Their
beam-centre rays cross the stop plane at (5, y, 20), so their distance from the opening centre is |y - 1.5|:
1.5, 0 and 1.0 mm. A fourth source at y = -2.5 mm lies 4 mm from that centre with all its rays further than 3 mm from
it (waist radius 0.5 mm), so the engine blocks it at the stop: the engine's own clipping places the opening on the
side the offsets are measured from. With the opening on the axis (the centred control) the offsets are |y|.

A stop holding a thin sample ends the crossing segment with the event complex_transmission instead of aperture_stop;
its crossing is found by the stop's Engine surface id.

The mirror-alignment case folds its only path at a mirror before the plane `near`: the command refuses it
(REFLECTED_PATH_UNSUPPORTED).
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from os_optics_kit import field_aberrations

ROOT = Path(__file__).resolve().parents[1]
HEIGHTS_MM = {"AXIS": 0.0, "AIMED": 1.5, "HIGH": 2.5, "BLOCKED": -2.5}
OPENING_RADIUS_MM = 3.0
STOP_X_MM = 5.0

try:
    import os_optics  # noqa: F401
    HAS_ENGINE = True
except ImportError:
    HAS_ENGINE = False


def stop_project(centre: tuple[float, float], sample: bool = False) -> dict:
    project = json.loads((ROOT / "cases/ideal-lens-focus/project.json").read_text(encoding="utf-8"))
    project["targets"] = []
    (source,) = [c for c in project["components"] if c["optical_role"] == "source"]
    sources = []
    for name, height in HEIGHTS_MM.items():
        item = json.loads(json.dumps(source))
        item["instance_id"] = name
        item["nominal_pose"]["position_mm"] = [0.0, height, 20.0]
        item["source_model"]["waist_radius_mm"] = 0.5
        sources.append(item)
    model = {"edge_radius_mm": "12.7", "model_kind": "APERTURE_STOP", "model_version": "1",
             "opening_radius_mm": str(OPENING_RADIUS_MM), "opening_center_mm": [str(centre[0]), str(centre[1])],
             "plane_z_mm": "0", "status": "SPECIFIED"}
    if sample:
        model["sample"] = {"amplitude": "1", "phase_rad": "0.1"}
    project["optical_models"].append({"model_id": "stop", "model": model})
    stop = {"custom_part": {"custom_part_key": "stop", "description": "aperture stop"}, "instance_id": "STOP",
            "nominal_pose": {"position_mm": [STOP_X_MM, 0.0, 20.0], "quaternion_wxyz": [0.5, 0.5, 0.5, 0.5]},
            "optical_model_id": "stop", "optical_role": "aperture", "retained_dofs": [], "selected_parameters": []}
    project["components"] = [stop] + [c for c in project["components"] if c["optical_role"] != "source"] + sources
    plane = project["observations"][0]
    plane["aperture"]["radius_mm"] = 20.0
    plane["pose"]["position_mm"][0] = 110.0
    return project


def run(base: Path, name: str, project: dict, *options: str) -> Path:
    (base / f"{name}.json").write_text(json.dumps(project), encoding="utf-8")
    out = base / name
    done = subprocess.run([sys.executable, "-m", "os_optics", "project", "run", str(base / f"{name}.json"),
                           "--out-dir", str(out), *options], capture_output=True, text=True)
    if not (out / "manifest.json").exists():
        raise AssertionError(f"engine run failed: {done.stderr[-800:]}")
    return out


@unittest.skipUnless(HAS_ENGINE, "needs os-optics (the engine)")
class StopCrossingOnEngineRuns(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.runs = {"decentred": run(base, "decentred", stop_project((1.5, 0.0))),
                    "centred": run(base, "centred", stop_project((0.0, 0.0))),
                    "sampled": run(base, "sampled", stop_project((1.5, 0.0), sample=True))}
        cls.documents = {name: field_aberrations.build(path, "focus", efl_mm=100.0) for name, path in cls.runs.items()}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def rows(self, name: str) -> dict[str, dict]:
        return {row["source_id"]: row for row in self.documents[name]["fields"]}

    def segment_events(self, name: str, source_id: str) -> list[tuple]:
        result = json.loads((self.runs[name] / "simulation_result.json").read_text(encoding="utf-8"))
        (path,) = [p for p in result["paths"] if p["source_id"] == source_id]
        return [(s["end_event"], s["end_surface_id"]) for s in path["segments"]]

    def check_offsets(self, name: str, centre_y: float) -> None:
        rows = self.rows(name)
        for source_id in ("AXIS", "AIMED", "HIGH"):
            with self.subTest(run=name, source=source_id):
                chief = rows[source_id]["chief_ray"]
                self.assertEqual(chief["stop_id"], "STOP/stop")
                self.assertEqual(chief["identified_by"], "ENGINE_SURFACE_ID")
                self.assertAlmostEqual(chief["stop_offset_mm"], abs(HEIGHTS_MM[source_id] - centre_y), delta=1e-9)
                within = abs(HEIGHTS_MM[source_id] - centre_y) <= field_aberrations.STOP_TOLERANCE_MM
                self.assertEqual(chief["status"], "WITHIN_STOP_TOLERANCE" if within else "CHIEF_RAY_OFF_STOP")
                if within:
                    self.assertAlmostEqual(rows[source_id]["distortion_pct"], 0.0, delta=1e-9)
                else:
                    self.assertIsNone(rows[source_id]["distortion_pct"])
                    self.assertIsNone(rows[source_id]["tangential_focus_mm"])
                    self.assertTrue(rows[source_id]["distortion_reason"].startswith("CHIEF_RAY_OFF_STOP"))

    def test_a_decentred_stop_reports_the_offset_from_its_opening(self):
        (stop,) = self.documents["decentred"]["aperture_stops"]
        self.assertEqual(stop["stop_id"], "STOP/stop")
        self.assertEqual(stop["opening_center_mm"], [1.5, 0.0])
        for got, want in zip(stop["centre_mm"], (STOP_X_MM, 1.5, 20.0)):
            self.assertAlmostEqual(got, want, delta=1e-12)
        # the on-axis beam-centre ray crosses 1.5 mm from the opening centre and fails the 0.01 mm tolerance
        self.check_offsets("decentred", 1.5)
        self.assertEqual(self.segment_events("decentred", "AXIS")[0], ("aperture_stop", "STOP/stop"))

    def test_the_engine_blocks_the_source_beyond_the_opening(self):
        unreached = {entry["source_id"]: entry for entry in self.documents["decentred"]["unreached_fields"]}
        self.assertEqual(sorted(unreached), ["BLOCKED"])
        self.assertEqual(unreached["BLOCKED"]["termination_surface_id"], "STOP/stop")
        self.assertNotIn("BLOCKED", self.rows("decentred"))

    def test_a_centred_stop_is_the_control(self):
        self.assertEqual(self.documents["centred"]["aperture_stops"][0]["opening_center_mm"], [0.0, 0.0])
        self.check_offsets("centred", 0.0)

    def test_a_sampled_stop_crossing_is_found_by_its_surface_id(self):
        self.assertEqual(self.segment_events("sampled", "AXIS")[0], ("complex_transmission", "STOP/stop"))
        self.check_offsets("sampled", 1.5)


@unittest.skipUnless(HAS_ENGINE, "needs os-optics (the engine)")
class ReflectedPathOnAnEngineRun(unittest.TestCase):
    def test_a_folded_path_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = json.loads((ROOT / "cases/mirror-alignment/project.json").read_text(encoding="utf-8"))
            path = run(Path(tmp), "mirror", project)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = field_aberrations.main([str(path), "--monitor", "near", "--efl", "100"])
        self.assertEqual(code, 4)
        error = json.loads(stderr.getvalue())["error"]
        self.assertEqual(error["code"], "REFLECTED_PATH_UNSUPPORTED")
        self.assertEqual(error["details"], {"monitor": "near",
                                            "reflected_paths": [{"path_id": "SRC#0", "reflections": 1}]})
        self.assertIn("is reflected before it", error["message"])
        self.assertIn("supports paths without a reflection before the plane only", error["message"])


if __name__ == "__main__":
    unittest.main()
