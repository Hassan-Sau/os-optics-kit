"""Ray fans from published ray rows: geometry on synthetic rays and the command line, no engine needed."""

from __future__ import annotations

import contextlib
import io
import json
import math
from pathlib import Path
import subprocess
import tempfile
import unittest

from os_optics_kit import ray_fans

IDENTITY = {"matrix": [[1.0, 0.0], [0.0, 1.0]]}
RING_RADII = (0.25, 0.5, 0.75, 1.0)
PER_RING = 12


def ray(index, x, y, u, v, du=0.0, dv=0.0, ring=0, chief=False, position=None, counted=True, forward=True):
    n = math.sqrt(max(0.0, 1.0 - du * du - dv * dv)) * (1.0 if forward else -1.0)
    return {"path_id": "P#0", "source_id": "P", "wavelength_nm": 550.0, "crossing_index": 0, "counted": counted,
            "ray_index": index, "chief": chief, "ring_index": None if chief else ring,
            "ring_position_index": None if chief else position,
            "launch_offset_x": x, "launch_offset_y": y, "power_weight": None if chief else 0.25,
            "u_mm": u, "v_mm": v, "direction_u": du, "direction_v": dv, "direction_n": n,
            "direction_x": n, "direction_y": du, "direction_z": dv}


def bundle(chief_uv=(0.0, 0.0), focus_mm=2.0, scale_mm=0.01, rings=RING_RADII, frame_deg=0.0, mirror=False,
           drop_rings=(), stagger=True):
    """A pupil of full rings (12 rays each, alternate rings turned by 15 deg) converging to one point focus_mm behind
    the plane. The launch offsets are given in a source frame turned by frame_deg against (u, v) (mirrored: v flipped)."""
    cu, cv = chief_uv
    rays = [ray(0, 0, 0, cu, cv, chief=True)]
    index = 1
    c, s = math.cos(math.radians(frame_deg)), math.sin(math.radians(frame_deg))
    for k, radius in enumerate(rings):
        for m in range(PER_RING):
            if k in drop_rings:
                index += 1
                continue
            a = 2.0 * math.pi * (m + (0.5 if stagger and k % 2 else 0.0)) / PER_RING
            pu, pv = radius * math.cos(a), radius * math.sin(a)  # pupil point in the plane's (u, v)
            # launch offsets in the source frame: p_uv = R(frame) p_launch (mirrored: then v flipped)
            qv = -pv if mirror else pv
            x, y = c * pu + s * qv, -s * pu + c * qv
            h_u, h_v = scale_mm * pu, scale_mm * pv
            # direction cosines whose slopes du/dn, dv/dn are exactly -h / focus_mm
            norm = math.sqrt(1.0 + (h_u / focus_mm) ** 2 + (h_v / focus_mm) ** 2)
            rays.append(ray(index, x, y, cu + h_u, cv + h_v, du=-h_u / focus_mm / norm, dv=-h_v / focus_mm / norm,
                            ring=k, position=m))
            index += 1
    return rays


def frame(deg=0.0, mirror=False):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    matrix = [[c, -s], [s, c]]
    if mirror:
        matrix = [[c, -s], [-s, -c]]
    return {"matrix": matrix}


def rows_by_rho(rows):
    return {round(row["rho"], 9): row for row in rows}


