"""Field curvature and distortion against closed-form values on a lens with known non-zero values.

An ideal thin lens (perfect imaging, f = 100 mm) with the image plane placed a known distance DELTA from its focal
plane. Each source sits 10 mm before the lens centre and is aimed at it; the ideal lens does not deviate that ray, so
every chief ray crosses the axis at the lens and reaches the measurement plane at exactly h = (f + DELTA) tan(theta).
With the EFL stated as f, the distortion is exactly DELTA / f at every field, in percent, and its sign follows DELTA:
a field-independent scale term, which checks the distortion arithmetic, not a field-dependent distortion (the
refractive reference in test_field_aberrations_refractive_engine.py checks that). The tangential and sagittal focus are
both exactly DELTA at every field (no astigmatism: an ideal lens has none), so the focus reading is a known non-zero
offset too. These are closed-form values, not engine outputs: the engine only traces the rays.

A second measurement plane DELTA2 checks that both quantities follow the plane, and the on-axis field satisfies
h = 0 and distortion 0.

Without --efl the reference is the engine's paraxial analysis: EFL f, rear focal point on the focal plane, DELTA in
front of the measurement plane. Each chief ray is carried to that focal plane, where h = f tan(theta) exactly, so the
distortion is 0 at every field for both planes: the plane's defocus does not enter the default value.
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
FIELDS_DEG = (0.0, 4.0, 8.0)
OFFSETS_MM = (5.0, -3.0)

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


def project(offset_mm: float) -> dict:
    """ideal-lens-focus with three field angles and the image plane OFFSET from focus."""
    project = json.loads((ROOT / "cases/ideal-lens-focus/project.json").read_text(encoding="utf-8"))
    project["targets"] = []
    source = next(c for c in project["components"] if c["optical_role"] == "source")
    lens = next(c for c in project["components"] if c["optical_role"] == "lens")
    centre = lens["nominal_pose"]["position_mm"]
    base_q = source["nominal_pose"]["quaternion_wxyz"]
    sources = []
    for i, theta in enumerate(FIELDS_DEG):
        half = math.radians(-theta) / 2.0
        q = _qmul([math.cos(half), 0.0, math.sin(half), 0.0], base_q)
        axis = field_aberrations._rotate(q, (0.0, 0.0, 1.0))
        item = copy.deepcopy(source)
        item["instance_id"] = f"F{i}"
        item["nominal_pose"]["quaternion_wxyz"] = q
        item["nominal_pose"]["position_mm"] = [centre[k] - 10.0 * axis[k] for k in range(3)]
        item["source_model"]["waist_radius_mm"] = 2.0
        sources.append(item)
    project["components"] = [c for c in project["components"] if c["optical_role"] != "source"] + sources
    plane = project["observations"][0]
    plane["aperture"]["radius_mm"] = 40.0
    plane["pose"]["position_mm"][0] = centre[0] + FOCAL_MM + offset_mm
    return project


@unittest.skipUnless(HAS_ENGINE, "needs os-optics (the engine)")
class FieldAberrationsAgainstClosedForm(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.runs = {}
        base = Path(cls.tmp.name)
        for offset in OFFSETS_MM:
            case = base / f"offset{offset:+.0f}"
            case.mkdir()
            (case / "project.json").write_text(json.dumps(project(offset)), encoding="utf-8")
            run = case / "run"
            done = subprocess.run([sys.executable, "-m", "os_optics", "project", "run", str(case / "project.json"),
                                   "--out-dir", str(run)], capture_output=True, text=True)
            if not (run / "manifest.json").exists():
                raise AssertionError(f"engine run failed: {done.stderr[-800:]}")
            cls.runs[offset] = run

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def document(self, offset):
        return field_aberrations.build(self.runs[offset], "focus", efl_mm=FOCAL_MM)

    def test_distortion_is_the_plane_offset_over_the_efl(self):
        for offset in OFFSETS_MM:
            document = self.document(offset)
            expected = 100.0 * offset / FOCAL_MM
            self.assertNotEqual(expected, 0.0)
            for row in document["fields"]:
                with self.subTest(offset=offset, field=row["field_angle_deg"]):
                    if row["field_angle_deg"] == 0.0:
                        self.assertEqual(row["distortion_pct"], 0.0)
                    else:
                        self.assertAlmostEqual(row["distortion_pct"], expected, delta=1e-9 * abs(expected))

    def test_focus_reads_the_known_plane_offset(self):
        for offset in OFFSETS_MM:
            for row in self.document(offset)["fields"]:
                with self.subTest(offset=offset, field=row["field_angle_deg"]):
                    self.assertAlmostEqual(row["tangential_focus_mm"], -offset, delta=1e-6)
                    self.assertAlmostEqual(row["sagittal_focus_mm"], -offset, delta=1e-6)
                    self.assertAlmostEqual(row["astigmatism_mm"], 0.0, delta=1e-6)
                    self.assertGreater(row["focus_zone_rho"], 0.0)

    def test_without_an_efl_distortion_is_taken_on_the_paraxial_focal_plane(self):
        for offset in OFFSETS_MM:
            document = field_aberrations.build(self.runs[offset], "focus")
            (reference,) = document["references"].values()
            with self.subTest(offset=offset):
                self.assertAlmostEqual(reference["efl_mm"], FOCAL_MM, delta=1e-9)
                self.assertIn("paraxial rear focal plane", reference["distortion_plane"])
                self.assertAlmostEqual(reference["paraxial_focus_from_plane_mm"], -offset, delta=1e-9)
            for row in document["fields"]:
                expected = FOCAL_MM * math.tan(math.radians(row["field_angle_deg"]))
                with self.subTest(offset=offset, field=row["field_angle_deg"]):
                    self.assertAlmostEqual(row["image_height_mm"], (FOCAL_MM + offset) / FOCAL_MM * expected,
                                           delta=1e-6)
                    self.assertAlmostEqual(row["distortion_height_mm"], expected, delta=1e-9)
                    self.assertAlmostEqual(row["distortion_pct"], 0.0, delta=1e-9)

    def test_image_height_is_the_closed_form(self):
        for offset in OFFSETS_MM:
            for row in self.document(offset)["fields"]:
                expected = (FOCAL_MM + offset) * math.tan(math.radians(row["field_angle_deg"]))
                with self.subTest(offset=offset, field=row["field_angle_deg"]):
                    self.assertAlmostEqual(row["image_height_mm"], expected, delta=1e-6)


if __name__ == "__main__":
    unittest.main()
