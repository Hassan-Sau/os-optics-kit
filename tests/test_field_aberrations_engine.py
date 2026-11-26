"""Field curvature and distortion against an analytic reference: an ideal thin lens at several field angles.

An ideal f = 100 mm thin lens images a field angle theta at height f tan(theta) on its focal plane, for every pupil ray:
no distortion and a flat focal surface (tangential and sagittal focus on the plane). The chief rays pass the lens
centre. Reference: paraxial/ideal thin-lens imaging, h = f tan(theta).
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from os_optics_kit import field_aberrations

ROOT = Path(__file__).resolve().parents[1]
FOCAL_MM = 100.0
FIELDS_DEG = (0.0, 3.0, 6.0)

try:
    import os_optics  # noqa: F401
    HAS_ENGINE = True
except ImportError:
    HAS_ENGINE = False


def _qmul(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return [aw * bw - ax * bx - ay * by - az * bz, aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx, aw * bz + ax * by - ay * bx + az * bw]


def _project() -> dict:
    project = json.loads((ROOT / "cases/ideal-lens-focus/project.json").read_text(encoding="utf-8"))
    project["targets"] = []
    source = next(c for c in project["components"] if c["optical_role"] == "source")
    lens = next(c for c in project["components"] if c["optical_role"] == "lens")
    centre = lens["nominal_pose"]["position_mm"]
    base_q = source["nominal_pose"]["quaternion_wxyz"]
    sources = []
    for i, theta in enumerate(FIELDS_DEG):
        half = math.radians(-theta) / 2.0
        q = _qmul([math.cos(half), 0.0, math.sin(half), 0.0], base_q)  # rotate the +x emission axis towards +z
        axis = field_aberrations._rotate(q, (0.0, 0.0, 1.0))
        item = copy.deepcopy(source)
        item["instance_id"] = f"F{i}"
        item["nominal_pose"]["quaternion_wxyz"] = q
        item["nominal_pose"]["position_mm"] = [centre[k] - 10.0 * axis[k] for k in range(3)]
        item["source_model"]["waist_radius_mm"] = 0.5
        sources.append(item)
    project["components"] = [c for c in project["components"] if c["optical_role"] != "source"] + sources
    plane = project["observations"][0]
    plane["aperture"]["radius_mm"] = 20.0
    plane["pose"]["position_mm"][0] = centre[0] + FOCAL_MM
    return project


@unittest.skipUnless(HAS_ENGINE, "needs os-optics (the engine)")
class FieldAberrationsOfAnIdealLens(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        (base / "project.json").write_text(json.dumps(_project()), encoding="utf-8")
        run = base / "run"
        done = subprocess.run([sys.executable, "-m", "os_optics", "project", "run", str(base / "project.json"),
                               "--out-dir", str(run)], capture_output=True, text=True)
        if not (run / "manifest.json").exists():
            raise AssertionError(f"engine run failed: {done.stderr[-800:]}")
        cls.document = field_aberrations.build(run, "focus", efl_mm=FOCAL_MM)
        cls.default = field_aberrations.build(run, "focus")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_field_angles_come_from_the_source_poses(self):
        angles = sorted(round(row["field_angle_deg"], 9) for row in self.document["fields"])
        self.assertEqual(angles, [round(a, 9) for a in FIELDS_DEG])

    def test_image_height_is_f_tan_theta_and_distortion_is_zero(self):
        for row in self.document["fields"]:
            expected = FOCAL_MM * math.tan(math.radians(row["field_angle_deg"]))
            with self.subTest(field=row["field_angle_deg"]):
                self.assertAlmostEqual(row["image_height_mm"], expected, delta=1e-6)
                self.assertAlmostEqual(row["distortion_pct"], 0.0, delta=1e-5)

    def test_without_an_efl_the_reference_is_the_engine_paraxial_efl(self):
        for wavelength, reference in self.default["references"].items():
            with self.subTest(wavelength=wavelength):
                self.assertAlmostEqual(reference["efl_mm"], FOCAL_MM, delta=1e-9)
                self.assertIn("engine paraxial EFL of LENS", reference["basis"])
        for row in self.default["fields"]:
            with self.subTest(field=row["field_angle_deg"]):
                self.assertAlmostEqual(row["distortion_pct"], 0.0, delta=1e-5)
                # the ideal lens has no aperture stop: the values are those of each source's beam-centre ray
                self.assertEqual(row["chief_ray"]["status"], "NO_APERTURE_STOP")
        self.assertIn("no aperture stop", self.default["chief_ray"])
        self.assertEqual(self.default["unreached_fields"], [])

    def test_focal_surface_is_flat(self):
        for row in self.document["fields"]:
            with self.subTest(field=row["field_angle_deg"]):
                self.assertAlmostEqual(row["tangential_focus_mm"], 0.0, delta=1e-4)
                self.assertAlmostEqual(row["sagittal_focus_mm"], 0.0, delta=1e-4)


@unittest.skipUnless(HAS_ENGINE, "needs os-optics (the engine)")
class EveryDeclaredFieldIsAccountedFor(unittest.TestCase):
    """The same lens with an on-axis field, a 3 deg field, a 30 deg field whose image (f tan 30 deg = 57.7 mm) lies
    outside the 20 mm plane, and a source that emits away from the lens."""

    @classmethod
    def setUpClass(cls):
        project = _project()
        sources = {c["instance_id"]: c for c in project["components"] if c["optical_role"] == "source"}
        lens = next(c for c in project["components"] if c["optical_role"] == "lens")
        centre = lens["nominal_pose"]["position_mm"]
        far = copy.deepcopy(sources["F1"])
        q = _qmul([math.cos(math.radians(-30.0) / 2.0), 0.0, math.sin(math.radians(-30.0) / 2.0), 0.0],
                  sources["F0"]["nominal_pose"]["quaternion_wxyz"])
        axis = field_aberrations._rotate(q, (0.0, 0.0, 1.0))
        far.update(instance_id="FAR")
        far["nominal_pose"].update(quaternion_wxyz=q, position_mm=[centre[k] - 10.0 * axis[k] for k in range(3)])
        away = copy.deepcopy(sources["F0"])
        away["instance_id"] = "AWAY"
        # half a turn about the source's local x: it emits along -x, away from the lens
        away["nominal_pose"]["quaternion_wxyz"] = _qmul(sources["F0"]["nominal_pose"]["quaternion_wxyz"],
                                                        [0.0, 1.0, 0.0, 0.0])
        project["components"] = ([c for c in project["components"] if c["instance_id"] != "F2"] + [far, away])
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        (base / "project.json").write_text(json.dumps(project), encoding="utf-8")
        run = base / "run"
        done = subprocess.run([sys.executable, "-m", "os_optics", "project", "run", str(base / "project.json"),
                               "--out-dir", str(run)], capture_output=True, text=True)
        if not (run / "manifest.json").exists():
            raise AssertionError(f"engine run failed: {done.stderr[-800:]}")
        cls.declared = sorted(c["instance_id"] for c in project["components"] if c["optical_role"] == "source")
        cls.document = field_aberrations.build(run, "focus")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_every_declared_source_is_in_fields_or_unreached_fields(self):
        fields = {row["source_id"] for row in self.document["fields"]}
        unreached = {entry["source_id"]: entry for entry in self.document["unreached_fields"]}
        self.assertEqual(fields, {"F0", "F1"})
        self.assertEqual(sorted(fields | set(unreached)), self.declared)
        self.assertFalse(fields & set(unreached))
        far, away = unreached["FAR"], unreached["AWAY"]
        self.assertEqual(far["reason"], "NO_COUNTED_RAYS_AT_MONITOR")
        self.assertEqual(away["reason"], "NO_RAYS_AT_MONITOR")
        for entry in (far, away):
            with self.subTest(source=entry["source_id"]):
                self.assertEqual(entry["path_id"], f"{entry['source_id']}#0")
                self.assertEqual(entry["wavelength_nm"], self.document["fields"][0]["wavelength_nm"])
                self.assertEqual(entry["termination"], "ESCAPED")
                self.assertIn("termination ESCAPED", entry["message"])


if __name__ == "__main__":
    unittest.main()
