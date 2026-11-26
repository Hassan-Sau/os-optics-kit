"""Field curvature and distortion without the engine: geometry helpers, the EFL reference, the on-axis and stop checks,
argument validation and the structured refusals."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import math
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from os_optics_kit import field_aberrations

WAVELENGTH = 550.0


def _axis(angle_deg: float) -> tuple[float, float, float]:
    return (math.cos(math.radians(angle_deg)), 0.0, -math.sin(math.radians(angle_deg)))


def _path(source: str, height_mm: float, focus_t=0.0, focus_s=0.0, u_mm=0.0, wavelength=WAVELENGTH, tangential=None,
          sagittal=None) -> dict:
    return {"path_id": f"{source}#0", "source_id": source, "wavelength_nm": wavelength,
            "chief_uv_mm": [u_mm, height_mm],
            "fans": {"tangential": tangential if tangential is not None else
                     [{"rho": 0.1, "longitudinal_mm": focus_t}, {"rho": -0.1, "longitudinal_mm": focus_t},
                      {"rho": 0.5, "longitudinal_mm": focus_t + 1.0}],
                     "sagittal": sagittal if sagittal is not None else [{"rho": 0.1, "longitudinal_mm": focus_s}]}}


def _mapping(efl_mm: float, distortion: dict[float, float]) -> tuple[list[dict], dict]:
    """Paths of a lens with h = EFL tan(theta) (1 + D(theta)), one source per field angle."""
    paths, axes = [], {}
    for i, (angle, d) in enumerate(sorted(distortion.items())):
        source = f"F{i}"
        axes[source] = _axis(angle)
        paths.append(_path(source, efl_mm * math.tan(math.radians(angle)) * (1.0 + d)))
    return paths, axes


class Geometry(unittest.TestCase):
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
            {"optical_role": "lens", "instance_id": "L", "nominal_pose": {"quaternion_wxyz": [1, 0, 0, 0]}}]}),
            {"A": (1.0, 0.0, 0.0)})
        self.assertAlmostEqual(field_aberrations._angle_deg((1, 0, 0), (0, 1, 0)), 90.0)


class DistortionReference(unittest.TestCase):
    """The reference height is EFL tan(theta) with a stated or engine-paraxial EFL, never a field of the run itself."""

    def test_non_monotone_distortion_keeps_its_sign_at_every_field(self):
        # D(7 deg) = +0.0477 %, D(14 deg) = +0.0350 %: pincushion at both fields, smaller at the larger one
        paths, axes = _mapping(84.9999, {0.0: 0.0, 7.0: 4.77e-4, 14.0: 3.50e-4})
        document = field_aberrations.analyze(paths, axes,
                                             efl_for=lambda source, w: {"efl_mm": 84.9999, "basis": "paraxial (test)"})
        rows = {round(r["field_angle_deg"], 6): r for r in document["fields"]}
        self.assertAlmostEqual(rows[7.0]["distortion_pct"], 0.0477, delta=1e-9)
        self.assertAlmostEqual(rows[14.0]["distortion_pct"], 0.0350, delta=1e-9)
        self.assertGreater(rows[14.0]["distortion_pct"], 0.0)
        self.assertEqual(rows[0.0]["distortion_pct"], 0.0)
        self.assertEqual(document["references"][str(WAVELENGTH)], {
            "efl_mm": 84.9999, "basis": "paraxial (test)",
            "distortion_plane": "the plane itself, taken as the focal plane of the stated EFL",
            "rear_focal_point_mm": None, "paraxial_focus_from_plane_mm": None})

    def test_the_efl_comes_per_wavelength_from_the_axis_field(self):
        paths, axes = _mapping(100.0, {0.0: 0.0, 5.0: 0.0})
        paths += [dict(p, path_id=p["path_id"] + "b", wavelength_nm=650.0) for p in paths]
        asked = []

        def efl_for(source, wavelength):
            asked.append((source, wavelength))
            return {"efl_mm": 100.0, "basis": f"paraxial at {wavelength}"}

        document = field_aberrations.analyze(paths, axes, efl_for=efl_for)
        self.assertEqual(sorted(asked), [("F0", WAVELENGTH), ("F0", 650.0)])
        self.assertEqual(set(document["references"]), {"550.0", "650.0"})

    def test_a_stated_efl_wins_and_is_named(self):
        paths, axes = _mapping(100.0, {0.0: 0.0, 5.0: 0.01})
        document = field_aberrations.analyze(paths, axes, efl_mm=100.0, efl_for=lambda s, w: self.fail("asked"))
        row = [r for r in document["fields"] if r["field_angle_deg"] > 1][0]
        self.assertAlmostEqual(row["distortion_pct"], 1.0, places=9)
        self.assertEqual(document["references"]["550.0"]["basis"], "stated EFL (--efl)")

    def test_an_unusable_reference_is_refused(self):
        paths, axes = _mapping(100.0, {0.0: 0.0, 5.0: 0.0})
        for reply in ((100.0, "a tuple"), {"efl_mm": math.nan, "basis": "b"}, {"efl_mm": -1.0, "basis": "b"},
                      {"efl_mm": 100.0, "basis": "b", "rear_focal_point_mm": [100.0, 0.0, 0.0]},
                      {"efl_mm": 100.0, "basis": "b", "rear_focal_point_mm": [100.0, 0.0],
                       "axis_direction": [1.0, 0.0, 0.0]},
                      {"efl_mm": 100.0, "basis": "b", "rear_focal_point_mm": [100.0, 0.0, 0.0],
                       "axis_direction": [0.0, 0.0, 0.0]}):
            with self.subTest(reply=reply):
                with self.assertRaises(field_aberrations.FieldError) as caught:
                    field_aberrations.analyze(paths, axes, efl_for=lambda s, w, reply=reply: reply)
                self.assertEqual(caught.exception.code, "EFL_NOT_STATED")
                self.assertIn("--efl", str(caught.exception))

    def test_no_efl_is_refused(self):
        paths, axes = _mapping(100.0, {0.0: 0.0, 5.0: 0.0})
        with self.assertRaises(field_aberrations.FieldError) as caught:
            field_aberrations.analyze(paths, axes)
        self.assertEqual(caught.exception.code, "EFL_NOT_STATED")


class FocalPlaneDistortion(unittest.TestCase):
    """Without --efl, h is taken where each chief ray meets the paraxial rear focal plane, not on the plane it is read
    at. A lens of f = 100 mm along +x, focus at x = 100, plane at x = 100 + DELTA; each chief ray leaves with its own
    slope and meets the focal plane at h = f tan(theta) (1 + D)."""

    F, DELTA = 100.0, 2.5
    FOCUS = {"efl_mm": 100.0, "basis": "paraxial (test)", "rear_focal_point_mm": [100.0, 0.0, 0.0],
             "axis_direction": [1.0, 0.0, 0.0]}

    def setup(self, distortion: dict[float, float], slope_factor: float = 0.93):
        paths, axes, chiefs = [], {}, {}
        for i, (angle, d) in enumerate(sorted(distortion.items())):
            source = f"F{i}"
            axes[source] = _axis(angle)
            h_focus = self.F * math.tan(math.radians(angle)) * (1.0 + d)
            slope = slope_factor * math.tan(math.radians(angle))
            h_plane = h_focus + self.DELTA * slope
            paths.append(_path(source, h_plane))
            norm = math.hypot(1.0, slope)
            chiefs[f"{source}#0"] = {"point_mm": [self.F + self.DELTA, 0.0, h_plane],
                                     "direction": [1.0 / norm, 0.0, slope / norm]}
        return paths, axes, chiefs

    def test_distortion_is_taken_on_the_focal_plane(self):
        distortion = {0.0: 0.0, 5.0: -0.002, 10.0: 0.004}
        paths, axes, chiefs = self.setup(distortion)
        document = field_aberrations.analyze(paths, axes, efl_for=lambda s, w: dict(self.FOCUS), chiefs=chiefs,
                                             plane={"origin_mm": [self.F + self.DELTA, 0.0, 0.0]})
        reference = document["references"][str(WAVELENGTH)]
        self.assertEqual(reference["rear_focal_point_mm"], [100.0, 0.0, 0.0])
        self.assertAlmostEqual(reference["paraxial_focus_from_plane_mm"], -self.DELTA)
        self.assertIn("paraxial rear focal plane", reference["distortion_plane"])
        for row in document["fields"]:
            angle = row["field_angle_deg"]
            want = distortion[round(angle, 6)]
            with self.subTest(field=angle):
                self.assertAlmostEqual(row["distortion_pct"], 100.0 * want, delta=1e-9)
                self.assertAlmostEqual(row["distortion_height_mm"],
                                       self.F * math.tan(math.radians(angle)) * (1.0 + want), delta=1e-9)
        # on the plane itself the same rays read a defocus term of the opposite sign at 5 deg
        on_plane = {round(r["field_angle_deg"], 6): r for r in field_aberrations.analyze(paths, axes, efl_mm=self.F)["fields"]}
        self.assertGreater(on_plane[5.0]["distortion_pct"], 1.0)
        self.assertEqual(on_plane[5.0]["distortion_height_mm"], on_plane[5.0]["image_height_mm"])

    def test_height_on_a_focal_plane(self):
        height, reason = field_aberrations.height_on_focal_plane(
            {"point_mm": [3.0, 4.0, 0.0], "direction": [math.sqrt(0.5), math.sqrt(0.5), 0.0]}, [1.0, 1.0, 0.0],
            [1.0, 0.0, 0.0])
        self.assertIsNone(reason)
        self.assertAlmostEqual(height, 1.0)
        height, reason = field_aberrations.height_on_focal_plane(
            {"point_mm": [3.0, 4.0, 0.0], "direction": [0.0, 1.0, 0.0]}, [1.0, 1.0, 0.0], [1.0, 0.0, 0.0])
        self.assertIsNone(height)
        self.assertTrue(reason.startswith("CHIEF_RAY_PARALLEL_TO_FOCAL_PLANE"))

    def test_a_missing_chief_ray_has_no_distortion_value(self):
        paths, axes, chiefs = self.setup({0.0: 0.0, 5.0: 0.0})
        del chiefs["F1#0"]
        rows = {r["source_id"]: r for r in field_aberrations.analyze(
            paths, axes, efl_for=lambda s, w: dict(self.FOCUS), chiefs=chiefs)["fields"]}
        self.assertIsNone(rows["F1"]["distortion_pct"])
        self.assertIsNone(rows["F1"]["distortion_height_mm"])
        self.assertTrue(rows["F1"]["distortion_reason"].startswith("CHIEF_RAY_MISSING"))
        self.assertEqual(rows["F0"]["distortion_pct"], 0.0)

    def test_chief_rays_from_the_fan_rows(self):
        chief = {"chief": True, "counted": True, "position_x_mm": 1.0, "position_y_mm": 2.0, "position_z_mm": 3.0,
                 "direction_x": 0.0, "direction_y": 0.0, "direction_z": 2.0}
        collected = {"rays": {"A#0": [dict(chief, chief=False), chief], "B#0": [dict(chief, counted=False)],
                              "C#0": [dict(chief, direction_z=math.nan)]}}
        self.assertEqual(field_aberrations.chief_rays(collected),
                         {"A#0": {"point_mm": [1.0, 2.0, 3.0], "direction": [0.0, 0.0, 1.0]}})


class OnAxisField(unittest.TestCase):
    def test_a_shifted_plane_is_refused_and_cannot_be_loosened(self):
        # an ideal f = 100 lens imaged on a plane shifted 0.5 mm sideways: no field lands on the origin
        paths, axes = _mapping(100.0, {0.0: 0.0, 4.0: 0.0, 8.0: 0.0})
        for path in paths:
            path["chief_uv_mm"][0] += 0.5
        with self.assertRaises(field_aberrations.FieldError) as caught:
            field_aberrations.analyze(paths, axes, efl_mm=100.0)
        self.assertEqual(caught.exception.code, "AXIS_FIELD_MISSING")
        self.assertNotIn("tolerance-mm", str(caught.exception))
        for tolerance in (1.0, 20.0):
            with self.assertRaises(field_aberrations.FieldError) as caught:
                field_aberrations.analyze(paths, axes, efl_mm=100.0, axis_tolerance_mm=tolerance)
            self.assertEqual(caught.exception.code, "BAD_ARGUMENT")

    def test_a_zero_angle_field_off_the_origin_has_no_distortion_value(self):
        paths, axes = _mapping(100.0, {0.0: 0.0, 4.0: 0.0})
        axes["P"] = _axis(0.0)
        paths.append(_path("P", 0.5))
        rows = {r["source_id"]: r for r in field_aberrations.analyze(paths, axes, efl_mm=100.0)["fields"]}
        self.assertEqual(rows["F0"]["distortion_pct"], 0.0)
        self.assertIsNone(rows["F0"]["distortion_reason"])
        self.assertIsNone(rows["P"]["distortion_pct"])
        self.assertTrue(rows["P"]["distortion_reason"].startswith("ZERO_FIELD_NONZERO_HEIGHT"))
        self.assertAlmostEqual(rows["F1"]["distortion_pct"], 0.0, places=9)


class Focus(unittest.TestCase):
    def test_focus_zone_and_astigmatism(self):
        paths, axes = _mapping(100.0, {0.0: 0.0, 5.0: 0.0})
        paths[1]["fans"] = _path("F1", 0.0, focus_t=-0.5, focus_s=-0.2)["fans"]
        row = {r["source_id"]: r for r in field_aberrations.analyze(paths, axes, efl_mm=100.0)["fields"]}["F1"]
        self.assertEqual(row["focus_zone_rho"], 0.1)
        self.assertAlmostEqual(row["tangential_focus_mm"], -0.5)
        self.assertAlmostEqual(row["sagittal_focus_mm"], -0.2)
        self.assertAlmostEqual(row["astigmatism_mm"], -0.3)
        self.assertIsNone(row["tangential_focus_reason"])
        self.assertIsNone(row["sagittal_focus_reason"])

    def test_a_null_focus_carries_its_reason(self):
        paths, axes = _mapping(100.0, {0.0: 0.0, 5.0: 0.0})
        paths[1]["fans"] = {"tangential": [], "sagittal": [{"rho": 0.1, "longitudinal_mm": None}]}
        row = {r["source_id"]: r for r in field_aberrations.analyze(paths, axes, efl_mm=100.0)["fields"]}["F1"]
        self.assertIsNone(row["tangential_focus_mm"])
        self.assertTrue(row["tangential_focus_reason"].startswith("FAN_EMPTY"))
        self.assertIsNone(row["sagittal_focus_mm"])
        self.assertTrue(row["sagittal_focus_reason"].startswith("NO_CROSSING"))
        self.assertIsNone(row["astigmatism_mm"])


class ApertureStop(unittest.TestCase):
    SCENE = {"elements": [
        {"element_id": "AS", "pose": {"position_mm": [10.0, 0.0, 0.0], "quaternion_wxyz": [0.5, 0.5, 0.5, 0.5]},
         "geometry": {"kind": "OPTICAL_UNIT", "members": [
             {"member_id": "STOP", "pose": {"position_mm": [0.0, 0.0, 2.0], "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0]},
              "body": {"surfaces": [{"role": "APERTURE_STOP", "surface_id": "stop", "vertex_z_mm": 0.5,
                                     "clear_radius_mm": 3.0}]}},
             {"member_id": "L1", "pose": {"position_mm": [0.0, 0.0, 5.0], "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0]},
              "body": {"surfaces": [{"role": "REFRACT", "surface_id": "S1", "vertex_z_mm": 0.0}]}}]}},
        {"element_id": "SRC", "pose": {"position_mm": [0.0, 0.0, 0.0], "quaternion_wxyz": [0.5, 0.5, 0.5, 0.5]},
         "geometry": {"kind": "SOURCE_EMITTER"}}]}

    @staticmethod
    def result(points: dict[str, list[list[float]]]) -> dict:
        return {"paths": [{"path_id": path_id, "source_id": path_id.split("#")[0], "parent_path_id": None,
                           "segments": [{"end_event": "aperture_stop", "end_mm": p} for p in crossings]}
                          for path_id, crossings in points.items()]}

    def test_stop_centre_of_a_unit_member(self):
        (stop,) = field_aberrations.scene_stops(self.SCENE)
        self.assertEqual(stop["stop_id"], "AS/STOP-stop")
        for got, want in zip(stop["centre_mm"], (12.5, 0.0, 0.0)):
            self.assertAlmostEqual(got, want)
        for got, want in zip(stop["normal"], (1.0, 0.0, 0.0)):
            self.assertAlmostEqual(got, want)

    def test_stop_offsets(self):
        states = field_aberrations.chief_ray_stops(
            self.result({"A#0": [[12.5, 0.0, 0.0]], "B#0": [[12.5, 0.3, -0.4]], "C#0": [], "D#0": [[20.0, 0.0, 0.0]]}),
            self.SCENE)
        # these segments carry no surface id: the stop is the one whose plane holds the crossing point
        self.assertEqual(states["A#0"], {"status": "STOP_CROSSED", "stop_id": "AS/STOP-stop",
                                         "stop_offset_mm": 0.0, "identified_by": "STOP_PLANE", "detail": None})
        self.assertAlmostEqual(states["B#0"]["stop_offset_mm"], 0.5)
        self.assertEqual(states["C#0"]["status"], "CHIEF_RAY_STOP_NOT_CROSSED")
        self.assertEqual(states["D#0"]["status"], "CHIEF_RAY_STOP_UNRESOLVED")
        no_stop = field_aberrations.chief_ray_stops(self.result({"A#0": []}), {"elements": []})
        self.assertEqual(no_stop["A#0"]["status"], "NO_APERTURE_STOP")

    def test_a_chief_ray_off_the_stop_centre_has_no_distortion_or_focus(self):
        paths, axes = _mapping(100.0, {0.0: 0.0, 5.0: 0.01})
        axes["OFF"] = axes["F1"]
        paths.append(dict(_path("OFF", paths[1]["chief_uv_mm"][1] + 0.2)))
        stops = {"F0#0": {"status": "STOP_CROSSED", "stop_id": "S", "stop_offset_mm": 0.0},
                 "F1#0": {"status": "STOP_CROSSED", "stop_id": "S", "stop_offset_mm": 0.004},
                 "OFF#0": {"status": "STOP_CROSSED", "stop_id": "S", "stop_offset_mm": 1.1}}
        rows = {r["source_id"]: r for r in
                field_aberrations.analyze(paths, axes, efl_mm=100.0, stops=stops)["fields"]}
        # the status names what was checked: within the stated tolerance, which the row repeats
        self.assertEqual(rows["F1"]["chief_ray"], {"status": "WITHIN_STOP_TOLERANCE", "stop_id": "S",
                                                   "stop_offset_mm": 0.004, "stop_tolerance_mm": 0.01})
        self.assertAlmostEqual(rows["F1"]["distortion_pct"], 1.0, places=9)
        off = rows["OFF"]
        self.assertEqual(off["chief_ray"], {"status": "CHIEF_RAY_OFF_STOP", "stop_id": "S", "stop_offset_mm": 1.1,
                                            "stop_tolerance_mm": 0.01})
        self.assertAlmostEqual(off["field_angle_deg"], rows["F1"]["field_angle_deg"])
        for key in ("distortion_pct", "reference_height_mm", "tangential_focus_mm", "sagittal_focus_mm",
                    "astigmatism_mm"):
            self.assertIsNone(off[key], key)
        for key in ("distortion_reason", "tangential_focus_reason", "sagittal_focus_reason"):
            self.assertTrue(off[key].startswith("CHIEF_RAY_OFF_STOP"), key)
        # a tighter tolerance flags the 4 um offset too; without a stop the beam-centre values are kept and labelled
        tight = {r["source_id"]: r for r in field_aberrations.analyze(
            paths, axes, efl_mm=100.0, stops=stops, stop_tolerance_mm=0.001)["fields"]}
        self.assertEqual(tight["F1"]["chief_ray"]["status"], "CHIEF_RAY_OFF_STOP")
        self.assertEqual(tight["F1"]["chief_ray"]["stop_tolerance_mm"], 0.001)
        loose = {r["source_id"]: r for r in field_aberrations.analyze(
            paths, axes, efl_mm=100.0, stops=stops, stop_tolerance_mm=2.0)["fields"]}
        self.assertEqual(loose["OFF"]["chief_ray"], {"status": "WITHIN_STOP_TOLERANCE", "stop_id": "S",
                                                     "stop_offset_mm": 1.1, "stop_tolerance_mm": 2.0})
        none = {path_id: {"status": "NO_APERTURE_STOP", "stop_id": None, "stop_offset_mm": None} for path_id in stops}
        free = {r["source_id"]: r for r in
                field_aberrations.analyze(paths, axes, efl_mm=100.0, stops=none)["fields"]}
        self.assertEqual(free["OFF"]["chief_ray"]["status"], "NO_APERTURE_STOP")
        self.assertIsNone(free["OFF"]["chief_ray"]["stop_tolerance_mm"])
        self.assertIsNotNone(free["OFF"]["distortion_pct"])


class DecentredStop(unittest.TestCase):
    """The chief ray is measured from the centre of the stop's opening (aperture_center_mm in the stop plane), and the
    crossed stop is the one whose Engine surface id ends the segment.

    Frames: the pose quaternion [0.5, 0.5, 0.5, 0.5] turns local x, y, z into design y, z, x; a member turned 90 deg
    about its z axis ([cos 45, 0, 0, sin 45]) turns its x into the unit's y."""

    Q = [0.5, 0.5, 0.5, 0.5]
    TURN_Z = [math.cos(math.pi / 4), 0.0, 0.0, math.sin(math.pi / 4)]

    @classmethod
    def scene(cls, centre=(1.5, 0.0)) -> dict:
        return {"elements": [
            {"element_id": "STOP", "pose": {"position_mm": [5.0, 0.0, 20.0], "quaternion_wxyz": cls.Q},
             "geometry": {"kind": "OPTIC_BODY", "surfaces": [
                 {"role": "APERTURE_STOP", "surface_id": "stop", "vertex_z_mm": 0.0, "clear_radius_mm": 3.0,
                  "aperture_center_mm": list(centre)}]}},
            {"element_id": "AS", "pose": {"position_mm": [10.0, 0.0, 0.0], "quaternion_wxyz": cls.Q},
             "geometry": {"kind": "OPTICAL_UNIT", "members": [
                 {"member_id": "IRIS", "pose": {"position_mm": [0.0, 0.0, 2.0], "quaternion_wxyz": cls.TURN_Z},
                  "body": {"surfaces": [{"role": "APERTURE_STOP", "surface_id": "stop", "vertex_z_mm": 0.5,
                                         "clear_radius_mm": 3.0, "aperture_center_mm": [1.0, 0.0]}]}}]}},
            {"element_id": "LENS", "pose": {"position_mm": [10.0, 0.0, 20.0], "quaternion_wxyz": cls.Q},
             "geometry": {"kind": "OPTIC_BODY", "surfaces": [{"role": "THIN_LENS", "surface_id": "lens",
                                                             "vertex_z_mm": 0.0}]}}]}

    @staticmethod
    def result(segments: dict[str, list[dict]]) -> dict:
        return {"paths": [{"path_id": path_id, "source_id": path_id.split("#")[0], "segments": items}
                          for path_id, items in segments.items()]}

    @staticmethod
    def segment(point, surface_id="STOP/stop", event="aperture_stop") -> dict:
        return {"end_event": event, "end_surface_id": surface_id, "end_mm": list(point)}

    def test_the_opening_centre_is_carried_to_the_design_frame(self):
        stops = {stop["stop_id"]: stop for stop in field_aberrations.scene_stops(self.scene())}
        self.assertEqual(sorted(stops), ["AS/IRIS-stop", "STOP/stop"])
        top = stops["STOP/stop"]
        self.assertEqual(top["opening_center_mm"], [1.5, 0.0])
        self.assertIsNone(top["geometry_issue"])
        for got, want in zip(top["centre_mm"], (5.0, 1.5, 20.0)):  # local x -> design y
            self.assertAlmostEqual(got, want)
        # member origin (12, 0, 0); its local (1, 0, 0.5) -> unit (0, 1, 0.5) -> design (0.5, 0, 1)
        for got, want in zip(stops["AS/IRIS-stop"]["centre_mm"], (12.5, 0.0, 1.0)):
            self.assertAlmostEqual(got, want)
        for got, want in zip(stops["AS/IRIS-stop"]["normal"], (1.0, 0.0, 0.0)):
            self.assertAlmostEqual(got, want)

    def test_a_decentred_stop_reports_the_offset_from_its_opening(self):
        states = field_aberrations.chief_ray_stops(self.result({
            "AXIS#0": [self.segment([5.0, 0.0, 20.0]), {"end_event": "thin_lens", "end_surface_id": "LENS/lens",
                                                         "end_mm": [10.0, 0.0, 20.0]}],
            "AIMED#0": [self.segment([5.0, 1.5, 20.0])],
            "UNIT#0": [self.segment([12.5, 0.0, 0.0], surface_id="AS/IRIS-stop")]}), self.scene())
        self.assertEqual(states["AXIS#0"]["status"], "STOP_CROSSED")
        self.assertEqual(states["AXIS#0"]["stop_id"], "STOP/stop")
        self.assertEqual(states["AXIS#0"]["identified_by"], "ENGINE_SURFACE_ID")
        self.assertAlmostEqual(states["AXIS#0"]["stop_offset_mm"], 1.5)
        self.assertAlmostEqual(states["AIMED#0"]["stop_offset_mm"], 0.0)
        self.assertAlmostEqual(states["UNIT#0"]["stop_offset_mm"], 1.0)
        # the 0.01 mm tolerance fails the on-axis beam-centre ray of the decentred stop
        paths, axes = _mapping(100.0, {0.0: 0.0})
        row = field_aberrations.analyze(paths, axes, efl_mm=100.0,
                                        stops={"F0#0": states["AXIS#0"]})["fields"][0]
        self.assertEqual(row["chief_ray"]["status"], "CHIEF_RAY_OFF_STOP")
        self.assertIsNone(row["distortion_pct"])
        self.assertTrue(row["distortion_reason"].startswith("CHIEF_RAY_OFF_STOP: the beam-centre ray crosses the "
                                                            "aperture stop 1.5 mm from the centre of its opening"))
        # the centred control
        centred = field_aberrations.chief_ray_stops(self.result({"AXIS#0": [self.segment([5.0, 0.0, 20.0])]}),
                                                    self.scene(centre=(0.0, 0.0)))
        self.assertEqual(centred["AXIS#0"]["stop_offset_mm"], 0.0)

    def test_a_sampled_stop_crossing_is_found_by_its_surface_id(self):
        states = field_aberrations.chief_ray_stops(self.result({
            "S#0": [self.segment([5.0, 0.0, 20.0], event="complex_transmission")],
            "L#0": [{"end_event": "complex_transmission", "end_surface_id": "LENS/lens", "end_mm": [10.0, 0.0, 20.0]}]}),
            self.scene())
        self.assertEqual(states["S#0"]["status"], "STOP_CROSSED")
        self.assertEqual(states["S#0"]["identified_by"], "ENGINE_SURFACE_ID")
        self.assertAlmostEqual(states["S#0"]["stop_offset_mm"], 1.5)
        self.assertEqual(states["L#0"]["status"], "CHIEF_RAY_STOP_NOT_CROSSED")

    def test_an_unresolved_crossing_or_stop_geometry_says_why(self):
        bad_centre = self.scene(centre=("1.5", None))
        twin = self.scene()
        twin["elements"].append(copy.deepcopy(twin["elements"][0]))
        twin["elements"][-1]["element_id"] = "STOP2"
        cases = {
            "unknown stop id": (self.scene(), [self.segment([5.0, 0.0, 20.0], surface_id="GONE/stop")],
                                "GONE/stop", None),
            "off the stop plane": (self.scene(), [self.segment([6.0, 0.0, 20.0])], "off the stop plane",
                                   "STOP/stop"),
            "opening centre": (bad_centre, [self.segment([5.0, 0.0, 20.0])], "is not two finite lengths",
                               "STOP/stop"),
            "two stop planes": (twin, [{"end_event": "aperture_stop", "end_mm": [5.0, 0.0, 20.0]}],
                                "plane of 2 aperture stops", None)}
        paths, axes = _mapping(100.0, {0.0: 0.0})
        for name, (scene, segments, words, stop_id) in cases.items():
            with self.subTest(name):
                state = field_aberrations.chief_ray_stops(self.result({"F0#0": segments}), scene)["F0#0"]
                self.assertEqual(state["status"], "CHIEF_RAY_STOP_UNRESOLVED")
                self.assertEqual(state["stop_id"], stop_id)
                self.assertIsNone(state["stop_offset_mm"])
                self.assertIn(words, state["detail"])
                row = field_aberrations.analyze(paths, axes, efl_mm=100.0, stops={"F0#0": state})["fields"][0]
                self.assertEqual(row["chief_ray"]["status"], "CHIEF_RAY_STOP_UNRESOLVED")
                for key in ("distortion_pct", "tangential_focus_mm", "sagittal_focus_mm"):
                    self.assertIsNone(row[key], key)
                for key in ("distortion_reason", "tangential_focus_reason", "sagittal_focus_reason"):
                    self.assertTrue(row[key].startswith("CHIEF_RAY_STOP_UNRESOLVED"), key)
                    self.assertIn(words, row[key])
        (issue,) = [s for s in field_aberrations.scene_stops(bad_centre) if s["stop_id"] == "STOP/stop"]
        self.assertIsNone(issue["centre_mm"])
        self.assertIn("is not two finite lengths", issue["geometry_issue"])