class RayFanGeometryTests(unittest.TestCase):
    def test_transverse_and_longitudinal_aberration_of_a_defocused_bundle(self):
        fan = ray_fans.path_fans(bundle(), IDENTITY)
        for name in ("tangential", "sagittal"):
            rows = fan["fans"][name]
            self.assertEqual(len(rows), 2 * len(RING_RADII))
            for row in rows:
                self.assertAlmostEqual(row["transverse_um"], 10.0 * row["rho"], places=9)
                self.assertAlmostEqual(row["longitudinal_mm"], 2.0, places=9)
        self.assertEqual(fan["rho_max"], 1.0)

    def test_fans_at_a_field_azimuth_off_the_sampling_grid_cover_every_ring(self):
        # chief image point at 20 deg: no sampled ray lies on a fan azimuth; every ring still gives fan points
        u, v = 5.0 * math.cos(math.radians(20.0)), 5.0 * math.sin(math.radians(20.0))
        fan = ray_fans.path_fans(bundle(chief_uv=(u, v)), IDENTITY)
        self.assertAlmostEqual(fan["tangential_axis_uv"][0], math.cos(math.radians(20.0)))
        for name in ("tangential", "sagittal"):
            rows = fan["fans"][name]
            self.assertEqual(sorted({row["zone"] for row in rows}), [0, 1, 2, 3])
            self.assertEqual(max(abs(row["rho"]) for row in rows), 1.0)
            for row in rows:
                self.assertFalse(row["sampled"])
                self.assertIsNone(row["ray_index"])
                self.assertLess(row["fit_residual_um"], 1e-9)
                self.assertAlmostEqual(row["transverse_um"], 10.0 * row["rho"], places=9)
                self.assertAlmostEqual(row["longitudinal_mm"], 2.0, places=9)

    def test_rays_on_a_fan_azimuth_are_used_as_sampled(self):
        fan = ray_fans.path_fans(bundle(), IDENTITY)
        sampled = {row["zone"] for row in fan["fans"]["tangential"] if row["sampled"]}
        interpolated = {row["zone"] for row in fan["fans"]["tangential"] if not row["sampled"]}
        self.assertEqual(sampled, {0, 2})  # even rings have rays at 90 and 270 deg
        self.assertEqual(interpolated, {1, 3})  # odd rings are turned by 15 deg
        for row in fan["fans"]["tangential"]:
            self.assertEqual(row["ray_index"] is not None, row["sampled"])

    def test_the_ring_fit_follows_a_coma_like_ring_and_states_its_residual(self):
        # transverse displacement with harmonics up to 2 (coma): reproduced exactly at any azimuth
        rays = [ray(0, 0, 0, 0.0, 0.0, chief=True)]
        for m in range(PER_RING):
            a = 2.0 * math.pi * (m + 0.5) / PER_RING
            x, y = math.cos(a), math.sin(a)
            du_mm = 0.003 * math.sin(2 * a)
            dv_mm = 0.003 * (2.0 + math.cos(2 * a))
            rays.append(ray(m + 1, x, y, du_mm, dv_mm, du=-0.01 * x, dv=-0.01 * y, ring=0, position=m))
        fan = ray_fans.path_fans(rays, IDENTITY)
        t = rows_by_rho(fan["fans"]["tangential"])  # tangential = +v at the origin: azimuths 90 and 270 deg
        self.assertAlmostEqual(t[1.0]["transverse_um"], 3.0, places=9)
        self.assertAlmostEqual(t[-1.0]["transverse_um"], 3.0, places=9)
        s = rows_by_rho(fan["fans"]["sagittal"])  # sagittal = +u: azimuths 0 and 180 deg
        self.assertAlmostEqual(s[1.0]["transverse_um"], 0.0, places=9)
        self.assertLess(t[1.0]["fit_residual_um"], 1e-9)

    def test_parallel_ray_has_no_longitudinal_aberration(self):
        # one ray on the tangential azimuth (+v) and one on the sagittal azimuth (+u), both parallel to the chief ray
        rays = [ray(0, 0, 0, 0, 0, chief=True), ray(1, 0, 1, 0, 0.005, ring=0, position=0),
                ray(2, 1, 0, 0.005, 0, ring=0, position=1)]
        row = ray_fans.path_fans(rays, IDENTITY)["fans"]["tangential"][0]
        self.assertAlmostEqual(row["transverse_um"], 5.0)
        self.assertIsNone(row["longitudinal_mm"])
        self.assertTrue(row["sampled"])

    def test_longitudinal_aberration_is_along_the_normal_with_a_tilted_chief_ray(self):
        # chief ray tilted by 0.3 in u; fan rays converge on it 4 mm behind the plane along the normal
        slope = 0.3
        rays = [ray(0, 0, 0, 5.0, 0.0, du=slope, chief=True)]
        for i, t in enumerate((1.0, -1.0), start=1):
            h = 0.02 * t
            rays.append(ray(i, t, 0, 5.0 + h, 0.0, du=slope - h / 4.0, ring=0, position=i))
            # and the sagittal pair (launch +-y, along v), so that both fans have points
            rays.append(ray(i + 2, 0, t, 5.0, h, du=slope, dv=-h / 4.0, ring=0, position=i + 2))
        row = next(r for r in ray_fans.path_fans(rays, IDENTITY)["fans"]["tangential"] if r["rho"] > 0)
        self.assertAlmostEqual(row["transverse_um"], 20.0)
        n_chief = math.sqrt(1.0 - slope * slope)
        self.assertAlmostEqual(row["longitudinal_mm"],
                               -0.02 / ((slope - 0.005) / math.sqrt(1 - (slope - 0.005) ** 2) - slope / n_chief),
                               places=9)
        self.assertGreater(row["longitudinal_mm"], 0.0)

    def test_a_plane_facing_against_the_light_keeps_the_sign_of_the_longitudinal_aberration(self):
        # The same bundle seen on a plane whose normal points back to the source: v and n are reversed, the pupil
        # frame is mirrored. The crossing still lies 2 mm further along the light: +2 mm, as on the forward plane.
        forward = ray_fans.path_fans(bundle(), IDENTITY)
        rays = bundle()
        for r in rays:
            r["v_mm"], r["direction_v"], r["direction_n"] = -r["v_mm"], -r["direction_v"], -r["direction_n"]
        backward = ray_fans.path_fans(rays, frame(mirror=True))
        self.assertFalse(backward["light_along_normal"])
        for name in ("tangential", "sagittal"):
            ahead, behind = rows_by_rho(forward["fans"][name]), rows_by_rho(backward["fans"][name])
            self.assertEqual(sorted(ahead), sorted(behind))
            for rho, row in behind.items():
                self.assertAlmostEqual(row["longitudinal_mm"], 2.0, places=9)
                self.assertAlmostEqual(row["transverse_um"], ahead[rho]["transverse_um"], places=9)

    def test_rays_crossing_against_the_chief_ray_are_refused(self):
        rays = bundle()
        rays[3]["direction_n"] = -rays[3]["direction_n"]
        with self.assertRaises(ray_fans.FanError) as caught:
            ray_fans.path_fans(rays, IDENTITY)
        self.assertEqual(caught.exception.code, "RAY_DIRECTION_MIXED")

    def test_a_source_frame_rolled_against_the_plane_gives_the_same_fans(self):
        reference = ray_fans.path_fans(bundle(chief_uv=(0.0, 5.0)), IDENTITY)
        for roll in (30.0, 45.0, 90.0):
            with self.subTest(roll=roll):
                rolled = ray_fans.path_fans(bundle(chief_uv=(0.0, 5.0), frame_deg=roll), frame(roll))
                for name in ("tangential", "sagittal"):
                    want, got = rows_by_rho(reference["fans"][name]), rows_by_rho(rolled["fans"][name])
                    self.assertEqual(sorted(want), sorted(got))
                    for rho in want:
                        self.assertAlmostEqual(got[rho]["transverse_um"], want[rho]["transverse_um"], places=9)
                        self.assertAlmostEqual(got[rho]["longitudinal_mm"], want[rho]["longitudinal_mm"], places=9)

    def test_axes_on_axis_and_along_u(self):
        on_axis = ray_fans.path_fans(bundle(), IDENTITY)
        self.assertEqual(on_axis["tangential_axis_uv"], [0.0, 1.0])
        self.assertEqual(on_axis["sagittal_axis_uv"], [1.0, -0.0])
        # the ray launched at +x (ring 0, azimuth 0) is the sagittal ray at rho > 0
        plus_x = next(r for r in on_axis["fans"]["sagittal"] if r["ray_index"] == 1)
        self.assertGreater(plus_x["rho"], 0.0)
        along_u = ray_fans.path_fans(bundle(chief_uv=(5.0, 0.0)), IDENTITY)
        self.assertEqual(along_u["tangential_axis_uv"], [1.0, 0.0])
        self.assertEqual(along_u["sagittal_axis_uv"], [0.0, -1.0])
        self.assertIn(1, [r["ray_index"] for r in along_u["fans"]["tangential"] if r["rho"] > 0])

    def test_tangential_axis_on_axis_and_off_axis(self):
        self.assertEqual(ray_fans.tangential_axis(0.0, 0.0), (0.0, 1.0))
        self.assertEqual(ray_fans.tangential_axis(3.0, 0.0), (1.0, 0.0))
        u, v = ray_fans.tangential_axis(-2.0, -2.0)
        self.assertAlmostEqual(u, -math.sqrt(0.5))
        self.assertAlmostEqual(v, -math.sqrt(0.5))


