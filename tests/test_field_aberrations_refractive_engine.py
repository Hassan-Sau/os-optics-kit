"""Field curvature and distortion of a refractive lens against an independent real-ray trace.

The aspheric-lens case: five elements (conic and even-aspheric surfaces, N-BK7 and F2) behind a front aperture stop, at
587.5618 nm, with fields of 0, 12.5 and 25 deg whose sources are aimed at the stop centre. The reference values come
from this file's own trace of the case's prescription.csv: vector Snell refraction at each conic/aspheric surface
(intersections by Newton iteration), with the refractive index of each medium that the run states for its segments
(the air of the Project's environment and the two glasses). Only the indices and the pupil-ray launch points of the run
are inputs; the trace, the paraxial EFL and rear focal point (a y-nu trace of the same surfaces) and the expected
distortion and inner-zone focus are computed here:

* image height: the chief ray's height at the image plane;
* EFL: -y1 / u' of a paraxial marginal ray in the same media (the engine's EFL in the air of the environment), and the
  rear focal point where that ray crosses the axis;
* distortion: (h - EFL tan(theta)) / (EFL tan(theta)), h the chief ray's height on the paraxial rear focal plane;
* tangential and sagittal focus: where the innermost-ring rays of the meridional and sagittal sections cross the chief
  ray, along the axis from the plane.

A second, non-absorbing plane 1 mm before the image plane (further from the paraxial focus) gives the same distortion:
h and EFL tan(theta) both refer to the paraxial focal plane, whichever plane the rays are read at.

A fourth source at the same 12.5 deg field is emitted from the on-axis position [-5, 0, 0], so its beam-centre ray
crosses the stop 5 tan(12.5 deg) from its centre: its row is flagged and carries no distortion or focus value, while
the aimed source at the same field keeps its values.
"""

from __future__ import annotations

import copy
import csv
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from os_optics_kit import field_aberrations

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "cases" / "aspheric-lens"
WAVELENGTH_NM = 587.5618
KEPT_SOURCES = ("SRC0_1", "SRC1_1", "SRC2_1")
OFF_STOP = "OFF1_1"
NEAR_PLANE = "near_plane"
NEAR_SHIFT_MM = -1.0

try:
    import os_optics  # noqa: F401
    HAS_ENGINE = True
except ImportError:
    HAS_ENGINE = False


def prescription() -> list[dict]:
    """Surfaces of the case's prescription: vertex position along the axis, curvature, conic, aspheric terms, the
    material after the surface (None: air)."""
    lines = (CASE / "prescription.csv").read_text(encoding="utf-8").splitlines()
    table = [line for line in lines[lines.index("[surfaces]") + 1:] if line.strip() and not line.startswith("#")]
    surfaces, vertex = [], 0.0
    for row in csv.DictReader(table):
        radius = float(row["radius"]) if row.get("radius") else math.inf
        surfaces.append({"vertex": vertex, "c": 0.0 if math.isinf(radius) else 1.0 / radius,
                         "k": float(row.get("conic") or 0.0),
                         "a": [float(row.get(f"a{n}") or 0.0) for n in (4, 6, 8, 10)],
                         "material": row.get("material") or None})
        vertex += float(row["thickness"])
    return surfaces


def _sag(surface: dict, r2: float) -> tuple[float, float]:
    """Sag and d(sag)/d(r^2) of a conic plus even-aspheric surface."""
    c, k = surface["c"], surface["k"]
    root = math.sqrt(1.0 - (1.0 + k) * c * c * r2)
    sag = c * r2 / (1.0 + root)
    dsag = c / (1.0 + root) + c * (1.0 + k) * c * c * r2 / (2.0 * root * (1.0 + root) ** 2)
    for i, a in enumerate(surface["a"]):
        power = i + 2
        sag += a * r2 ** power
        dsag += power * a * r2 ** (power - 1)
    return sag, dsag


