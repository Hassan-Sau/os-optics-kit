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

    def test_focal_surface_is_flat(self):
        for row in self.document["fields"]:
            with self.subTest(field=row["field_angle_deg"]):
                self.assertAlmostEqual(row["tangential_focus_mm"], 0.0, delta=1e-4)
                self.assertAlmostEqual(row["sagittal_focus_mm"], 0.0, delta=1e-4)


if __name__ == "__main__":
    unittest.main()
