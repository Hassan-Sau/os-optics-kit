"""Ray fans from published ray rows: geometry on synthetic rays, no engine needed."""

from __future__ import annotations

import math
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

    def test_engine_spot_radius_is_read_per_source(self):
        imaging = {"quantities": [
            {"quantity_id": "S#0.geometric_spot_rms_radius", "state": "KNOWN", "value": 5.0, "scope": {"source_id": "S"}},
            {"quantity_id": "T#0.geometric_spot_rms_radius", "state": "UNAVAILABLE", "value": None,
             "scope": {"source_id": "T"}}]}
        self.assertEqual(ray_fans.engine_spot_rms(imaging), {"S": 5.0})


if __name__ == "__main__":
    unittest.main()