def trace(point, direction, surfaces, index, plane_x):
    """Real ray through the surfaces (axis along +x, vertices at surface['vertex']) to the plane x = plane_x: its point
    and unit direction there. ``index(material)`` is the medium's refractive index (material None: air)."""
    p, d = list(point), list(direction)
    n1 = index(None)
    for surface in surfaces:
        t = (surface["vertex"] - p[0]) / d[0]
        for _ in range(60):
            x, y, z = (p[k] + t * d[k] for k in range(3))
            sag, dsag = _sag(surface, y * y + z * z)
            f = x - surface["vertex"] - sag
            df = d[0] - dsag * 2.0 * (y * d[1] + z * d[2])
            step = f / df
            t -= step
            if abs(step) < 1e-14:
                break
        p = [p[k] + t * d[k] for k in range(3)]
        _sag_value, dsag = _sag(surface, p[1] ** 2 + p[2] ** 2)
        normal = [1.0, -2.0 * dsag * p[1], -2.0 * dsag * p[2]]
        length = math.sqrt(sum(v * v for v in normal))
        normal = [v / length for v in normal]
        n2 = index(surface["material"])
        cos_i = sum(normal[k] * d[k] for k in range(3))
        if cos_i < 0:
            normal, cos_i = [-v for v in normal], -cos_i
        mu = n1 / n2
        cos_t = math.sqrt(1.0 - mu * mu * (1.0 - cos_i * cos_i))
        d = [mu * d[k] + (cos_t - mu * cos_i) * normal[k] for k in range(3)]
        n1 = n2
    t = (plane_x - p[0]) / d[0]
    return [p[k] + t * d[k] for k in range(3)], d


def paraxial_focus(surfaces, index) -> tuple[float, float]:
    """EFL (-y1 / u') of a ray parallel to the axis at height 1 (y-nu trace, absolute indices), and the position along
    the axis where it crosses the axis after the last surface (the rear focal point)."""
    y, nu, n1 = 1.0, 0.0, index(None)
    previous = 0.0
    for i, surface in enumerate(surfaces):
        if i:
            y += (surface["vertex"] - previous) * nu / n1
        n2 = index(surface["material"])
        nu -= y * (n2 - n1) * surface["c"]
        n1, previous = n2, surface["vertex"]
    slope = nu / n1
    return -1.0 / slope, previous - y / slope


def _crossing(ray, chief, axis: int) -> float:
    """Distance along x from the plane at which a ray crosses the chief ray in the section of coordinate ``axis``."""
    (rp, rd), (cp, cd) = ray, chief
    return -(rp[axis] - cp[axis]) / (rd[axis] / rd[0] - cd[axis] / cd[0])


def project() -> dict:
    """The case at one wavelength, without targets, with a mis-aimed copy of the 12.5 deg source."""
    document = json.loads((CASE / "project.json").read_text(encoding="utf-8"))
    document["targets"] = []
    sources = {c["instance_id"]: c for c in document["components"] if c["optical_role"] == "source"}
    off = copy.deepcopy(sources["SRC1_1"])
    off["instance_id"] = OFF_STOP
    off["nominal_pose"]["position_mm"] = list(sources["SRC0_1"]["nominal_pose"]["position_mm"])
    document["components"] = ([c for c in document["components"] if c["optical_role"] != "source"]
                              + [sources[name] for name in KEPT_SOURCES] + [off])
    near = copy.deepcopy(document["observations"][0])
    near["observation_id"] = NEAR_PLANE
    near["absorbing"] = False
    near["pose"]["position_mm"][0] += NEAR_SHIFT_MM
    document["observations"].append(near)
    return document