class ReflectedPaths(unittest.TestCase):
    """A plane that only reflected paths reach is refused with REFLECTED_PATH_UNSUPPORTED."""

    @staticmethod
    def collected(reflections: dict[str, int]) -> dict:
        return {"monitor": "near", "selected": list(reflections), "unreached": [], "plane": {},
                "arrivals": {path_id: {"first_order": {"reflection_count": count}}
                             for path_id, count in reflections.items()}}

    def test_only_reflected_paths_are_refused(self):
        refusal = field_aberrations.reflected_refusal(self.collected({"SRC#0": 1, "SRC#1": 2}))
        self.assertIsInstance(refusal, field_aberrations.FieldError)
        assert refusal is not None
        self.assertEqual(refusal.code, "REFLECTED_PATH_UNSUPPORTED")
        self.assertEqual(refusal.details, {"monitor": "near", "reflected_paths": [
            {"path_id": "SRC#0", "reflections": 1}, {"path_id": "SRC#1", "reflections": 2}]})
        self.assertIn("is not resolved for reflected paths", str(refusal))
        self.assertIsNone(field_aberrations.reflected_refusal(self.collected({"SRC#0": 1, "OTHER#0": 0})))
        self.assertIsNone(field_aberrations.reflected_refusal(self.collected({})))

    def test_the_command_refuses_a_reflected_plane_with_a_json_error(self):
        collected = self.collected({"SRC#0": 1})

        def no_fans(_collected, *args, **kwargs):
            raise field_aberrations.ray_fans.FanError("NO_FANS", "no path at near gives fans "
                                                                 "(SRC#0: PUPIL_FRAME_UNRESOLVED)")

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            run.mkdir()
            for name in ("project.json", "simulation_result.json", "optical_scene.json"):
                (run / name).write_text("{}", encoding="utf-8")
            stderr = io.StringIO()
            with mock.patch.object(field_aberrations.ray_fans, "collect", return_value=collected), \
                    mock.patch.object(field_aberrations.ray_fans, "assemble", side_effect=no_fans), \
                    contextlib.redirect_stderr(stderr):
                code = field_aberrations.main([str(run), "--monitor", "near", "--efl", "100"])
        self.assertEqual(code, 4)
        error = json.loads(stderr.getvalue())["error"]
        self.assertEqual(error["code"], "REFLECTED_PATH_UNSUPPORTED")
        self.assertEqual(error["details"]["reflected_paths"], [{"path_id": "SRC#0", "reflections": 1}])
        # another fans refusal keeps its own code
        collected["arrivals"]["SRC#0"]["first_order"]["reflection_count"] = 0
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            run.mkdir()
            for name in ("project.json", "simulation_result.json", "optical_scene.json"):
                (run / name).write_text("{}", encoding="utf-8")
            stderr = io.StringIO()
            with mock.patch.object(field_aberrations.ray_fans, "collect", return_value=collected), \
                    mock.patch.object(field_aberrations.ray_fans, "assemble", side_effect=no_fans), \
                    contextlib.redirect_stderr(stderr):
                code = field_aberrations.main([str(run), "--monitor", "near", "--efl", "100"])
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(stderr.getvalue())["error"]["code"], "NO_FANS")


