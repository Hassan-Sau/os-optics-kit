"""Ray fans against the engine on analytic cases, and a real lens against itself turned about its axis.

Ideal thin lens (f = 100 mm at x = 10, focus plane x = 110), collimated Gaussian sources (1/e^2 radius 1 mm at the
source, so rho = launch offset in mm). Every ray of a field meets at one point of the focal plane. On a plane at
x = 110 - D every fan ray therefore crosses the chief ray D behind the plane (longitudinal aberration +D, on a plane
facing the light or facing the source alike; on a plane D after focus, -D: the crossing lies before it), and a ray at pupil coordinate rho lies D / f rho mm from the chief ray
along the sagittal direction and D / f rho / cos(theta) along the tangential one (the beam's footprint on the lens is
stretched by 1 / cos(theta) in the field direction). This holds at every field azimuth and for any roll of the source
about its own axis.

The real lens (cases/aspheric-lens, rotationally symmetric) at one field along -v and the same field turned by 20 deg
about the optical axis: no pupil ray of the turned field lies on its fan azimuths, so its fans come from the ring fit;
they must equal the sampled fans of the unturned field.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import math
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

FOCAL_MM = 100.0
LENS_X = 10.0
FOCUS_X = LENS_X + FOCAL_MM
BEFORE_MM = 2.0  # the 'before' plane: 2 mm before focus; the 'after' plane: 2 mm after it
FLIPPED_MM = 5.0  # the 'flipped' and 'decentred' planes: 5 mm before focus
FIELD_DEG = 3.0
LONGITUDINAL_TOL_MM = 1e-6
TRANSVERSE_TOL_UM = 1e-6


def _qmul(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return [aw * bw - ax * bx - ay * by - az * bz, aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx, aw * bz + ax * by - ay * bx + az * bw]


def _about(axis, deg):
    half = math.radians(deg) / 2.0
    return [math.cos(half)] + [a * math.sin(half) for a in axis]


def _run(project: dict, base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    (base / "project.json").write_text(json.dumps(project), encoding="utf-8")
    run_dir = base / "run"
    done = subprocess.run([sys.executable, "-m", "os_optics", "project", "run", str(base / "project.json"),
                           "--out-dir", str(run_dir)], capture_output=True, text=True)
    if not (run_dir / "manifest.json").exists():
        raise AssertionError(f"engine run failed: {done.stderr[-800:]}")
    return run_dir


def _ideal_project() -> dict:
    """The ideal lens with fields at several azimuths and rolls, three planes before focus and one after it."""
    project = json.loads((ROOT / "cases/ideal-lens-focus/project.json").read_text(encoding="utf-8"))
    project["targets"] = []
    source = next(c for c in project["components"] if c["optical_role"] == "source")
    base_q = source["nominal_pose"]["quaternion_wxyz"]
    centre = next(c for c in project["components"] if c["optical_role"] == "lens")["nominal_pose"]["position_mm"]

    def field(name, azimuth_deg=None, roll_deg=0.0, theta_deg=FIELD_DEG, wavelength_nm=None, away=False):
        item = copy.deepcopy(source)
        item["instance_id"] = name
        q = _qmul(_about((1.0, 0.0, 0.0), roll_deg), base_q)  # roll about the beam (world +x) first
        if azimuth_deg is not None:
            # tilt the beam by theta towards the plane direction cos(az) u + sin(az) v (u = +y, v = +z)
            a = math.radians(azimuth_deg)
            q = _qmul(_about((0.0, -math.sin(a), math.cos(a)), theta_deg), q)
        if away:
            q = _qmul(_about((0.0, 0.0, 1.0), 180.0), q)
        item["nominal_pose"]["quaternion_wxyz"] = q
        axis = ray_fans._rotate(q, (0.0, 0.0, 1.0))
        item["nominal_pose"]["position_mm"] = [centre[k] - 10.0 * axis[k] for k in range(3)]
        if wavelength_nm:
            item["source_model"]["wavelength_nm"] = wavelength_nm
        return item

    project["components"] = [c for c in project["components"] if c["optical_role"] != "source"] + [
        field("AX"), field("RED", wavelength_nm=632.8),
        field("F0", 0.0), field("F20", 20.0), field("F45", 45.0), field("F90", 90.0),
        field("R30", 90.0, roll_deg=30.0), field("R90", 90.0, roll_deg=90.0),
        field("FAR", 90.0, theta_deg=30.0), field("AWAY", away=True)]
    plane = project["observations"][0]
    plane["aperture"]["radius_mm"] = 20.0
    before = copy.deepcopy(plane)
    before["observation_id"] = "before"
    before["pose"]["position_mm"][0] = FOCUS_X - BEFORE_MM
    flipped = copy.deepcopy(plane)
    flipped["observation_id"] = "flipped"
    flipped["pose"]["position_mm"][0] = FOCUS_X - FLIPPED_MM
    flipped["pose"]["quaternion_wxyz"] = _qmul(_about((0.0, 0.0, 1.0), 180.0), plane["pose"]["quaternion_wxyz"])
    decentred = copy.deepcopy(plane)
    decentred["observation_id"] = "decentred"
    decentred["pose"]["position_mm"] = [FOCUS_X - FLIPPED_MM, 0.0094, plane["pose"]["position_mm"][2] + 0.0034]
    after = copy.deepcopy(plane)
    after["observation_id"] = "after"
    after["pose"]["position_mm"][0] = FOCUS_X + BEFORE_MM
    project["observations"] = [before, flipped, decentred, after]
    return project


@unittest.skipUnless(HAS_ENGINE, "needs os-optics (the engine)")
class RayFansOfAnIdealLens(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.run_dir = _run(_ideal_project(), Path(cls.tmp.name) / "ideal")
        cls.collected = ray_fans.collect(cls.run_dir, "before")
        cls.document = ray_fans.assemble(cls.collected)
        cls.paths = {p["source_id"]: p for p in cls.document["paths"]}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def assert_closed_form(self, path, distance_mm, theta_deg):
        for name in ("tangential", "sagittal"):
            rows = path["fans"][name]
            stretch = 1.0 / math.cos(math.radians(theta_deg)) if name == "tangential" else 1.0
            with self.subTest(path=path["path_id"], fan=name):
                self.assertEqual(len(rows), 16)  # 8 rings, both sides of the chief ray
                self.assertAlmostEqual(max(abs(r["rho"]) for r in rows), path["rho_max"], places=12)
                for row in rows:
                    self.assertAlmostEqual(row["longitudinal_mm"], distance_mm, delta=LONGITUDINAL_TOL_MM)
                    expected = 1000.0 * distance_mm / FOCAL_MM * row["rho"] * stretch
                    self.assertAlmostEqual(row["transverse_um"], expected, delta=TRANSVERSE_TOL_UM)

    def test_fields_at_every_azimuth_give_full_fans_with_the_closed_form_values(self):
        for name in ("F0", "F20", "F45", "F90"):
            path = self.paths[name]
            with self.subTest(field=name):
                self.assertEqual(path["rings"]["complete"], list(range(8)))
                self.assert_closed_form(path, BEFORE_MM, FIELD_DEG)
        # 20 deg is off the sampling grid: no ray lies on a fan azimuth, every point comes from the ring fit
        self.assertTrue(all(not r["sampled"] for r in self.paths["F20"]["fans"]["tangential"]))

    def test_the_fans_reach_the_outermost_ring_that_arrived(self):
        rows = [r for r in self.collected["rays"]["AX#0"] if not r["chief"]]
        outermost = max(math.hypot(r["launch_offset_x"], r["launch_offset_y"]) for r in rows)
        path = self.paths["AX"]
        self.assertAlmostEqual(path["rho_max"], outermost, places=9)
        self.assertEqual(path["vignetted_rays"], 0)
        self.assertEqual((path["rings"]["rays_per_ring"], path["rings"]["rings_launched"]), (12, 8))
        for name in ("tangential", "sagittal"):
            self.assertAlmostEqual(max(r["rho"] for r in path["fans"][name]), outermost, places=9)

    def test_on_axis_tangential_and_sagittal_fans_agree_on_every_ring(self):
        for name in ("AX", "RED"):
            path = self.paths[name]
            tangential = {round(r["rho"], 9): r for r in path["fans"]["tangential"]}
            sagittal = {round(r["rho"], 9): r for r in path["fans"]["sagittal"]}
            with self.subTest(path=name):
                self.assertEqual(sorted({r["zone"] for r in tangential.values()}), list(range(8)))
                self.assertEqual(sorted(tangential), sorted(sagittal))
                for rho, row in tangential.items():
                    self.assertAlmostEqual(row["transverse_um"], sagittal[rho]["transverse_um"], delta=1e-9)
                self.assert_closed_form(path, BEFORE_MM, 0.0)
        self.assertEqual(sorted({p["wavelength_nm"] for p in self.document["paths"]}), [532.0, 632.8])

    def test_a_source_rolled_about_its_axis_gives_the_same_fans(self):
        reference = self.paths["F90"]
        for name, roll in (("R30", 30.0), ("R90", 90.0)):
            path = self.paths[name]
            with self.subTest(roll=roll):
                for fan in ("tangential", "sagittal"):
                    self.assertEqual(len(path["fans"][fan]), len(reference["fans"][fan]))
                    for want, got in zip(reference["fans"][fan], path["fans"][fan]):
                        self.assertAlmostEqual(got["rho"], want["rho"], delta=1e-12)
                        self.assertAlmostEqual(got["transverse_um"], want["transverse_um"], delta=TRANSVERSE_TOL_UM)
                        self.assertAlmostEqual(got["longitudinal_mm"], want["longitudinal_mm"],
                                               delta=LONGITUDINAL_TOL_MM)
                # the roll seen in the plane's (u, v); the 3 deg tilt after the roll adds a second-order turn
                self.assertAlmostEqual(path["pupil_frame"]["rotation_deg"], roll, delta=1e-2)

    def test_a_plane_facing_the_source_keeps_the_sign_and_a_decentred_plane_keeps_the_fans(self):
        for monitor in ("flipped", "decentred"):
            document = ray_fans.build(self.run_dir, monitor, paths=["AX#0"])
            path = document["paths"][0]
            with self.subTest(monitor=monitor):
                self.assertEqual(path["light_along_normal"], monitor != "flipped")
                self.assertEqual(path["pupil_frame"]["mirrored"], monitor == "flipped")
                self.assert_closed_form(path, FLIPPED_MM, 0.0)

    def test_a_plane_after_focus_gives_a_negative_longitudinal_aberration(self):
        # the rays have crossed the chief ray 2 mm before the plane: longitudinal -2 mm, transverse sign reversed
        paths = {p["source_id"]: p for p in ray_fans.build(self.run_dir, "after")["paths"]}
        for name, theta in (("AX", 0.0), ("F0", FIELD_DEG), ("F20", FIELD_DEG), ("F90", FIELD_DEG)):
            path = paths[name]
            with self.subTest(field=name):
                self.assertTrue(path["light_along_normal"])
                self.assert_closed_form(path, -BEFORE_MM, theta)
                outer = max(path["fans"]["tangential"], key=lambda row: row["rho"])
                self.assertLess(outer["transverse_um"], 0.0)
                self.assertTrue(all(row["longitudinal_mm"] < 0.0 for rows in path["fans"].values() for row in rows))

    def test_zones_keep_rings_that_carry_fan_points(self):
        for zones in (1, 2, 3, 4, 8, 100):
            path = next(p for p in ray_fans.assemble(self.collected, zones)["paths"] if p["source_id"] == "F20")
            with self.subTest(zones=zones):
                for name in ("tangential", "sagittal"):
                    listed = path["zones"][name]
                    self.assertEqual(len(listed), min(zones, 8))
                    self.assertIn(7, listed)
                    self.assertEqual(sorted({r["zone"] for r in path["fans"][name]}), listed)

    def test_paths_that_do_not_reach_the_plane_are_listed_with_their_termination(self):
        unreached = {u["source_id"]: u for u in self.document["unreached"]}
        self.assertEqual(sorted(unreached), ["AWAY", "FAR"])
        self.assertEqual(unreached["FAR"]["reason"], "NO_COUNTED_RAYS_AT_MONITOR")
        self.assertEqual(unreached["AWAY"]["reason"], "NO_RAYS_AT_MONITOR")
        self.assertEqual(unreached["AWAY"]["termination"], "ESCAPED")
        declared = {c["instance_id"] for c in json.loads((self.run_dir / "project.json").read_text(encoding="utf-8"))
                    ["components"] if c["optical_role"] == "source"}
        self.assertEqual(declared, set(self.paths) | set(unreached))
        self.assertEqual(self.document["refused"], [])

    def test_a_refused_wavefront_keeps_the_fans_and_the_engine_reason(self):
        direct = subprocess.run([sys.executable, "-m", "os_optics", "results", "wavefront", str(self.run_dir),
                                 "--monitor", "before", "--path", "AX#0"], capture_output=True, text=True)
        self.assertEqual(direct.returncode, 2)
        engine = json.loads(direct.stdout)
        path = self.paths["AX"]
        self.assertEqual(path["engine"]["wavefront_status"], "UNAVAILABLE")
        self.assertEqual(path["engine"]["wavefront_reason"], "RAY_OPL_UNDEFINED")
        self.assertEqual(path["engine"]["wavefront_message"], engine["message"])
        self.assertIsNone(path["opd_rms_waves"])
        self.assertTrue(all(row["opd_waves"] is None for row in path["fans"]["tangential"]))

    def test_command_line_errors_are_json_with_exit_4(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ray_fans.main([str(self.run_dir), "--monitor", "before", "--path", "AWAY#0"])
        self.assertEqual(code, 4)
        error = json.loads(err.getvalue())["error"]
        self.assertEqual(error["code"], "PATH_NOT_FOUND")
        self.assertIn("ESCAPED", error["message"])
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = ray_fans.main([str(self.run_dir), "--monitor", "nowhere"])
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(err.getvalue())["error"]["engine_code"], "MONITOR_NOT_FOUND")


@unittest.skipUnless(HAS_ENGINE, "needs os-optics (the engine)")
class RayFansWithVignettingAndFolds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        project = json.loads((ROOT / "cases/ideal-lens-focus/project.json").read_text(encoding="utf-8"))
        project["targets"] = []
        plane = project["observations"][0]
        plane["observation_id"] = "before"
        plane["pose"]["position_mm"][0] = FOCUS_X - BEFORE_MM
        open_run = _run(project, base / "open")
        clipped = copy.deepcopy(project)
        clipped["optical_models"][0]["model"]["clear_radius_mm"] = "0.8"
        clipped_run = _run(clipped, base / "clipped")
        cls.open = ray_fans.build(open_run, "before")["paths"][0]
        cls.clipped = ray_fans.build(clipped_run, "before")["paths"][0]
        mirror = json.loads((ROOT / "cases/mirror-alignment/project.json").read_text(encoding="utf-8"))
        mirror["targets"] = []
        cls.mirror_run = _run(mirror, base / "mirror")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_vignetting_keeps_the_pupil_scale_and_is_counted(self):
        self.assertGreater(self.clipped["vignetted_rays"], 0)
        self.assertEqual(self.clipped["pupil_rays_launched"] - self.clipped["pupil_rays_arrived"],
                         self.clipped["vignetted_rays"])
        self.assertLess(self.clipped["rho_max"], self.open["rho_max"])
        open_rows = {(r["zone"], round(r["rho"], 12)): r for r in self.open["fans"]["tangential"]}
        self.assertTrue(self.clipped["fans"]["tangential"])
        for row in self.clipped["fans"]["tangential"]:
            want = open_rows[(row["zone"], round(row["rho"], 12))]  # same ring, same rho
            self.assertAlmostEqual(row["transverse_um"], want["transverse_um"], delta=TRANSVERSE_TOL_UM)

    def test_a_reflected_path_is_refused_with_the_reason(self):
        with self.assertRaises(ray_fans.FanError) as caught:
            ray_fans.build(self.mirror_run, "near", paths=["SRC#0"])
        self.assertEqual(caught.exception.code, "PUPIL_FRAME_UNRESOLVED")
        self.assertIn("reflected", str(caught.exception))
        with self.assertRaises(ray_fans.FanError) as caught:
            ray_fans.build(self.mirror_run, "near")
        self.assertEqual(caught.exception.code, "NO_FANS")
        self.assertIn("PUPIL_FRAME_UNRESOLVED", str(caught.exception))


@unittest.skipUnless(HAS_ENGINE, "needs os-optics (the engine)")
class RayFansWhenEveryRingIsVignetted(unittest.TestCase):
    """The ideal lens clipped to a 0.8 mm clear radius, with 3 deg fields shifted 0.7 mm across it: every ring of the
    pupil loses rays at the lens. At field azimuth 0 deg rays still lie on the fan azimuths; at 20 deg none does, and
    with no complete ring to fit neither fan has a point: that path is refused, never returned with empty fans."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        project = json.loads((ROOT / "cases/ideal-lens-focus/project.json").read_text(encoding="utf-8"))
        project["targets"] = []
        project["optical_models"][0]["model"]["clear_radius_mm"] = "0.8"
        source = next(c for c in project["components"] if c["optical_role"] == "source")
        centre = next(c for c in project["components"] if c["optical_role"] == "lens")["nominal_pose"]["position_mm"]

        def field(name, azimuth_deg):
            item = copy.deepcopy(source)
            item["instance_id"] = name
            a = math.radians(azimuth_deg)
            q = _qmul(_about((0.0, -math.sin(a), math.cos(a)), FIELD_DEG), source["nominal_pose"]["quaternion_wxyz"])
            item["nominal_pose"]["quaternion_wxyz"] = q
            axis = ray_fans._rotate(q, (0.0, 0.0, 1.0))
            item["nominal_pose"]["position_mm"] = [centre[k] - 10.0 * axis[k] + (0.0, 0.7, 0.0)[k] for k in range(3)]
            return item

        project["components"] = [c for c in project["components"] if c["optical_role"] != "source"] + [
            field("A0", 0.0), field("A20", 20.0)]
        plane = project["observations"][0]
        plane["observation_id"] = "before"
        plane["aperture"]["radius_mm"] = 20.0
        plane["pose"]["position_mm"][0] = FOCUS_X - 5.0
        cls.run_dir = _run(project, Path(cls.tmp.name) / "vignetted")
        cls.collected = ray_fans.collect(cls.run_dir, "before")
        cls.document = ray_fans.assemble(cls.collected)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_path_whose_fans_have_no_point_is_refused_with_the_reason(self):
        self.assertEqual([p["source_id"] for p in self.document["paths"]], ["A0"])
        refused = self.document["refused"]
        self.assertEqual([(r["source_id"], r["code"]) for r in refused], [("A20", "FAN_AXIS_NOT_SAMPLED")])
        details = refused[0]["details"]
        self.assertEqual(details["fans_without_points"], ["tangential", "sagittal"])
        self.assertEqual(details["rings"]["complete"], [])
        self.assertEqual(details["rings"]["partial"], list(range(8)))
        self.assertEqual(details["rings"]["rays_per_ring"], 12)
        self.assertGreater(min(details["nearest_ray_deg"].values()), 1.0)
        with self.assertRaises(ray_fans.FanError) as caught:
            ray_fans.build(self.run_dir, "before", paths=["A20#0"])
        self.assertEqual(caught.exception.code, "FAN_AXIS_NOT_SAMPLED")
        with self.assertRaises(ray_fans.FanError) as caught:
            ray_fans.assemble(dict(self.collected, selected=["A20#0"]))
        self.assertEqual(caught.exception.code, "NO_FANS")
        self.assertIn("A20#0: FAN_AXIS_NOT_SAMPLED", str(caught.exception))

    def test_rays_per_ring_are_the_engine_sampling_not_the_last_position_that_arrived(self):
        path = self.document["paths"][0]
        pupil = [r for r in self.collected["rays"]["A0#0"] if not r["chief"]]
        self.assertLess(max(r["ring_position_index"] for r in pupil), 11)  # the last positions of every ring are lost
        self.assertEqual((path["rings"]["rays_per_ring"], path["rings"]["rings_launched"]), (12, 8))
        self.assertEqual(path["pupil_rays_launched"], 96)
        self.assertEqual(path["rings"]["complete"], [])
        # both fans have points; the sagittal one stops at a smaller rho than the tangential one, and fan_rho_max says so
        self.assertTrue(path["fans"]["tangential"] and path["fans"]["sagittal"])
        for name in ("tangential", "sagittal"):
            self.assertEqual(path["fan_rho_max"][name], max(abs(r["rho"]) for r in path["fans"][name]))
        self.assertLess(path["fan_rho_max"]["sagittal"], path["fan_rho_max"]["tangential"])