@unittest.skipUnless(HAS_ENGINE, "needs os-optics (the engine)")
class FieldAberrationsOfARefractiveLens(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.project = project()
        (base / "project.json").write_text(json.dumps(cls.project), encoding="utf-8")
        cls.run_dir = base / "run"
        done = subprocess.run([sys.executable, "-m", "os_optics", "project", "run", str(base / "project.json"),
                               "--out-dir", str(cls.run_dir)], capture_output=True, text=True)
        if not (cls.run_dir / "manifest.json").exists():
            raise AssertionError(f"engine run failed: {done.stderr[-800:]}")
        cls.document = field_aberrations.build(cls.run_dir, "image_plane")
        cls.rows = {row["source_id"]: row for row in cls.document["fields"]}
        cls.near = field_aberrations.build(cls.run_dir, NEAR_PLANE)
        result = json.loads((cls.run_dir / "simulation_result.json").read_text(encoding="utf-8"))
        media = {}
        for path in result["paths"]:
            for segment in path["segments"]:
                media[segment["medium_id"]] = segment["refractive_index"]
        cls.index = staticmethod(lambda material: media["air" if material is None else f"project:{material}"])
        cls.surfaces = prescription()
        cls.plane_x = cls.project["observations"][0]["pose"]["position_mm"][0]
        cls.efl, cls.focus_x = paraxial_focus(cls.surfaces, cls.index)
        cls.rays = {p["source_id"]: p for p in json.loads((cls.run_dir / "ray_data.json").read_text(encoding="utf-8"))["paths"]}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def chief(self, source_id, plane_x=None):
        pose = next(c for c in self.project["components"] if c["instance_id"] == source_id)["nominal_pose"]
        axis = field_aberrations._rotate(pose["quaternion_wxyz"], (0.0, 0.0, 1.0))
        return trace(pose["position_mm"], axis, self.surfaces, self.index,
                     self.plane_x if plane_x is None else plane_x)

    def test_the_default_efl_is_the_paraxial_efl_of_the_prescription(self):
        for document, plane_x in ((self.document, self.plane_x), (self.near, self.plane_x + NEAR_SHIFT_MM)):
            reference = document["references"][str(WAVELENGTH_NM)]
            with self.subTest(monitor=document["monitor"]):
                self.assertIn("engine paraxial EFL", reference["basis"])
                self.assertIn("paraxial rear focal plane", reference["distortion_plane"])
                self.assertAlmostEqual(reference["efl_mm"], self.efl, delta=1e-6)
                for got, want in zip(reference["rear_focal_point_mm"], (self.focus_x, 0.0, 0.0)):
                    self.assertAlmostEqual(got, want, delta=1e-6)
                self.assertAlmostEqual(reference["paraxial_focus_from_plane_mm"], self.focus_x - plane_x, delta=1e-6)
        # the near plane lies well off the paraxial focus
        self.assertGreater(abs(self.focus_x - self.plane_x - NEAR_SHIFT_MM), 0.5)

    def test_image_height_and_distortion_match_the_real_ray_trace(self):
        for source_id in KEPT_SOURCES:
            row = self.rows[source_id]
            point, _direction = self.chief(source_id)
            height = math.hypot(point[1], point[2])
            at_focus, _direction = self.chief(source_id, self.focus_x)
            focal_height = math.hypot(at_focus[1], at_focus[2])
            with self.subTest(source=source_id, field=row["field_angle_deg"]):
                self.assertEqual(row["chief_ray"]["status"], "WITHIN_STOP_TOLERANCE")
                self.assertEqual(row["chief_ray"]["stop_tolerance_mm"], field_aberrations.STOP_TOLERANCE_MM)
                self.assertAlmostEqual(row["image_height_mm"], height, delta=1e-6)
                self.assertAlmostEqual(row["distortion_height_mm"], focal_height, delta=1e-6)
                if row["field_angle_deg"] > 1e-6:
                    reference = self.efl * math.tan(math.radians(row["field_angle_deg"]))
                    self.assertAlmostEqual(row["reference_height_mm"], reference, delta=1e-6)
                    self.assertAlmostEqual(row["distortion_pct"], 100.0 * (focal_height - reference) / reference,
                                           delta=1e-5)
                else:
                    self.assertEqual(row["distortion_pct"], 0.0)
        # a real lens with non-zero distortion across the field
        self.assertGreater(self.rows["SRC1_1"]["distortion_pct"], 0.05)
        self.assertLess(self.rows["SRC2_1"]["distortion_pct"], -0.2)

    def test_distortion_does_not_depend_on_the_plane_the_rays_are_read_at(self):
        near = {row["source_id"]: row for row in self.near["fields"]}
        for source_id in KEPT_SOURCES:
            with self.subTest(source=source_id):
                self.assertAlmostEqual(near[source_id]["distortion_pct"], self.rows[source_id]["distortion_pct"],
                                       delta=1e-6)
                self.assertAlmostEqual(near[source_id]["distortion_height_mm"],
                                       self.rows[source_id]["distortion_height_mm"], delta=1e-6)
        # measured on the near plane itself with the same EFL, the value carries that plane's defocus term
        efl = self.near["references"][str(WAVELENGTH_NM)]["efl_mm"]
        on_plane = field_aberrations.build(self.run_dir, NEAR_PLANE, efl_mm=efl)
        self.assertIn("the plane itself", on_plane["references"][str(WAVELENGTH_NM)]["distortion_plane"])
        self.assertIsNone(on_plane["references"][str(WAVELENGTH_NM)]["paraxial_focus_from_plane_mm"])
        row = {r["source_id"]: r for r in on_plane["fields"]}["SRC2_1"]
        self.assertEqual(row["distortion_height_mm"], row["image_height_mm"])
        self.assertGreater(abs(row["distortion_pct"] - near["SRC2_1"]["distortion_pct"]), 0.1)

    def test_inner_zone_focus_matches_the_real_ray_trace(self):
        for source_id in KEPT_SOURCES:
            row = self.rows[source_id]
            chief = self.chief(source_id)
            start = next(c for c in self.project["components"]
                         if c["instance_id"] == source_id)["nominal_pose"]["position_mm"]
            inner = [r for r in self.rays[source_id]["pupil"]["rays"] if r["ring_index"] == 0]

            def shift(ray, k):
                return abs(ray["points_mm"][0][k] - start[k])

            # the fields lie in the x-z plane: the tangential fan launches in it (no y shift), the sagittal fan
            # launches across it (a y shift only)
            fans = {"tangential": [r for r in inner if shift(r, 1) < 1e-9],
                    "sagittal": [r for r in inner if shift(r, 0) < 1e-9 and shift(r, 2) < 1e-9]}
            expected = {}
            for name, axis in (("tangential", 2), ("sagittal", 1)):
                fan = fans[name]
                self.assertEqual(len(fan), 2, name)
                crossings = [_crossing(trace(r["points_mm"][0], r["directions"][0], self.surfaces, self.index,
                                             self.plane_x), chief, axis) for r in fan]
                expected[name] = sum(crossings) / len(crossings)
            with self.subTest(source=source_id, field=row["field_angle_deg"]):
                self.assertAlmostEqual(row["tangential_focus_mm"], expected["tangential"], delta=1e-5)
                self.assertAlmostEqual(row["sagittal_focus_mm"], expected["sagittal"], delta=1e-5)
        # field curvature and astigmatism off axis, none on axis
        self.assertAlmostEqual(self.rows["SRC0_1"]["astigmatism_mm"], 0.0, delta=1e-6)
        self.assertGreater(abs(self.rows["SRC2_1"]["astigmatism_mm"]), 0.5)

    def test_a_source_not_aimed_at_the_stop_centre_is_flagged(self):
        row = self.rows[OFF_STOP]
        aimed = self.rows["SRC1_1"]
        self.assertAlmostEqual(row["field_angle_deg"], aimed["field_angle_deg"], places=9)
        self.assertEqual(row["chief_ray"]["status"], "CHIEF_RAY_OFF_STOP")
        self.assertEqual(row["chief_ray"]["stop_id"], aimed["chief_ray"]["stop_id"])
        self.assertAlmostEqual(row["chief_ray"]["stop_offset_mm"], 5.0 * math.tan(math.radians(12.5)), delta=1e-6)
        self.assertLess(aimed["chief_ray"]["stop_offset_mm"], 1e-9)
        for key in ("distortion_pct", "tangential_focus_mm", "sagittal_focus_mm", "astigmatism_mm"):
            self.assertIsNone(row[key], key)
        for key in ("distortion_reason", "tangential_focus_reason", "sagittal_focus_reason"):
            self.assertTrue(row[key].startswith("CHIEF_RAY_OFF_STOP"), key)
        self.assertIsNone(aimed["distortion_reason"])
        # a looser tolerance that admits the offset reports the beam-centre ray's values, and says how far it is
        loose = field_aberrations.build(self.run_dir, "image_plane", efl_mm=50.0, stop_tolerance_mm=2.0)
        row = {r["source_id"]: r for r in loose["fields"]}[OFF_STOP]
        self.assertEqual(row["chief_ray"]["status"], "WITHIN_STOP_TOLERANCE")
        self.assertEqual(row["chief_ray"]["stop_tolerance_mm"], 2.0)
        self.assertGreater(row["chief_ray"]["stop_offset_mm"], 1.0)
        self.assertIsNotNone(row["distortion_pct"])

    def test_every_declared_source_reaches_the_plane(self):
        self.assertEqual(self.document["unreached_fields"], [])
        self.assertEqual(len(self.document["fields"]), len(KEPT_SOURCES) + 1)


if __name__ == "__main__":
    unittest.main()