class UnreachedFields(unittest.TestCase):
    """Every declared source is in fields or in unreached_fields, with the reason it has no row."""

    RESULT = {"paths": [
        {"path_id": "A#0", "source_id": "A", "wavelength_nm": 550.0, "parent_path_id": None,
         "termination": "DETECTED"},
        {"path_id": "R#0", "source_id": "R", "wavelength_nm": 550.0, "parent_path_id": None,
         "termination": "DETECTED", "termination_surface_id": "image"},
        {"path_id": "B#0", "source_id": "B", "wavelength_nm": 550.0, "parent_path_id": None,
         "termination": "CLIPPED", "termination_surface_id": "AS/STOP-stop"}]}
    FANS = {"unreached": [
        {"path_id": "B#0", "source_id": "B", "wavelength_nm": 550.0, "reason": "NO_RAYS_AT_MONITOR",
         "termination": "CLIPPED", "termination_surface_id": "AS/STOP-stop"},
        {"path_id": "C#0", "source_id": "C", "wavelength_nm": 550.0, "reason": "NO_COUNTED_RAYS_AT_MONITOR",
         "termination": "ESCAPED", "termination_surface_id": None}],
        "refused": [{"path_id": "R#0", "source_id": "R", "wavelength_nm": 550.0, "code": "PUPIL_FRAME_UNRESOLVED",
                     "message": "path R#0 is reflected before the plane"}]}

    def test_unreached_refused_and_missing_sources_are_listed(self):
        entries = field_aberrations.unreached_fields(self.FANS, self.RESULT, ["A", "B", "C", "D", "R"], {"A"})
        self.assertEqual([e["source_id"] for e in entries], ["B", "C", "D", "R"])
        by_source = {e["source_id"]: e for e in entries}
        self.assertEqual(by_source["B"], {
            "path_id": "B#0", "source_id": "B", "wavelength_nm": 550.0, "termination": "CLIPPED",
            "termination_surface_id": "AS/STOP-stop", "reason": "NO_RAYS_AT_MONITOR",
            "message": "the path ends before the plane (termination CLIPPED at AS/STOP-stop)"})
        self.assertEqual(by_source["C"]["reason"], "NO_COUNTED_RAYS_AT_MONITOR")
        self.assertEqual(by_source["C"]["message"],
                         "the path meets the plane only outside its active area (termination ESCAPED)")
        self.assertEqual(by_source["R"]["reason"], "PUPIL_FRAME_UNRESOLVED")
        self.assertEqual(by_source["R"]["termination"], "DETECTED")
        self.assertIn("is reflected before the plane", by_source["R"]["message"])
        self.assertEqual(by_source["D"], {"path_id": None, "source_id": "D", "wavelength_nm": None, "termination": None,
                                          "termination_surface_id": None, "reason": "NO_PATH_IN_RUN",
                                          "message": "the Project declares this source and the run has no path of it"})

    def test_a_source_with_a_row_is_not_repeated(self):
        self.assertEqual(field_aberrations.unreached_fields({"unreached": [], "refused": []}, self.RESULT, ["A"], {"A"}),
                         [])