class RayFanPupilTests(unittest.TestCase):
    def test_rho_keeps_its_scale_when_outer_rings_are_vignetted(self):
        full = ray_fans.path_fans(bundle(), IDENTITY, arrival={"pupil_ray_count": 48, "pupil_rays_arrived": 48})
        clipped = ray_fans.path_fans(bundle(drop_rings=(3,)), IDENTITY,
                                     arrival={"pupil_ray_count": 48, "pupil_rays_arrived": 36})
        self.assertEqual(clipped["vignetted_rays"], 12)
        self.assertEqual(full["vignetted_rays"], 0)
        self.assertEqual(clipped["rings"]["present"], [0, 1, 2])
        self.assertEqual(clipped["rho_max"], 0.75)
        ring2 = {r["zone"]: r["rho"] for r in full["fans"]["tangential"] if r["rho"] > 0}[2]
        self.assertEqual({r["zone"]: r["rho"] for r in clipped["fans"]["tangential"] if r["rho"] > 0}[2], ring2)

    def test_a_partial_ring_gives_only_its_rays_on_a_fan_azimuth(self):
        rays = [r for r in bundle() if not (r.get("ring_index") == 1 and r["ring_position_index"] in (0, 1, 2))]
        fan = ray_fans.path_fans(rays, IDENTITY)
        self.assertEqual(fan["rings"]["partial"], [1])
        self.assertNotIn(1, fan["zones"]["tangential"])  # ring 1 has no ray on the fan azimuths (odd ring)
        self.assertIn(1, fan["rings"]["present"])

    def test_zones_keep_n_of_the_rings_that_carry_fan_points(self):
        for zones, expected in ((1, [3]), (2, [1, 3]), (3, [0, 2, 3]), (4, [0, 1, 2, 3]), (9, [0, 1, 2, 3])):
            with self.subTest(zones=zones):
                fan = ray_fans.path_fans(bundle(), IDENTITY, zones=zones)
                self.assertEqual(fan["zones"], {"tangential": expected, "sagittal": expected})
                self.assertEqual(fan["zones_available"], 4)
                self.assertEqual(sorted({r["zone"] for r in fan["fans"]["tangential"]}), expected)
        for bad in (0, -1):
            with self.assertRaises(ray_fans.FanError) as caught:
                ray_fans.path_fans(bundle(), IDENTITY, zones=bad)
            self.assertEqual(caught.exception.code, "BAD_ARGUMENT")

    def test_opd_comes_from_the_engine_wavefront(self):
        rays = bundle()
        wavefront = {"status": "OK", "rms_waves": 0.11, "pv_waves": 0.3,
                     "rays": [{"ray_index": r["ray_index"], "opd_waves": 0.1 * r["ring_index"]}
                              for r in rays if not r["chief"]]}
        fan = ray_fans.path_fans(rays, IDENTITY, wavefront)
        for row in fan["fans"]["tangential"]:
            self.assertAlmostEqual(row["opd_waves"], 0.1 * row["zone"], places=9)
        self.assertEqual(fan["engine"]["opd_rms_waves"], 0.11)
        self.assertIsNone(fan["engine"]["wavefront_reason"])

    def test_missing_chief_or_pupil_rays_are_refused(self):
        with self.assertRaises(ray_fans.FanError) as caught:
            ray_fans.path_fans([ray(1, 0, 1, 0, 0, position=0)], IDENTITY)
        self.assertEqual(caught.exception.code, "CHIEF_RAY_MISSING")
        with self.assertRaises(ray_fans.FanError) as caught:
            ray_fans.path_fans([ray(0, 0, 0, 0, 0, chief=True)], IDENTITY)
        self.assertEqual(caught.exception.code, "PUPIL_RAYS_MISSING")

    def test_rays_per_ring_come_from_the_engine_numbering_when_the_last_positions_are_vignetted(self):
        # positions 10 and 11 of every ring are lost: the largest position that arrived is 9, the rings have 12 rays
        rays = [r for r in bundle() if r["chief"] or r["ring_position_index"] < 10]
        fan = ray_fans.path_fans(rays, IDENTITY, arrival={"pupil_ray_count": 48, "pupil_rays_arrived": 40})
        self.assertEqual(fan["rings"]["rays_per_ring"], 12)
        self.assertEqual(fan["rings"]["rings_launched"], 4)
        self.assertIn("pupil numbering", fan["rings"]["rays_per_ring_basis"])
        self.assertEqual(fan["rings"]["complete"], [])
        self.assertEqual(fan["vignetted_rays"], 8)
        # a launched count that the numbering does not divide is not used to name a ring count
        per_ring, rings, basis = ray_fans.rays_per_ring([r for r in rays if not r["chief"]], 47)
        self.assertEqual((per_ring, rings), (None, None))
        self.assertIn("does not fit the 47 pupil rays launched", basis)

    def test_rays_per_ring_from_a_complete_first_ring_alone(self):
        first = [r for r in bundle() if not r["chief"] and r["ring_index"] == 0]
        self.assertEqual(ray_fans.rays_per_ring(first, 48)[:2], (12, 4))
        self.assertEqual(ray_fans.rays_per_ring(first[:-1], 48)[:2], (None, None))

    def test_a_path_with_no_counted_rays_is_a_structured_refusal(self):
        rays = bundle()
        for r in rays:
            r["counted"] = False
        with self.assertRaises(ray_fans.FanError) as caught:
            ray_fans.path_fans(rays, IDENTITY)
        self.assertEqual(caught.exception.code, "PATH_NOT_COUNTED")
        self.assertIn("P#0", str(caught.exception))