TURN_DEG = 20.0


@unittest.skipUnless(HAS_ENGINE, "needs os-optics (the engine)")
class RayFansOfARealLensTurnedAboutItsAxis(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        project = json.loads((ROOT / "cases/aspheric-lens/project.json").read_text(encoding="utf-8"))
        project["targets"] = []
        field = next(c for c in project["components"] if c["instance_id"] == "SRC1_1")  # 12.5 deg, 587.6 nm
        turned = copy.deepcopy(field)
        turned["instance_id"] = "TURNED"
        turned["nominal_pose"]["quaternion_wxyz"] = _qmul(_about((1.0, 0.0, 0.0), TURN_DEG),
                                                          field["nominal_pose"]["quaternion_wxyz"])
        x, y, z = field["nominal_pose"]["position_mm"]
        a = math.radians(TURN_DEG)
        turned["nominal_pose"]["position_mm"] = [x, y * math.cos(a) - z * math.sin(a), y * math.sin(a) + z * math.cos(a)]
        project["components"] = [c for c in project["components"] if c["optical_role"] != "source"] + [field, turned]
        run_dir = _run(project, Path(cls.tmp.name) / "turned")
        cls.paths = {p["source_id"]: p for p in ray_fans.build(run_dir, "image_plane")["paths"]}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_fitted_fans_equal_the_sampled_fans_of_the_same_field(self):
        sampled, fitted = self.paths["SRC1_1"], self.paths["TURNED"]
        self.assertAlmostEqual(math.hypot(*fitted["chief_uv_mm"]), math.hypot(*sampled["chief_uv_mm"]), delta=1e-9)
        for name in ("tangential", "sagittal"):
            self.assertTrue(any(r["sampled"] for r in sampled["fans"][name]))
            self.assertTrue(all(not r["sampled"] for r in fitted["fans"][name]))
            self.assertEqual(len(fitted["fans"][name]), 16)
            for want, got in zip(sampled["fans"][name], fitted["fans"][name]):
                with self.subTest(fan=name, zone=got["zone"], rho=got["rho"]):
                    self.assertAlmostEqual(got["rho"], want["rho"], delta=1e-9)
                    self.assertLess(got["fit_residual_um"], 1e-3)
                    self.assertAlmostEqual(got["transverse_um"], want["transverse_um"], delta=1e-3)
                    self.assertAlmostEqual(got["longitudinal_mm"], want["longitudinal_mm"], delta=1e-4)
                    self.assertAlmostEqual(got["opd_waves"], want["opd_waves"], delta=1e-5)
        # the real lens has aberrations: the tangential fan is not linear in rho (a linear fan would test nothing)
        t = {round(r["rho"], 6): r["transverse_um"] for r in sampled["fans"]["tangential"]}
        self.assertGreater(abs(t[max(t)] + t[min(t)]), 1.0)


def _native(project_file: Path, class_id: str, instance_id: str, position: str, params: list[str]) -> None:
    """Add an 'os-optics native build' component to a Project file, along +x (local z -> world +x)."""
    argv = [sys.executable, "-m", "os_optics", "native", "build", class_id, "--into", str(project_file),
            "--instance-id", instance_id, "--position", position, "--quaternion", "0.5,0.5,0.5,0.5"]
    for param in params:
        argv += ["--param", param]
    done = subprocess.run(argv, capture_output=True, text=True)
    if done.returncode != 0:
        raise AssertionError(f"native build {class_id} failed: {done.stdout[-400:]} {done.stderr[-400:]}")


def _imaging(run_dir: Path, base: Path, request: dict) -> dict:
    (base / "imaging-request.json").write_text(json.dumps(request), encoding="utf-8")
    done = subprocess.run([sys.executable, "-m", "os_optics", "results", "imaging", str(run_dir), "--monitor", "image",
                           "--request", str(base / "imaging-request.json")], capture_output=True, text=True)
    if done.returncode != 0:
        raise AssertionError(f"results imaging failed: {done.stderr[-800:]}")
    return json.loads(done.stdout)


@unittest.skipUnless(HAS_ENGINE, "needs os-optics (the engine)")
class EngineSpotOfTheSameLight(unittest.TestCase):
    """The engine's imaging spot beside the fans only where it is of the same plane crossing, route and run.

    Folded double pass: a 3 mm stop at x = 2 (it bounds the imaging pupil), the plane 'image' at x = 5, the ideal lens
    (f = 100 mm) at x = 10 and a flat mirror at x = 35 facing back: the path crosses the plane on the way out
    (crossing 0, unreflected: the fans) and again converging after the mirror (crossing 1). 'results imaging' reads the
    last crossing unless its request states 'crossing_index': 0.
    Two routes: a calcite-like beam displacer (o / e indices 1.6557 / 1.4849, 2 mm walk-off) before the lens splits the
    line, polarized at 30 deg, into two unreflected paths to the plane, SRC#0 (ordinary) and SRC#0.0 (extraordinary);
    the imaging analysis reads one route per line, the strongest at the plane.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        lens = json.loads((ROOT / "cases/ideal-lens-focus/project.json").read_text(encoding="utf-8"))
        mirror_case = json.loads((ROOT / "cases/mirror-alignment/project.json").read_text(encoding="utf-8"))

        fold = copy.deepcopy(lens)
        fold["targets"] = []
        mirror = copy.deepcopy(next(c for c in mirror_case["components"] if c["optical_role"] == "mirror"))
        mirror["instance_id"] = "M"
        # the mirror's face normal (its local +z) along world -x, back towards the lens
        mirror["nominal_pose"] = {"position_mm": [35.0, 0.0, 20.0], "quaternion_wxyz": [0.5, -0.5, -0.5, 0.5]}
        fold["components"].append(mirror)
        fold["optical_models"] += mirror_case["optical_models"]
        fold["model_coatings"] = mirror_case["model_coatings"]
        plane = fold["observations"][0]
        plane["observation_id"] = "image"
        plane["pose"]["position_mm"] = [5.0, 0.0, 20.0]
        plane["aperture"]["radius_mm"] = 20.0
        (base / "fold").mkdir()
        (base / "fold" / "project.json").write_text(json.dumps(fold), encoding="utf-8")
        _native(base / "fold" / "project.json", "aperture.circular", "STOP", "2,0,20",
                ["opening_diameter_mm=3", "plate_diameter_mm=25"])
        cls.fold_run = _run(json.loads((base / "fold" / "project.json").read_text(encoding="utf-8")), base / "fold")

        split = copy.deepcopy(lens)
        split["targets"] = []
        next(c for c in split["components"] if c["optical_role"] == "source")["source_model"]["polarization"][
            "angle_deg"] = 30.0
        next(c for c in split["components"] if c["optical_role"] == "lens")["nominal_pose"]["position_mm"] = [
            40.0, 0.0, 20.0]
        plane = split["observations"][0]
        plane["observation_id"] = "image"
        plane["pose"]["position_mm"] = [120.0, 0.0, 20.0]
        plane["aperture"]["radius_mm"] = 20.0
        (base / "split").mkdir()
        (base / "split" / "project.json").write_text(json.dumps(split), encoding="utf-8")
        _native(base / "split" / "project.json", "splitter.beam_displacer", "BD", "5,0,20",
                ['material={"ordinary": {"constant_index": "1.6557"}, "extraordinary": {"constant_index": "1.4849"}, '
                 '"valid_band_nm": [400, 1100]}', "design_wavelength_nm=532", "displacement_mm=2", "width_mm=10",
                 "height_mm=10", 'ar_coating={"reflectance": "0", "valid_aoi_deg": [0, 30]}'])
        cls.split_run = _run(json.loads((base / "split" / "project.json").read_text(encoding="utf-8")), base / "split")

        cls.fold_last = _imaging(cls.fold_run, base / "fold", {})
        cls.fold_first = _imaging(cls.fold_run, base / "fold", {"crossing_index": 0})
        cls.split_imaging = _imaging(cls.split_run, base / "split", {})
        # the engine data of each run once, as build() collects it with an imaging document that states its run
        cls.fold = dict(ray_fans.collect(cls.fold_run, "image"), run_identity=ray_fans.load_run_identity(cls.fold_run))
        cls.split = dict(ray_fans.collect(cls.split_run, "image"),
                         run_identity=ray_fans.load_run_identity(cls.split_run))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @staticmethod
    def traced(imaging):
        return {key: (value["path_id"], value["crossing_index"]) for key, value in imaging["arrays"].items()
                if key.endswith(".rays")}

    @staticmethod
    def spot(imaging):
        return next(q for q in imaging["quantities"] if q["quantity_id"] == "SRC#0.geometric_spot_rms_radius")

    def test_a_later_crossing_of_a_folded_path_is_not_compared(self):
        self.assertEqual(self.traced(self.fold_last), {"SRC#0.rays": ("SRC#0", 1)})
        self.assertEqual(self.spot(self.fold_last)["state"], "KNOWN")
        document = ray_fans.assemble(self.fold, imaging=self.fold_last)
        path = document["paths"][0]
        self.assertEqual(path["path_id"], "SRC#0")
        self.assertEqual(len(path["fans"]["tangential"]), 16)
        self.assertIsNone(path["engine"]["spot_rms_um"])
        comparison = path["engine"]["spot_rms_comparison"]
        self.assertEqual((comparison["status"], comparison["reason"], comparison["crossing_index"]),
                         ("NOT_COMPARED", "IMAGING_CROSSING_MISMATCH", 1))
        self.assertEqual(document["imaging"]["run"]["status"], "MATCHED")
        self.assertEqual(document["imaging"]["run"]["run"], self.fold["run_identity"])

    def test_the_same_crossing_is_compared(self):
        self.assertEqual(self.traced(self.fold_first), {"SRC#0.rays": ("SRC#0", 0)})
        spot = self.spot(self.fold_first)
        self.assertEqual(spot["state"], "KNOWN")
        path = ray_fans.assemble(self.fold, imaging=self.fold_first)["paths"][0]
        self.assertEqual(path["engine"]["spot_rms_um"], spot["value"])
        self.assertEqual(path["engine"]["spot_rms_comparison"]["status"], "COMPARED")
        self.assertNotEqual(spot["value"], self.spot(self.fold_last)["value"])

    def test_another_route_is_not_compared(self):
        self.assertEqual(self.traced(self.split_imaging), {"SRC#0.rays": ("SRC#0", 0)})
        document = ray_fans.assemble(self.split, imaging=self.split_imaging)
        paths = {p["path_id"]: p for p in document["paths"]}
        self.assertEqual(sorted(paths), ["SRC#0", "SRC#0.0"])
        self.assertEqual(paths["SRC#0"]["engine"]["spot_rms_um"], self.spot(self.split_imaging)["value"])
        self.assertEqual(paths["SRC#0"]["engine"]["spot_rms_comparison"]["status"], "COMPARED")
        other = paths["SRC#0.0"]
        self.assertEqual(len(other["fans"]["tangential"]), 16)
        self.assertIsNone(other["engine"]["spot_rms_um"])
        self.assertEqual(other["engine"]["spot_rms_comparison"]["reason"], "IMAGING_PATH_MISMATCH")

    def test_a_document_of_another_run_is_not_compared(self):
        # the same source, wavelength, plane, path and crossing 0, but of the beam-displacer run
        document = ray_fans.assemble(self.fold, imaging=self.split_imaging)
        self.assertEqual(document["imaging"]["run"]["status"], "MISMATCHED")
        path = document["paths"][0]
        self.assertIsNone(path["engine"]["spot_rms_um"])
        self.assertEqual(path["engine"]["spot_rms_comparison"]["reason"], "IMAGING_RUN_MISMATCH")


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