class EngineParaxialEfl(unittest.TestCase):
    PROJECT = {"components": [
        {"instance_id": "SRC", "optical_role": "source",
         "nominal_pose": {"position_mm": [-5.0, 0.0, 0.0], "quaternion_wxyz": [0.5, 0.5, 0.5, 0.5]}},
        {"instance_id": "L2", "optical_role": "lens",
         "nominal_pose": {"position_mm": [20.0, 0.0, 0.0], "quaternion_wxyz": [0.5, 0.5, 0.5, 0.5]}},
        {"instance_id": "L1", "optical_role": "lens",
         "nominal_pose": {"position_mm": [0.0, 0.0, 0.0], "quaternion_wxyz": [0.5, 0.5, 0.5, 0.5]}},
        {"instance_id": "BEHIND", "optical_role": "lens",
         "nominal_pose": {"position_mm": [200.0, 3.0, 0.0], "quaternion_wxyz": [0.5, 0.5, 0.5, 0.5]}}],
        "observations": [{"observation_id": "image", "pose": {"position_mm": [100.0, 0.0, 0.0]}}]}

    def test_components_on_the_axis_in_light_order(self):
        self.assertEqual(field_aberrations.axis_components(self.PROJECT, "SRC", "image"), ["L1", "L2"])

    def test_a_component_off_the_axis_is_refused(self):
        project = json.loads(json.dumps(self.PROJECT))
        project["components"][1]["nominal_pose"]["position_mm"][1] = 0.2
        with self.assertRaises(field_aberrations.FieldError) as caught:
            field_aberrations.axis_components(project, "SRC", "image")
        self.assertEqual(caught.exception.code, "EFL_NOT_STATED")
        self.assertIn("L2", str(caught.exception))

    @staticmethod
    def runner(returncode: int, reply: dict, seen: list):
        def run(argv, **_kwargs):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, returncode, stdout=json.dumps(reply), stderr="")
        return run

    SYSTEM = {"efl_mm": 50.5, "afocal": False, "rear_focal_point_mm": [70.5, 0.0, 0.0]}
    AXIS_PATH = [{"direction": [0.0, 1.0, 0.0]}, {"direction": [2.0, 0.0, 0.0]}]

    def test_the_engine_reply_gives_the_efl_its_basis_and_the_focal_plane(self):
        seen: list = []
        reference = field_aberrations.paraxial_reference(
            Path("p.json"), self.PROJECT, "SRC", "image", 550.0,
            self.runner(0, {"system": self.SYSTEM, "axis_path": self.AXIS_PATH, "optical_axis": [0.0, 1.0, 0.0]},
                        seen))
        self.assertEqual(reference["efl_mm"], 50.5)
        self.assertIn("engine paraxial EFL of L1 L2 at 550.0 nm", reference["basis"])
        self.assertEqual(reference["rear_focal_point_mm"], [70.5, 0.0, 0.0])
        # the outgoing axis is the last stretch of the axis path, as a unit vector
        self.assertEqual(reference["axis_direction"], [1.0, 0.0, 0.0])
        argv = seen[0]
        self.assertEqual(argv[argv.index("--components") + 1:argv.index("--wavelength-nm")], ["L1", "L2"])
        self.assertEqual(argv[argv.index("--source") + 1], "SRC")
        without_path = field_aberrations.paraxial_reference(
            Path("p.json"), self.PROJECT, "SRC", "image", 550.0,
            self.runner(0, {"system": self.SYSTEM, "optical_axis": [3.0, 0.0, 0.0]}, []))
        self.assertEqual(without_path["axis_direction"], [1.0, 0.0, 0.0])

    def test_an_engine_refusal_or_an_unusable_efl_is_refused(self):
        refusals = [
            (65, {"status": "ERROR", "error": {"code": "PARAXIAL_ANALYSIS_INVALID",
                                               "problems": [{"code": "PARAXIAL_ELEMENT_UNSUPPORTED"}]}}),
            (0, {"system": {"efl_mm": None, "afocal": True}}),
            (0, {"system": {"efl_mm": -40.0, "afocal": False}}),
            (0, {"system": dict(self.SYSTEM, rear_focal_point_mm=None), "axis_path": self.AXIS_PATH}),
            (0, {"system": dict(self.SYSTEM, rear_focal_point_mm=[1.0, math.inf, 0.0]), "axis_path": self.AXIS_PATH}),
            (0, {"system": self.SYSTEM}),
            (0, {"system": self.SYSTEM, "axis_path": [{"direction": [0.0, 0.0, 0.0]}]})]
        for returncode, reply in refusals:
            with self.subTest(reply=reply):
                with self.assertRaises(field_aberrations.FieldError) as caught:
                    field_aberrations.paraxial_reference(Path("p.json"), self.PROJECT, "SRC", "image", 550.0,
                                                         self.runner(returncode, reply, []))
                self.assertEqual(caught.exception.code, "EFL_NOT_STATED")
                self.assertIn("--efl", str(caught.exception))
        with self.assertRaises(field_aberrations.FieldError) as caught:
            field_aberrations.paraxial_reference(Path("p.json"), self.PROJECT, "SRC", "image", 550.0,
                                                 self.runner(*refusals[0], []))
        self.assertIn("PARAXIAL_ELEMENT_UNSUPPORTED", str(caught.exception))


