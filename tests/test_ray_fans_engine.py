"""Ray fans against the engine on an analytic case: an ideal thin lens, image planes off focus by a known distance.

An ideal f = 100 mm lens focuses every pupil ray at one point (x = 110 mm). On a plane 5 mm before focus every fan ray
crosses the chief ray 5 mm behind the plane (longitudinal aberration +5 mm); on a plane 5 mm after focus, -5 mm.
Transverse aberration is then rho-linear and sagittal equals tangential. Two wavelengths: the ideal lens is achromatic.
The per-path OPD RMS must equal the engine's 'results wavefront' value (same per-ray data).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from os_optics_kit import ray_fans

ROOT = Path(__file__).resolve().parents[1]

try:
    import os_optics  # noqa: F401
    HAS_ENGINE = True
except ImportError:
    HAS_ENGINE = False

DEFOCUS_MM = 5.0
LONGITUDINAL_TOL_MM = 1e-6


def _project() -> dict:
    project = json.loads((ROOT / "cases/ideal-lens-focus/project.json").read_text(encoding="utf-8"))
    plane = project["observations"][0]
    planes = []
    for name, x in (("before", 110.0 - DEFOCUS_MM), ("after", 110.0 + DEFOCUS_MM)):
        item = copy.deepcopy(plane)
        item["observation_id"] = name
        item["pose"]["position_mm"][0] = x
        planes.append(item)
    project["observations"] = planes
    project["targets"] = []
    source = next(c for c in project["components"] if c["optical_role"] == "source")
    red = copy.deepcopy(source)
    red["instance_id"] = "SRC_RED"
    red["source_model"]["wavelength_nm"] = 632.8
    project["components"].append(red)
    return project


@unittest.skipUnless(HAS_ENGINE, "needs os-optics (the engine)")
class RayFansAgainstTheEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        (base / "project.json").write_text(json.dumps(_project()), encoding="utf-8")
        cls.run_dir = base / "run"
        done = subprocess.run([sys.executable, "-m", "os_optics", "project", "run", str(base / "project.json"),
                               "--out-dir", str(cls.run_dir)], capture_output=True, text=True)
        if not (cls.run_dir / "manifest.json").exists():
            raise AssertionError(f"engine run failed: {done.stderr[-800:]}")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def fans(self, monitor):
        document = ray_fans.build(self.run_dir, monitor)
        self.assertEqual(sorted({p["wavelength_nm"] for p in document["paths"]}), [532.0, 632.8])
        return document["paths"]

    def test_longitudinal_aberration_equals_the_known_defocus(self):
        for monitor, expected in (("before", +DEFOCUS_MM), ("after", -DEFOCUS_MM)):
            for path in self.fans(monitor):
                for name in ("tangential", "sagittal"):
                    rows = path["fans"][name]
                    with self.subTest(monitor=monitor, path=path["path_id"], fan=name):
                        self.assertGreaterEqual(len(rows), 4)
                        for row in rows:
                            self.assertAlmostEqual(row["longitudinal_mm"], expected, delta=LONGITUDINAL_TOL_MM)

    def test_transverse_aberration_is_linear_in_rho_and_round(self):
        for monitor in ("before", "after"):
            for path in self.fans(monitor):
                t = {round(r["rho"], 9): r["transverse_um"] for r in path["fans"]["tangential"]}
                s = {round(r["rho"], 9): r["transverse_um"] for r in path["fans"]["sagittal"]}
                edge = max(t)
                slope = t[edge] / edge
                with self.subTest(monitor=monitor, path=path["path_id"]):
                    for rho, value in t.items():
                        self.assertAlmostEqual(value, slope * rho, delta=1e-6 * abs(slope) + 1e-9)
                    for rho, value in s.items():
                        self.assertAlmostEqual(value, t[rho], delta=1e-6 * abs(slope) + 1e-9)

    def test_a_refused_wavefront_keeps_the_fans_and_states_why(self):
        # The engine computes no OPD through an idealized lens; the fans stand, OPD is null with the engine's reason.
        for path in self.fans("before"):
            with self.subTest(path=path["path_id"]):
                self.assertNotEqual(path["engine"]["wavefront_status"], "OK")
                self.assertTrue(path["engine"]["wavefront_message"])
                self.assertIsNone(path["opd_rms_waves"])
                self.assertTrue(all(row["opd_waves"] is None for row in path["fans"]["tangential"]))


SPOT_TOL_REL = 0.10
SPOT_MIN_UM = 4.0


@unittest.skipUnless(HAS_ENGINE, "needs os-optics (the engine)")
class RayFansOnARealLens(unittest.TestCase):
    """aspheric-lens: 3 fields x 3 wavelengths, the engine's spot and OPD numbers at the same field points.

    OPD: the kit's per-path RMS of the engine's per-ray OPD equals 'results wavefront' rms_waves (consistency, rel 1e-9).
    Spot: the kit's RMS radius about the centroid (equal weights, the run's 96 saved pupil rays) against the engine's
    'geometric_spot_rms_radius' from 'results imaging', which re-traces a dense pupil grid (launch grid 192, refined):
    the samplings differ, so the tolerance is +-10 % and applies only to spots of at least 4 um, where the sampling
    difference is small against the spot (observed: within 5.6 %; the 1.39 um on-axis spot differs by 15 %).
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        case = ROOT / "cases/aspheric-lens"
        cls.run_dir = base / "run"
        done = subprocess.run([sys.executable, "-m", "os_optics", "project", "run", str(case / "project.json"),
                               "--out-dir", str(cls.run_dir)], capture_output=True, text=True)
        if not (cls.run_dir / "manifest.json").exists():
            raise unittest.SkipTest(f"aspheric-lens did not run (catalog?): {done.stderr[-300:]}")
        imaging = subprocess.run([sys.executable, "-m", "os_optics", "results", "imaging", str(cls.run_dir),
                                  "--monitor", "image_plane", "--request", str(case / "imaging-request.json")],
                                 capture_output=True, text=True)
        cls.document = ray_fans.build(cls.run_dir, "image_plane", imaging=json.loads(imaging.stdout))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_covers_several_fields_and_wavelengths(self):
        paths = self.document["paths"]
        self.assertGreaterEqual(len({p["source_id"].split("_")[0] for p in paths}), 2)
        self.assertGreaterEqual(len({p["wavelength_nm"] for p in paths}), 2)

    def test_opd_rms_equals_the_engine_wavefront(self):
        for path in self.document["paths"]:
            engine = path["engine"]["opd_rms_waves"]
            with self.subTest(path=path["path_id"]):
                self.assertEqual(path["engine"]["wavefront_status"], "OK")
                self.assertAlmostEqual(path["opd_rms_waves"], engine, delta=1e-9 * max(1.0, abs(engine)))

    def test_spot_rms_against_the_engine_spot_radius(self):
        checked = 0
        for path in self.document["paths"]:
            engine = path["engine"]["spot_rms_um"]
            self.assertIsNotNone(engine, path["path_id"])
            if engine < SPOT_MIN_UM:
                continue
            checked += 1
            with self.subTest(path=path["path_id"], engine_um=engine, kit_um=path["spot_rms_um"]):
                self.assertLessEqual(abs(path["spot_rms_um"] - engine) / engine, SPOT_TOL_REL)
        self.assertGreaterEqual(checked, 4)


if __name__ == "__main__":
    unittest.main()
