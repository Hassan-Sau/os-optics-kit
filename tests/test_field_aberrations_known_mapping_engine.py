"""Field curvature and distortion against closed-form values on a lens with known non-zero values.

An ideal thin lens (perfect imaging, f = 100 mm) with the image plane placed a known distance DELTA from its focal
plane. Every chief ray crosses the axis at the focal plane, so at the measurement plane the image height is exactly
h = (f + DELTA) tan(theta): with the EFL stated as f, the distortion is exactly DELTA / f at every field, in percent,
and its sign follows DELTA. The tangential and sagittal focus are both exactly DELTA at every field (no astigmatism:
an ideal lens has none), so the focus reading is a known non-zero offset too. These are closed-form values, not engine
outputs: the engine only traces the rays.

A second measurement plane DELTA2 checks that both quantities follow the plane, and the on-axis field satisfies
h = 0 and distortion 0.
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

    def test_image_height_is_the_closed_form(self):
        for offset in OFFSETS_MM:
            for row in self.document(offset)["fields"]:
                expected = (FOCAL_MM + offset) * math.tan(math.radians(row["field_angle_deg"]))
                with self.subTest(offset=offset, field=row["field_angle_deg"]):
                    self.assertAlmostEqual(row["image_height_mm"], expected, delta=1e-6)


class FieldAberrationsSyntheticTests(unittest.TestCase):
    """No engine: the geometry helpers, the references and the on-axis refusal."""

    @staticmethod
    def paths():
        def path(path_id, angle, height_mm, tan_u, focus_t, focus_s):
            return {"path_id": path_id, "source_id": path_id.split("#")[0], "wavelength_nm": 550.0,
                    "chief_uv_mm": [height_mm, 0.0],
                    "fans": {"tangential": [{"rho": 0.1, "longitudinal_mm": focus_t},
                                            {"rho": -0.1, "longitudinal_mm": focus_t}],
                             "sagittal": [{"rho": 0.1, "longitudinal_mm": focus_s}]}}
        return [path("A#0", 0.0, 0.0, 0.0, 0.01, 0.01), path("B#0", 5.0, 10.0, 0.2, -0.5, -0.2)]

    def test_rotation_of_the_local_z_axis(self):
        for got, want in zip(field_aberrations._rotate([0.5, 0.5, 0.5, 0.5], (0.0, 0.0, 1.0)), (1.0, 0.0, 0.0)):
            self.assertAlmostEqual(got, want)
        x, y, z = field_aberrations._rotate([math.sqrt(0.5), 0.0, math.sqrt(0.5), 0.0], (0.0, 0.0, 1.0))
        self.assertAlmostEqual(x, 1.0)
        self.assertAlmostEqual(y, 0.0)
        self.assertAlmostEqual(z, 0.0)

    def test_axes_and_angles(self):
        self.assertEqual(field_aberrations.source_axes({"components": [
            {"optical_role": "source", "instance_id": "A", "nominal_pose": {"quaternion_wxyz": [0.5, 0.5, 0.5, 0.5]}},
            {"optical_role": "lens", "instance_id": "L", "nominal_pose": {"quaternion_wxyz": [1, 0, 0, 0]}}]}), {"A": (1.0, 0.0, 0.0)})
        self.assertAlmostEqual(field_aberrations._angle_deg((1, 0, 0), (0, 1, 0)), 90.0)

    def test_distortion_math_and_reference_selection(self):
        axes = {"A": (1.0, 0.0, 0.0), "B": (math.cos(math.radians(5.0)), 0.0, -math.sin(math.radians(5.0)))}
        document = field_aberrations.analyze(self.paths(), axes)
        rows = {row["path_id"]: row for row in document["fields"]}
        self.assertAlmostEqual(rows["A#0"]["distortion_pct"], 0.0)
        self.assertAlmostEqual(rows["B#0"]["field_angle_deg"], 5.0)
        # no EFL stated: the smallest non-zero field is the reference, so it reads 0 by construction
        self.assertAlmostEqual(rows["B#0"]["distortion_pct"], 0.0)
        self.assertAlmostEqual(document["references"]["550.0"]["efl_mm"],
                               10.0 / math.tan(math.radians(5.0)), places=9)
        stated = field_aberrations.analyze(self.paths(), axes, efl_mm=100.0)
        row = {r["path_id"]: r for r in stated["fields"]}["B#0"]
        expected = 100.0 * (10.0 - 100.0 * math.tan(math.radians(5.0))) / (100.0 * math.tan(math.radians(5.0)))
        self.assertAlmostEqual(row["distortion_pct"], expected, places=9)

    def test_focus_zone_and_astigmatism(self):
        axes = {"A": (1.0, 0.0, 0.0), "B": (math.cos(math.radians(5.0)), 0.0, -math.sin(math.radians(5.0)))}
        row = {r["path_id"]: r for r in field_aberrations.analyze(self.paths(), axes)["fields"]}["B#0"]
        self.assertEqual(row["focus_zone_rho"], 0.1)
        self.assertAlmostEqual(row["tangential_focus_mm"], -0.5)
        self.assertAlmostEqual(row["sagittal_focus_mm"], -0.2)
        self.assertAlmostEqual(row["astigmatism_mm"], -0.3)

    def test_a_run_without_an_on_axis_field_is_refused(self):
        axes = {"A": (1.0, 0.0, 0.0), "B": (math.cos(math.radians(5.0)), 0.0, -math.sin(math.radians(5.0)))}
        off_axis = [p for p in self.paths() if p["path_id"] != "A#0"]
        with self.assertRaises(field_aberrations.FieldError) as caught:
            field_aberrations.analyze(off_axis, axes)
        self.assertEqual(caught.exception.code, "AXIS_FIELD_MISSING")
        # a tolerance that accepts it lets the analysis run and measures angles from that field
        document = field_aberrations.analyze(off_axis, axes, axis_tolerance_mm=20.0)
        self.assertEqual(document["axis_source_id"], "B")
        self.assertEqual(document["fields"][0]["field_angle_deg"], 0.0)

    def test_missing_source_pose_is_refused(self):
        with self.assertRaises(field_aberrations.FieldError) as caught:
            field_aberrations.analyze(self.paths(), {"A": (1.0, 0.0, 0.0)})
        self.assertEqual(caught.exception.code, "SOURCE_POSE_MISSING")


if __name__ == "__main__":
    unittest.main()
