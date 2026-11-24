"""Ray fans from published ray rows: geometry on synthetic rays, no engine needed."""

from __future__ import annotations

import math
from pathlib import Path
import unittest

from os_optics_kit import ray_fans


def ray(index, x, y, u, v, du=0.0, dv=0.0, ring=0, chief=False):
    n = math.sqrt(max(0.0, 1.0 - du * du - dv * dv))
    return {"path_id": "P#0", "source_id": "P", "wavelength_nm": 550.0, "crossing_index": 0, "counted": True,
            "ray_index": index, "chief": chief, "ring_index": None if chief else ring,
            "launch_offset_x": x, "launch_offset_y": y, "power_weight": None if chief else 0.25,
            "u_mm": u, "v_mm": v, "direction_u": du, "direction_v": dv, "direction_n": n}


class RayFanTests(unittest.TestCase):
    def test_transverse_and_longitudinal_aberration_of_a_defocused_bundle(self):
        # Rays converge to the axis 2 mm behind the plane: a ray at height h on the plane has slope -h/2.
        rays = [ray(0, 0, 0, 0, 0, chief=True)]
        for i, (x, y) in enumerate(((0, 1), (0, -1), (1, 0), (-1, 0)), start=1):
            h_u, h_v = 0.01 * x, 0.01 * y
            rays.append(ray(i, x, y, h_u, h_v, du=-h_u / 2.0, dv=-h_v / 2.0))
        fan = ray_fans.path_fans(rays)
        tangential = fan["fans"]["tangential"]
        self.assertEqual([row["rho"] for row in tangential], [-1.0, 1.0])
        self.assertAlmostEqual(tangential[1]["transverse_um"], 10.0)
        for row in tangential + fan["fans"]["sagittal"]:
            self.assertAlmostEqual(row["longitudinal_mm"], 2.0, places=3)
        self.assertAlmostEqual(fan["spot_rms_um"], 10.0)

    def test_parallel_ray_has_no_longitudinal_aberration(self):
        rays = [ray(0, 0, 0, 0, 0, chief=True), ray(1, 0, 1, 0, 0.005)]
        row = ray_fans.path_fans(rays)["fans"]["tangential"][0]
        self.assertAlmostEqual(row["transverse_um"], 5.0)
        self.assertIsNone(row["longitudinal_mm"])

    def test_opd_comes_from_the_engine_wavefront_and_zones_are_thinned(self):
        rays = [ray(0, 0, 0, 0, 0, chief=True)]
        rays += [ray(i, 0, r, 0, 0.001 * r, ring=i - 1) for i, r in enumerate((0.25, 0.5, 0.75, 1.0), start=1)]
        wavefront = {"rays": [{"ray_index": i, "opd_waves": 0.1 * i} for i in range(1, 5)], "rms_waves": 0.11}
        fan = ray_fans.path_fans(rays, wavefront, zones=2)
        self.assertEqual(fan["zones"], [1, 3])
        self.assertEqual([row["opd_waves"] for row in fan["fans"]["tangential"]], [0.2, 0.4])
        self.assertEqual(fan["engine"]["opd_rms_waves"], 0.11)

    def test_missing_chief_or_pupil_rays_are_refused(self):
        with self.assertRaises(ray_fans.FanError) as caught:
            ray_fans.path_fans([ray(1, 0, 1, 0, 0)])
        self.assertEqual(caught.exception.code, "CHIEF_RAY_MISSING")
        with self.assertRaises(ray_fans.FanError) as caught:
            ray_fans.path_fans([ray(0, 0, 0, 0, 0, chief=True)])
        self.assertEqual(caught.exception.code, "PUPIL_RAYS_MISSING")

    def _coma_bundle(self, angle_deg):
        """An off-axis field at angle_deg in (u, v): coma along the field direction, chief ray tilted towards it."""
        a = math.radians(angle_deg)
        tu, tv = math.cos(a), math.sin(a)
        cu, cv, slope = 10.0 * tu, 10.0 * tv, 0.2
        rays = [ray(0, 0, 0, cu, cv, du=slope * tu, dv=slope * tv, chief=True)]
        index = 1
        for t, s in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            # pupil point t along the field direction, s across it (launch x/y share the plane's u/v frame)
            x, y = t * tu - s * tv, t * tv + s * tu
            coma = 0.004 * (t * t + s * s) + (0.002 * t if t else 0.0)  # tangential shift, asymmetric in t
            u, v = cu + coma * tu, cv + coma * tv
            rays.append(ray(index, x, y, u, v, du=slope * tu, dv=slope * tv))
            index += 1
        return rays

    def test_tangential_direction_follows_the_field_azimuth(self):
        for angle in (90.0, 0.0, 45.0, -135.0):
            with self.subTest(angle=angle):
                fan = ray_fans.path_fans(self._coma_bundle(angle))
                tangential = {round(row["rho"]): row["transverse_um"] for row in fan["fans"]["tangential"]}
                sagittal = {round(row["rho"]): row["transverse_um"] for row in fan["fans"]["sagittal"]}
                # tangential: coma makes the fan asymmetric; sagittal: symmetric shift, no transverse error across
                self.assertAlmostEqual(tangential[1], 6.0, places=6)
                self.assertAlmostEqual(tangential[-1], 2.0, places=6)
                self.assertAlmostEqual(sagittal[1], 0.0, places=6)
                self.assertAlmostEqual(sagittal[-1], 0.0, places=6)

    def test_tangential_axis_on_axis_and_off_axis(self):
        self.assertEqual(ray_fans.tangential_axis(0.0, 0.0), (0.0, 1.0))
        self.assertEqual(ray_fans.tangential_axis(3.0, 0.0), (1.0, 0.0))
        u, v = ray_fans.tangential_axis(-2.0, -2.0)
        self.assertAlmostEqual(u, -math.sqrt(0.5))
        self.assertAlmostEqual(v, -math.sqrt(0.5))

    def test_longitudinal_aberration_is_along_the_normal_with_a_tilted_chief_ray(self):
        # chief ray tilted by 0.3 in u; fan rays converge on it 4 mm behind the plane along the normal
        slope = 0.3
        rays = [ray(0, 0, 0, 5.0, 0.0, du=slope, chief=True)]
        for i, t in enumerate((1.0, -1.0), start=1):
            h = 0.02 * t
            rays.append(ray(i, t, 0, 5.0 + h, 0.0, du=slope - h / 4.0))
        row = next(r for r in ray_fans.path_fans(rays)["fans"]["tangential"] if r["rho"] > 0)
        self.assertAlmostEqual(row["transverse_um"], 20.0)
        n_chief = math.sqrt(1.0 - slope * slope)
        # slopes are du/dn: the crossing distance follows from them, not from the raw direction cosines
        self.assertAlmostEqual(row["longitudinal_mm"],
                               -0.02 / ((slope - 0.005) / math.sqrt(1 - (slope - 0.005) ** 2) - slope / n_chief),
                               places=9)
        self.assertGreater(row["longitudinal_mm"], 0.0)

    def test_engine_output_that_is_not_json_is_a_structured_error(self):
        import subprocess

        def runner(cmd, **_kwargs):
            return subprocess.CompletedProcess(cmd, 1, "Traceback (most recent call last): ...", "boom")

        with self.assertRaises(ray_fans.FanError) as caught:
            ray_fans.load_rays(Path("run"), "image_plane", runner)
        self.assertEqual(caught.exception.code, "ENGINE_RESULTS_FAILED")

    def test_engine_spot_radius_is_read_per_source(self):
        imaging = {"quantities": [
            {"quantity_id": "S#0.geometric_spot_rms_radius", "state": "KNOWN", "value": 5.0, "scope": {"source_id": "S"}},
            {"quantity_id": "T#0.geometric_spot_rms_radius", "state": "UNAVAILABLE", "value": None,
             "scope": {"source_id": "T"}}]}
        self.assertEqual(ray_fans.engine_spot_rms(imaging), {"S": 5.0})


if __name__ == "__main__":
    unittest.main()