class Arguments(unittest.TestCase):
    def test_bad_lengths_are_refused(self):
        paths, axes = _mapping(100.0, {0.0: 0.0, 4.0: 0.0})
        for efl in (0.0, -100.0, math.nan, math.inf):
            with self.subTest(efl=efl):
                with self.assertRaises(field_aberrations.FieldError) as caught:
                    field_aberrations.analyze(paths, axes, efl_mm=efl)
                self.assertEqual(caught.exception.code, "BAD_ARGUMENT")
        for tolerance in (math.nan, -1.0, 1.0):
            with self.subTest(axis_tolerance=tolerance):
                with self.assertRaises(field_aberrations.FieldError) as caught:
                    field_aberrations.analyze(paths, axes, efl_mm=100.0, axis_tolerance_mm=tolerance)
                self.assertEqual(caught.exception.code, "BAD_ARGUMENT")
        for tolerance in (math.nan, -1.0, math.inf):
            with self.subTest(stop_tolerance=tolerance):
                with self.assertRaises(field_aberrations.FieldError) as caught:
                    field_aberrations.analyze(paths, axes, efl_mm=100.0, stop_tolerance_mm=tolerance)
                self.assertEqual(caught.exception.code, "BAD_ARGUMENT")

    def test_the_command_refuses_bad_lengths_with_a_json_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            for argv in (["--efl", "0"], ["--efl", "-100"], ["--efl", "nan"], ["--stop-tolerance-mm", "-1"],
                         ["--axis-tolerance-mm", "1"]):
                stderr = io.StringIO()
                with self.subTest(argv=argv), contextlib.redirect_stderr(stderr):
                    code = field_aberrations.main([str(Path(tmp) / "run"), "--monitor", "image", *argv])
                self.assertEqual(code, 4)
                self.assertEqual(json.loads(stderr.getvalue().splitlines()[-1])["error"]["code"], "BAD_ARGUMENT")

    def test_a_missing_run_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = field_aberrations.main([str(Path(tmp) / "run"), "--monitor", "image"])
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(stderr.getvalue())["error"]["code"], "PROJECT_UNREADABLE")

    def test_missing_source_pose_is_refused(self):
        paths, _axes = _mapping(100.0, {0.0: 0.0, 4.0: 0.0})
        with self.assertRaises(field_aberrations.FieldError) as caught:
            field_aberrations.analyze(paths, {"F0": _axis(0.0)}, efl_mm=100.0)
        self.assertEqual(caught.exception.code, "SOURCE_POSE_MISSING")


if __name__ == "__main__":
    unittest.main()