class EmptyFanTests(unittest.TestCase):
    """A fan without points is never returned as a result: the path is refused with the reason."""

    def test_all_rings_partial_at_a_field_azimuth_off_the_sampling_grid_is_refused(self):
        u, v = 5.0 * math.cos(math.radians(20.0)), 5.0 * math.sin(math.radians(20.0))
        rays = [r for r in bundle(chief_uv=(u, v)) if r["chief"] or r["ring_position_index"] < 10]
        with self.assertRaises(ray_fans.FanError) as caught:
            ray_fans.path_fans(rays, IDENTITY, arrival={"pupil_ray_count": 48, "pupil_rays_arrived": 40})
        error = caught.exception
        self.assertEqual(error.code, "FAN_AXIS_NOT_SAMPLED")
        self.assertIn("tangential and sagittal", str(error))
        self.assertIn("rings partial: [0, 1, 2, 3]", str(error))
        details = error.details
        self.assertEqual(details["fans_without_points"], ["tangential", "sagittal"])
        self.assertEqual(details["rings"], {"complete": [], "partial": [0, 1, 2, 3], "rays_per_ring": 12})
        self.assertEqual(details["fan_azimuths_deg"]["tangential"], [20.0, 200.0])
        self.assertEqual(details["fan_azimuths_deg"]["sagittal"], [110.0, 290.0])
        self.assertEqual(sorted(details["ray_azimuths_deg"]), ["0", "1", "2", "3"])
        self.assertEqual(len(details["ray_azimuths_deg"]["0"]), 10)
        self.assertAlmostEqual(details["nearest_ray_deg"]["tangential"], 5.0, places=6)  # rays at 15 and 30 deg

    def test_one_fan_without_points_refuses_the_path(self):
        # chief along +u: the sagittal azimuths are 90 and 270 deg; positions 3 and 9 (the even rings' rays there) are
        # lost, the odd rings are turned by 15 deg, and no ring is complete
        rays = [r for r in bundle(chief_uv=(5.0, 0.0)) if r["chief"] or r["ring_position_index"] not in (3, 9)]
        with self.assertRaises(ray_fans.FanError) as caught:
            ray_fans.path_fans(rays, IDENTITY)
        self.assertEqual(caught.exception.code, "FAN_AXIS_NOT_SAMPLED")
        self.assertEqual(caught.exception.details["fans_without_points"], ["sagittal"])
        self.assertIn("the sagittal fan has no point", str(caught.exception))

    def test_zones_keep_the_outermost_ring_of_each_fan(self):
        # ring 2 has rays on the sagittal azimuths only, ring 3 none: one zone keeps ring 1 (tangential) and ring 2
        # (sagittal), so neither fan is emptied by --zones
        rays = [r for r in bundle(drop_rings=(3,)) if r["chief"] or not (r["ring_index"] == 2
                                                                          and r["ring_position_index"] in (3, 9))]
        fan = ray_fans.path_fans(rays, IDENTITY, zones=1)
        self.assertEqual(fan["zones"], {"tangential": [1], "sagittal": [2]})
        self.assertEqual(fan["fan_rho_max"], {"tangential": 0.5, "sagittal": 0.75})

    def _collected(self, paths):
        rays, selected = {}, []
        for path_id, rows in paths.items():
            for r in rows:
                r.update(path_id=path_id, source_id=path_id[0])
            rays[path_id] = rows
            selected.append(path_id)
        return {"run": "run", "monitor": "focus", "plane": PLANE, "rays": rays, "arrivals": {}, "selected": selected,
                "explicit": False, "unreached": [], "wavefronts": {},
                "source_poses": {path_id[0]: SOURCE for path_id in paths}}

    def test_refused_paths_are_listed_with_details_and_none_left_is_no_fans(self):
        u, v = 5.0 * math.cos(math.radians(20.0)), 5.0 * math.sin(math.radians(20.0))
        partial = [r for r in bundle(chief_uv=(u, v)) if r["chief"] or r["ring_position_index"] < 10]
        collected = self._collected({"A#0": bundle(chief_uv=(u, v)), "B#0": partial})
        document = ray_fans.assemble(collected)
        self.assertEqual([p["path_id"] for p in document["paths"]], ["A#0"])
        refused = document["refused"]
        self.assertEqual([(r["path_id"], r["code"]) for r in refused], [("B#0", "FAN_AXIS_NOT_SAMPLED")])
        self.assertEqual(refused[0]["details"]["fans_without_points"], ["tangential", "sagittal"])
        with self.assertRaises(ray_fans.FanError) as caught:
            ray_fans.assemble(dict(collected, explicit=True, selected=["B#0"]))
        self.assertEqual(caught.exception.code, "FAN_AXIS_NOT_SAMPLED")
        with self.assertRaises(ray_fans.FanError) as caught:
            ray_fans.assemble(dict(collected, selected=["B#0"]))
        self.assertEqual(caught.exception.code, "NO_FANS")
        self.assertIn("B#0: FAN_AXIS_NOT_SAMPLED", str(caught.exception))

    def test_the_command_line_error_carries_the_details(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(ray_fans._error("FAN_AXIS_NOT_SAMPLED", "m", details={"fans_without_points": ["sagittal"]}),
                             4)
        self.assertEqual(json.loads(err.getvalue())["error"]["details"], {"fans_without_points": ["sagittal"]})


def _chief_row(direction=(1.0, 0.0, 0.0), dn=1.0):
    return {"path_id": "S#0", "source_id": "S", "direction_x": direction[0], "direction_y": direction[1],
            "direction_z": direction[2], "direction_n": dn}


# source along +x with local x -> +y, local y -> +z (the kit's ideal-lens cases); plane u = +y, v = +z, n = +x
SOURCE = {"quaternion_wxyz": [0.5, 0.5, 0.5, 0.5]}
PLANE = {"u_axis": [0.0, 1.0, 0.0], "v_axis": [0.0, 0.0, 1.0], "normal": [1.0, 0.0, 0.0]}


def _first_order(deg, scale=-0.01):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return {"reflection_count": 0, "matrix": [[0.0, 0.0, 100.0, 0.0], [0.0, 0.0, 0.0, 100.0],
                                              [scale * c, -scale * s, 1.0, 0.0], [scale * s, scale * c, 0.0, 1.0]]}


class PupilFrameTests(unittest.TestCase):
    def test_unrolled_source_maps_launch_x_to_u(self):
        frame_ = ray_fans.pupil_frame({"first_order": _first_order(0.0)}, PLANE, SOURCE, _chief_row())
        self.assertEqual(frame_["orientation"], "FIRST_ORDER")
        self.assertAlmostEqual(frame_["rotation_deg"], 0.0, places=9)
        self.assertFalse(frame_["mirrored"])

    def test_a_rolled_source_turns_the_frame(self):
        roll = 30.0
        half = math.radians(roll) / 2.0  # about world x (the beam), applied after the base pose
        q = [math.cos(half), math.sin(half), 0.0, 0.0]
        w1, x1, y1, z1 = q
        w2, x2, y2, z2 = SOURCE["quaternion_wxyz"]
        rolled = {"quaternion_wxyz": [w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2, w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
                                      w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2, w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2]}
        pose_only = ray_fans.pupil_frame({}, PLANE, rolled, _chief_row())
        self.assertEqual(pose_only["orientation"], "SOURCE_POSE")
        self.assertAlmostEqual(pose_only["rotation_deg"], roll, places=9)
        engine = ray_fans.pupil_frame({"first_order": _first_order(roll)}, PLANE, rolled, _chief_row())
        self.assertAlmostEqual(engine["rotation_deg"], roll, places=9)

    def test_the_first_order_sign_comes_from_the_source_pose(self):
        # a focusing map (negative C block) has the orientation of R(180 deg) on its own; the pose fixes it to 0 deg
        frame_ = ray_fans.pupil_frame({"first_order": _first_order(180.0, scale=0.01)}, PLANE, SOURCE, _chief_row())
        self.assertAlmostEqual(frame_["rotation_deg"], 0.0, places=9)

    def test_a_plane_facing_the_source_gives_a_mirrored_frame(self):
        flipped = {"u_axis": [0.0, -1.0, 0.0], "v_axis": [0.0, 0.0, 1.0], "normal": [-1.0, 0.0, 0.0]}
        frame_ = ray_fans.pupil_frame({}, flipped, SOURCE, _chief_row(dn=-1.0))
        self.assertTrue(frame_["mirrored"])

    def test_reflected_paths_and_disagreeing_maps_are_refused(self):
        for arrival in ({"first_order": {"reflection_count": 1, "matrix": None}},
                        {"lineage": ["M1/surface:reflect"]},
                        {"first_order": _first_order(90.0)}):
            with self.subTest(arrival=arrival):
                with self.assertRaises(ray_fans.FanError) as caught:
                    ray_fans.pupil_frame(arrival, PLANE, SOURCE, _chief_row())
                self.assertEqual(caught.exception.code, "PUPIL_FRAME_UNRESOLVED")
        with self.assertRaises(ray_fans.FanError) as caught:
            ray_fans.pupil_frame({}, PLANE, None, _chief_row())
        self.assertEqual(caught.exception.code, "PUPIL_FRAME_UNRESOLVED")
        with self.assertRaises(ray_fans.FanError) as caught:
            ray_fans.pupil_frame({"reference_ray_promoted_at": ["L1/S1"]}, PLANE, SOURCE, _chief_row())
        self.assertEqual(caught.exception.code, "REFERENCE_RAY_PROMOTED")


def _runner(outputs):
    """A fake engine: argv[1:3] (e.g. ('results', 'wavefront')) -> (exit code, stdout)."""
    def run(cmd, **_kwargs):
        key = next(k for k in outputs if all(part in cmd for part in k))
        code, stdout = outputs[key]
        return subprocess.CompletedProcess(cmd, code, stdout, "")
    return run


class EngineDocumentTests(unittest.TestCase):
    def test_engine_output_that_is_not_json_is_a_structured_error(self):
        runner = _runner({("rays",): (1, "Traceback (most recent call last): ...")})
        with self.assertRaises(ray_fans.FanError) as caught:
            ray_fans.load_rays(Path("run"), "image_plane", runner)
        self.assertEqual(caught.exception.code, "ENGINE_RESULTS_FAILED")

    def test_a_refused_wavefront_keeps_the_engine_reason_and_message(self):
        message = "per-ray optical path lengths are not defined after an ideal or tabulated thin lens " + "x" * 600
        document = {"status": "UNAVAILABLE", "reason": "RAY_OPL_UNDEFINED", "message": message,
                    "model": "Geometric optics " + "y" * 400}
        wavefront = ray_fans.load_wavefront(Path("run"), "focus", "S#0",
                                            _runner({("wavefront",): (2, json.dumps(document, indent=2))}))
        self.assertEqual(wavefront, {"status": "UNAVAILABLE", "reason": "RAY_OPL_UNDEFINED", "message": message,
                                     "rays": []})

    def test_an_engine_error_is_not_taken_for_a_wavefront_refusal(self):
        document = {"status": "ERROR", "error": {"code": "PATH_NOT_FOUND", "message": "no beam path 'X#0'"}}
        with self.assertRaises(ray_fans.FanError) as caught:
            ray_fans.load_wavefront(Path("run"), "focus", "X#0", _runner({("wavefront",): (64, json.dumps(document))}))
        self.assertEqual(caught.exception.code, "ENGINE_RESULTS_FAILED")
        self.assertEqual(caught.exception.engine_code, "PATH_NOT_FOUND")
        self.assertIn("no beam path 'X#0'", str(caught.exception))

    def test_engine_spot_radius_is_read_per_source_wavelength_and_plane(self):
        imaging = {"quantities": [
            {"quantity_id": "S#0.geometric_spot_rms_radius", "state": "KNOWN", "value": 5.0,
             "scope": {"source_id": "S", "observation_id": "image", "wavelength_nm": 486.1}},
            {"quantity_id": "S#1.geometric_spot_rms_radius", "state": "KNOWN", "value": 7.0,
             "scope": {"source_id": "S", "observation_id": "image", "wavelength_nm": 656.3}},
            {"quantity_id": "S#0.geometric_spot_rms_radius", "state": "KNOWN", "value": 9.0,
             "scope": {"source_id": "S", "observation_id": "other", "wavelength_nm": 486.1}},
            {"quantity_id": "T#0.geometric_spot_rms_radius", "state": "UNAVAILABLE", "value": None,
             "scope": {"source_id": "T"}}]}
        self.assertEqual(ray_fans.engine_spot_rms(imaging, "image"), {("S", 486.1): 5.0, ("S", 656.3): 7.0})

    def test_unreached_paths_are_listed_with_the_engine_termination(self):
        rows = bundle()
        for r in rows:
            r.update(path_id="A#0", source_id="A")
        outside = [dict(r, path_id="B#0", source_id="B", counted=False) for r in bundle()]
        plane = {"u_axis": [0.0, 1.0, 0.0], "v_axis": [0.0, 0.0, 1.0], "normal": [1.0, 0.0, 0.0]}
        tree = [{"path_id": p, "source_id": p[0], "wavelength_nm": 550.0, "children": [], "termination": t,
                 "termination_surface_id": s} for p, t, s in
                (("A#0", "ESCAPED", None), ("B#0", "ESCAPED", None), ("C#0", "CLIPPED", "L1/S1"))]
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "project.json").write_text(json.dumps({"components": [
                {"instance_id": "A", "optical_role": "source", "nominal_pose": SOURCE}]}), encoding="utf-8")
            runner = _runner({("rays",): (0, json.dumps(rows + outside)),
                              ("monitor",): (0, json.dumps({"plane": plane, "arrivals": []})),
                              ("paths",): (0, json.dumps(tree)),
                              ("wavefront",): (2, json.dumps({"status": "UNAVAILABLE", "reason": "R", "message": "m"}))})
            document = ray_fans.build(run, "focus", runner=runner)
            self.assertEqual([p["path_id"] for p in document["paths"]], ["A#0"])
            self.assertEqual([(u["path_id"], u["reason"], u["termination"], u["termination_surface_id"])
                              for u in document["unreached"]],
                             [("B#0", "NO_COUNTED_RAYS_AT_MONITOR", "ESCAPED", None),
                              ("C#0", "NO_RAYS_AT_MONITOR", "CLIPPED", "L1/S1")])
            self.assertEqual(document["paths"][0]["engine"]["wavefront_reason"], "R")
            with self.assertRaises(ray_fans.FanError) as caught:
                ray_fans.build(run, "focus", paths=["C#0"], runner=runner)
            self.assertEqual(caught.exception.code, "PATH_NOT_FOUND")
            self.assertIn("CLIPPED at L1/S1", str(caught.exception))
            # a path without a source pose is refused, not given fans in an unknown frame
            (run / "project.json").write_text(json.dumps({"components": []}), encoding="utf-8")
            with self.assertRaises(ray_fans.FanError) as caught:
                ray_fans.build(run, "focus", runner=runner)
            self.assertEqual(caught.exception.code, "NO_FANS")
            self.assertIn("PUPIL_FRAME_UNRESOLVED", str(caught.exception))


class CommandLineTests(unittest.TestCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ray_fans.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def assert_error(self, argv, code):
        status, _out, err = self.run_main(*argv)
        self.assertEqual(status, 4, err)
        self.assertEqual(json.loads(err)["error"]["code"], code)

    def test_zones_below_one_are_usage_errors(self):
        for zones in ("0", "-1"):
            with self.subTest(zones=zones):
                self.assert_error(["run", "--monitor", "focus", "--zones", zones], "BAD_ARGUMENT")

    def test_bad_imaging_files_and_output_directories_are_json_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.assert_error(["run", "--monitor", "focus", "--imaging", str(base / "missing.json")],
                              "IMAGING_NOT_READABLE")
            for name, text in (("notjson", "notjson"), ("list", "[1, 2]"), ("numbers", '{"quantities": 5}'),
                               ("items", '{"quantities": [1]}'),
                               ("scope", '{"quantities": [{"quantity_id": "S#0.geometric_spot_rms_radius", '
                                         '"state": "KNOWN", "value": 1.0}]}')):
                with self.subTest(imaging=name):
                    (base / name).write_text(text, encoding="utf-8")
                    self.assert_error(["run", "--monitor", "focus", "--imaging", str(base / name)], "IMAGING_INVALID")
            self.assert_error(["run", "--monitor", "focus", "--out", str(base / "no" / "fans.json")],
                              "OUTPUT_NOT_WRITABLE")

    def test_help_returns_zero(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ray_fans.main(["--help"]), 0)


if __name__ == "__main__":
    unittest.main()
