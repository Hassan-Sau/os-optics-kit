"""Zoom study with the engine, against an independent paraxial reference.

The design is the engine's three-group thin-lens zoom: G1 f = 100 mm fixed at x = 0, variator G2 f = -30 mm,
compensator G3 f = 50 mm, a collimated 633 nm beam, image plane at x = 200 mm, configurations wide / mid / tele at
35 / 70 / 105 mm. The reference below is this test's own thin-lens calculation, not the engine's and not the kit
solver's: for a variator position x2 the compensator position that images the G1 focus (x = 100 mm) onto the image
plane is a root of a quadratic (Gaussian lens equation), and x2 is found by bisection on the system focal length,
computed with 2x2 ray-transfer matrices.
"""

from __future__ import annotations

import contextlib
import copy
import csv
import io
import json
import math
import unittest

from tests.workflow_fixtures import ROOT, TempDir, requires_engine

F1, F2, F3, IMAGE_X = 100.0, -30.0, 50.0, 200.0
WAVELENGTH_NM = 633.0
FIXTURE = {"wide": (35.0, 26.6323, 124.7022), "mid": (70.0, 47.9331, 124.2553), "tele": (105.0, 59.9205, 132.3609)}
CASE = ROOT / "cases" / "three-group-zoom"
Q = [0.5, 0.5, 0.5, 0.5]  # local +Z along design +X


# ------------------------------------------------------------------ independent reference (no engine, no kit)

def _mul(a, b):
    return [[a[0][0] * b[0][0] + a[0][1] * b[1][0], a[0][0] * b[0][1] + a[0][1] * b[1][1]],
            [a[1][0] * b[0][0] + a[1][1] * b[1][0], a[1][0] * b[0][1] + a[1][1] * b[1][1]]]


def abcd(x2, x3):
    """System matrix of G1 at 0, G2 at x2, G3 at x3 (thin lenses, air)."""
    def lens(f):
        return [[1.0, 0.0], [-1.0 / f, 1.0]]

    def gap(d):
        return [[1.0, d], [0.0, 1.0]]

    return _mul(lens(F3), _mul(gap(x3 - x2), _mul(lens(F2), _mul(gap(x2), lens(F1)))))


def efl_and_image(x2, x3):
    m = abcd(x2, x3)
    return -1.0 / m[1][0], x3 - m[0][0] / m[1][0]


def compensator_for(x2):
    """x3 imaging the G1 focus through G2 onto the image plane, on the zoom branch (v3 = L - x3 the smaller root;
    the larger one puts G3 near G2)."""
    u2 = F1 - x2                       # the G1 focus, measured from G2 (positive: behind G2, a virtual object)
    v2 = 1.0 / (1.0 / F2 + 1.0 / u2)   # Gaussian lens equation 1/v - 1/u = 1/f
    d = IMAGE_X - (x2 + v2)            # from the G2 image to the image plane
    # v3 = image distance of G3, u3 = v3 - d: 1/v3 - 1/(v3 - d) = 1/F3  ->  v3^2 - d v3 + d F3 = 0
    v3 = 0.5 * (d - math.sqrt(d * d - 4.0 * d * F3))
    return IMAGE_X - v3


def reference_positions(efl):
    """(x2, x3) of the zoom branch at a focal length, by bisection on x2 in [5, 66] mm (EFL rises with x2)."""
    lo, hi = 5.0, 66.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if efl_and_image(mid, compensator_for(mid))[0] < efl:
            lo = mid
        else:
            hi = mid
    x2 = 0.5 * (lo + hi)
    return x2, compensator_for(x2)


# ------------------------------------------------------------------ the engine's zoom design

def _component(instance_id, x, model_id, description):
    return {"instance_id": instance_id, "optical_role": "lens", "optical_model_id": model_id,
            "custom_part": {"custom_part_key": instance_id.lower(), "description": description},
            "nominal_pose": {"position_mm": [x, 0.0, 0.0], "quaternion_wxyz": Q}}


def _source(instance_id, quaternion):
    return {"instance_id": instance_id, "optical_role": "source",
            "custom_part": {"custom_part_key": instance_id.lower(), "description": "633 nm laser"},
            "nominal_pose": {"position_mm": [-50.0, 0.0, 0.0], "quaternion_wxyz": quaternion},
            "source_model": {"model_kind": "GAUSSIAN_BEAM", "wavelength_nm": WAVELENGTH_NM, "power_w": 0.001,
                             "waist_radius_mm": 1.0, "waist_position_mm": 0.0, "m2": 1.0,
                             "polarization": {"kind": "LINEAR", "angle_deg": 0.0}}}


def _model(model_id, focal_mm):
    return {"model_id": model_id, "model": {
        "model_kind": "IDEAL_THIN_LENS", "model_version": "1", "focal_length_mm": str(focal_mm),
        "clear_radius_mm": "10", "edge_radius_mm": "12.7",
        "valid_band_nm": {"min_nm": "400", "max_nm": "1100", "status": "SPECIFIED"}, "status": "SPECIFIED"}}


def zoom_project():
    """The engine's 35/70/105 mm thin-lens zoom with its named configurations."""
    configurations = []
    for cid, (efl, x2, x3) in FIXTURE.items():
        configurations.append({"configuration_id": cid, "label": f"{efl:g} mm", "component_poses": [
            {"instance_id": "G2", "nominal_pose": {"position_mm": [x2, 0.0, 0.0], "quaternion_wxyz": Q}},
            {"instance_id": "G3", "nominal_pose": {"position_mm": [x3, 0.0, 0.0], "quaternion_wxyz": Q}}]})
    return {
        "schema": "opensci.project", "project_schema_version": "2.0", "design_intent": "OPTICAL_SYSTEM",
        "components": [_source("AXIS", Q),
                       _source("FIELD", [0.495617693783, 0.495617693783, 0.504344229281, 0.504344229281]),
                       _component("G1", 0.0, "front", "Fixed front group"),
                       _component("G2", FIXTURE["mid"][1], "variator", "Variator"),
                       _component("G3", FIXTURE["mid"][2], "compensator", "Compensator")],
        "observations": [{"observation_id": "image", "pose": {"position_mm": [IMAGE_X, 0.0, 0.0], "quaternion_wxyz": Q},
                          "aperture": {"shape": "circle", "radius_mm": 10.0}}],
        "targets": [{"target_id": "axis_spot", "metric": "beam_radius_mm", "observation_id": "image",
                     "source_id": "AXIS", "maximum": 0.025, "unit": "mm"},
                    {"target_id": "image_height_mid", "metric": "position_u_mm", "observation_id": "image",
                     "source_id": "FIELD", "target_value": 1.221855, "tolerance_abs": 0.001, "unit": "mm",
                     "configuration_ids": ["mid"]}],
        "optical_models": [_model("front", F1), _model("variator", F2), _model("compensator", F3)],
        "configurations": configurations}


def zoom_spec(**changes):
    spec = {"schema": "os-optics-kit.zoom-spec", "schema_version": 1, "wavelength_nm": WAVELENGTH_NM,
            "groups": [{"group_id": "front", "components": ["G1"], "envelope_mm": [0, 3]},
                       {"group_id": "variator", "components": ["G2"], "moving": True, "travel_mm": [10, 70],
                        "envelope_mm": [3, 3]},
                       {"group_id": "compensator", "components": ["G3"], "moving": True, "travel_mm": [100, 150],
                        "envelope_mm": [3, 3]}],
            "image_plane": {"observation_id": "image"},
            "efl_mm": {"start": 35, "stop": 105, "step": 5},
            "anchors": [{"efl_mm": efl, "configuration_id": cid} for cid, (efl, _, _) in FIXTURE.items()],
            "tolerances": {"efl_mm": 0.01, "focus_mm": 0.005}, "clearance": {"min_mm": 1.0}}
    spec.update(changes)
    return spec


def _zoom(spec, out, base, extra=()):
    from os_optics_kit.cli import main

    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(["zoom", str(spec), "--out", str(out), "--base", str(base), *extra])
    return code, (json.loads(stdout.getvalue()) if stdout.getvalue().strip() else None), stderr.getvalue()


@requires_engine
class ZoomEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = TempDir()
        cls.root = cls.tmp.__enter__()
        cls.base = cls.root / "zoom_project.json"
        cls.base.write_text(json.dumps(zoom_project()), encoding="utf-8")
        cls.spec = cls.root / "zoom.json"
        cls.spec.write_text(json.dumps(zoom_spec()), encoding="utf-8")
        cls.code, cls.summary, cls.stderr = _zoom(cls.spec, cls.root / "out", cls.base)
        cls.trajectory = json.loads((cls.root / "out" / "trajectory.json").read_text(encoding="utf-8"))
        cls.samples = {s["efl_requested_mm"]: s for s in cls.trajectory["samples"]}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.__exit__(None, None, None)

    def test_reference_reproduces_the_engine_fixture(self):
        for efl, x2, x3 in FIXTURE.values():
            rx2, rx3 = reference_positions(efl)
            self.assertAlmostEqual(rx2, x2, delta=1e-4)
            self.assertAlmostEqual(rx3, x3, delta=1e-4)

    def test_every_sample_passes(self):
        self.assertEqual(self.code, 0, self.stderr)
        self.assertEqual(self.summary["status"], "PASS")
        self.assertEqual(self.summary["counts"], {"PASS": 15, "FAIL": 0, "INFEASIBLE": 0, "UNSOLVED": 0})
        self.assertEqual(self.summary["coverage"]["covered_fraction"], 1.0)
        self.assertEqual(self.summary["engine"]["engine_version"].split(".")[:2], ["0", "14"])

    def test_reference_positions_at_the_three_configurations(self):
        for cid, (efl, x2, x3) in FIXTURE.items():
            sample = self.samples[efl]
            self.assertEqual(sample["configuration_id"], cid)
            rx2, rx3 = reference_positions(efl)
            self.assertAlmostEqual(sample["positions_mm"]["variator"], rx2, delta=1e-5)
            self.assertAlmostEqual(sample["positions_mm"]["compensator"], rx3, delta=1e-5)
            self.assertAlmostEqual(sample["positions_mm"]["variator"], x2, delta=1e-3)
            self.assertAlmostEqual(sample["positions_mm"]["compensator"], x3, delta=1e-3)
        checks = [a["branch_check"] for a in self.trajectory["anchors"][1:]]
        self.assertTrue(all(c["ok"] for c in checks), checks)

    def test_intermediate_samples_hold_focus_on_the_same_image_plane(self):
        self.assertEqual(self.trajectory["image_plane"]["position_along_axis_mm"], IMAGE_X)
        for efl, sample in self.samples.items():
            x2, x3 = sample["positions_mm"]["variator"], sample["positions_mm"]["compensator"]
            value, image = efl_and_image(x2, x3)  # the reported positions, through the independent model
            self.assertAlmostEqual(value, efl, delta=1e-5, msg=efl)
            self.assertAlmostEqual(image - IMAGE_X, 0.0, delta=1e-5, msg=efl)
            self.assertAlmostEqual(sample["efl_achieved_mm"], value, delta=1e-6)          # engine vs reference
            self.assertAlmostEqual(sample["focus_error_mm"], image - IMAGE_X, delta=1e-6)
            self.assertLessEqual(abs(sample["focus_error_mm"]), 0.005)
            rx2, rx3 = reference_positions(efl)
            self.assertAlmostEqual(x2, rx2, delta=1e-5)
            self.assertAlmostEqual(x3, rx3, delta=1e-5)
            # least clearance is front envelope (3 mm after G1) to variator envelope (3 mm before G2)
            self.assertAlmostEqual(sample["min_clearance_mm"], x2 - 6.0, delta=1e-9)
        compensator = [self.samples[e]["positions_mm"]["compensator"] for e in sorted(self.samples)]
        self.assertLess(min(compensator), compensator[0])
        self.assertLess(min(compensator), compensator[-1])  # the compensator reverses inside the range

    def test_generated_project_runs_by_configuration(self):
        from os_optics.local import json_api
        from os_optics.local.configurations import configured_project

        document = json.loads((self.root / "out" / "project.json").read_text(encoding="utf-8"))
        ids = [c["configuration_id"] for c in document["configurations"]]
        self.assertEqual(len(ids), 15)
        self.assertEqual([ids[0], ids[7], ids[-1]], ["wide", "mid", "tele"])
        self.assertIn("efl_040p00", ids)
        self.assertEqual([t["target_id"] for t in document["targets"]], ["axis_spot", "image_height_mid"])
        self.assertTrue(json_api.check_project(document)["ok"])
        configured = configured_project(document, "efl_085p00").payload()
        g3 = next(c for c in configured["components"] if c["instance_id"] == "G3")
        self.assertAlmostEqual(g3["nominal_pose"]["position_mm"][0], reference_positions(85.0)[1], delta=1e-5)
        reply = json_api.paraxial_analysis(document, {"components": ["G1", "G2", "G3"], "wavelength_nm": 633,
                                                      "configuration_id": "efl_085p00"})
        self.assertAlmostEqual(reply["system"]["efl_mm"], 85.0, delta=1e-6)
        self.assertAlmostEqual(reply["system"]["rear_focal_point_mm"][0], IMAGE_X, delta=1e-6)
        rows = list(csv.DictReader(io.StringIO((self.root / "out" / "trajectory.csv").read_text(encoding="utf-8"))))
        self.assertEqual(len(rows), 15)
        self.assertIn("position_mm:compensator", rows[0])
        self.assertIn("| wide | PASS |", (self.root / "out" / "report.md").read_text(encoding="utf-8").replace(
            "| z001 ", ""))

    def test_existing_output_is_refused_without_overwrite(self):
        code, summary, stderr = _zoom(self.spec, self.root / "out", self.base)
        self.assertEqual(code, 4)
        self.assertIn("OUTPUT_EXISTS", stderr)

    def test_violated_travel_and_clearance_are_reported_not_clipped(self):
        spec = zoom_spec(efl_mm={"start": 20, "stop": 120, "step": 5})
        spec["groups"][1]["travel_mm"] = [15, 60]
        spec["groups"][2]["travel_mm"] = [120, 132]
        path = self.root / "constrained.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        code, summary, stderr = _zoom(path, self.root / "constrained", self.base)
        self.assertEqual(code, 1, stderr)
        self.assertEqual(summary["status"], "FAIL")
        self.assertEqual(summary["counts"], {"PASS": 15, "FAIL": 0, "INFEASIBLE": 6, "UNSOLVED": 0})
        trajectory = json.loads((self.root / "constrained" / "trajectory.json").read_text(encoding="utf-8"))
        samples = {s["efl_requested_mm"]: s for s in trajectory["samples"]}
        wide_end = samples[20.0]
        codes = {r["code"]: r for r in wide_end["reasons"]}
        self.assertEqual(set(codes), {"TRAVEL_EXCEEDED", "CLEARANCE_VIOLATED"})
        rx2, rx3 = reference_positions(20.0)
        self.assertAlmostEqual(codes["TRAVEL_EXCEEDED"]["position_mm"], rx2, delta=1e-5)  # unclipped
        self.assertLess(codes["TRAVEL_EXCEEDED"]["position_mm"], 15.0)
        self.assertEqual(codes["CLEARANCE_VIOLATED"]["between"], ["front", "variator"])
        self.assertAlmostEqual(codes["CLEARANCE_VIOLATED"]["clearance_mm"], rx2 - 6.0, delta=1e-5)
        tele = samples[105.0]
        self.assertEqual(tele["status"], "INFEASIBLE")
        self.assertEqual([(r["code"], r["group_id"]) for r in tele["reasons"]], [("TRAVEL_EXCEEDED", "compensator")])
        self.assertGreater(tele["positions_mm"]["compensator"], 132.0)
        for efl in (110.0, 115.0, 120.0):
            self.assertEqual(sorted(r["group_id"] for r in samples[efl]["reasons"]), ["compensator", "variator"])
        for efl, sample in samples.items():  # infeasible or not, each one is solved and in the Project
            self.assertIsNotNone(sample["configuration_id"])
            self.assertAlmostEqual(sample["efl_error_mm"], 0.0, delta=1e-6)
            self.assertAlmostEqual(sample["focus_error_mm"], 0.0, delta=1e-6)
        self.assertEqual(trajectory["coverage"]["pass_intervals_mm"], [[30.0, 100.0]])
        self.assertAlmostEqual(trajectory["coverage"]["covered_fraction"], 0.7)
        self.assertEqual(trajectory["project"]["configurations"], 21)

    def test_study_runs_every_configuration(self):
        spec = zoom_spec(efl_mm={"values": [35, 52.5, 70, 105]})
        path = self.root / "study.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        code, summary, stderr = _zoom(path, self.root / "with-study", self.base, ("--study",))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(summary["study"]["status"], "COMPLETED")
        self.assertEqual(summary["study"]["counts"]["by_outcome"]["PASS"], 4)
        table = list(csv.DictReader(io.StringIO(
            (self.root / "with-study" / "study" / "table.csv").read_text(encoding="utf-8"))))
        self.assertEqual([float(r["param:efl_mm"]) for r in table], [35.0, 52.5, 70.0, 105.0])
        for row in table:  # the traced focus: w = lambda f / (pi w0) for a 1 mm waist, at the fixed image plane
            efl = float(row["param:efl_mm"])
            self.assertAlmostEqual(float(row["target:axis_spot"]), WAVELENGTH_NM * 1e-6 * efl / math.pi,
                                   delta=2e-4)
        mid = next(r for r in table if float(r["param:efl_mm"]) == 70.0)
        self.assertEqual(mid["target:image_height_mid:assessment"], "MET")


@requires_engine
class CaseSpecTests(unittest.TestCase):
    """The case's two specs give the counts its README states."""

    def test_case_specs(self):
        base = copy.deepcopy(json.loads((CASE / "project.json").read_text(encoding="utf-8")))
        self.assertNotIn("configurations", base)
        with TempDir() as tmp:
            code, summary, stderr = _zoom(CASE / "studies" / "zoom.json", tmp / "zoom", CASE / "project.json")
            self.assertEqual(code, 0, stderr)
            self.assertEqual(summary["counts"]["PASS"], 29)
            self.assertEqual([a["label"] for a in summary["anchors"]], ["wide", "mid", "tele"])
            # every anchor lies in 35-105 mm: the full range is reported as before, nothing apart
            self.assertEqual(summary["coverage"], {"requested_range_mm": [35.0, 105.0], "samples": 29,
                                                   "max_step_mm": 2.5, "covered_fraction": 1.0,
                                                   "pass_intervals_mm": [[35.0, 105.0]]})
            self.assertEqual([(a["in_requested_range"], a["sample_id"]) for a in summary["anchors"]],
                             [(True, "z001"), (True, "z015"), (True, "z029")])
            self.assertEqual(summary["configurations"], 29)
            trajectory = json.loads((tmp / "zoom" / "trajectory.json").read_text(encoding="utf-8"))
            self.assertEqual(trajectory["anchors_outside_range"], [])
            self.assertEqual(len(trajectory["samples"]), 29)
            code, summary, stderr = _zoom(CASE / "studies" / "zoom_constrained.json", tmp / "constrained",
                                          CASE / "project.json")
            self.assertEqual(code, 1, stderr)
            self.assertEqual(summary["counts"], {"PASS": 15, "FAIL": 0, "INFEASIBLE": 6, "UNSOLVED": 0})
            self.assertEqual(summary["coverage"]["pass_intervals_mm"], [[30.0, 100.0]])
            self.assertAlmostEqual(summary["coverage"]["covered_fraction"], 0.7)
            self.assertEqual(summary["configurations"], 21)


def _case_spec(**changes):
    """The case's zoom spec (anchors 35 / 70 / 105 mm) with changes."""
    spec = json.loads((CASE / "studies" / "zoom.json").read_text(encoding="utf-8"))
    spec.update(changes)
    return spec


@requires_engine
class RequestedRangeTests(unittest.TestCase):
    """An anchor outside the requested range seeds the solve and is reported apart from the requested samples."""

    def run_case(self, tmp, spec, extra=()):
        path = tmp / "spec.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        code, summary, stderr = _zoom(path, tmp / "out", CASE / "project.json", extra)
        trajectory = json.loads((tmp / "out" / "trajectory.json").read_text(encoding="utf-8"))
        return code, summary, stderr, trajectory

    def test_shortened_range_is_scoped_to_the_requested_samples(self):
        with TempDir() as tmp:
            code, summary, stderr, trajectory = self.run_case(
                tmp, _case_spec(efl_mm={"start": 35, "stop": 70, "step": 5}), ("--study",))
            self.assertEqual(code, 0, stderr)
            self.assertEqual(summary["status"], "PASS")
            self.assertEqual(summary["counts"], {"PASS": 8, "FAIL": 0, "INFEASIBLE": 0, "UNSOLVED": 0})
            self.assertEqual(summary["coverage"], {"requested_range_mm": [35.0, 70.0], "samples": 8,
                                                   "max_step_mm": 5.0, "covered_fraction": 1.0,
                                                   "pass_intervals_mm": [[35.0, 70.0]]})
            self.assertEqual(trajectory["coverage"]["min_step_mm"], 5.0)
            self.assertEqual(trajectory["coverage"]["not_pass_runs"], [])
            self.assertEqual([s["efl_requested_mm"] for s in trajectory["samples"]],
                             [35.0, 40.0, 45.0, 50.0, 55.0, 60.0, 65.0, 70.0])
            self.assertTrue(all(s["in_requested_range"] for s in trajectory["samples"]))
            # the 105 mm anchor: solved, evaluated and written, reported apart
            (tele,) = trajectory["anchors_outside_range"]
            self.assertEqual((tele["sample_id"], tele["configuration_id"], tele["efl_requested_mm"], tele["status"]),
                             ("a001", "tele", 105.0, "PASS"))
            self.assertFalse(tele["in_requested_range"])
            rx2, rx3 = reference_positions(105.0)
            self.assertAlmostEqual(tele["positions_mm"]["variator"], rx2, delta=1e-5)
            self.assertAlmostEqual(tele["positions_mm"]["compensator"], rx3, delta=1e-5)
            anchors = {a["label"]: a for a in trajectory["anchors"]}
            self.assertEqual((anchors["tele"]["in_requested_range"], anchors["tele"]["sample_id"]), (False, "a001"))
            self.assertTrue(anchors["tele"]["branch_check"]["ok"])  # the march 70 -> 105 still checks the branch
            self.assertEqual((anchors["mid"]["in_requested_range"], anchors["mid"]["sample_id"]), (True, "z008"))
            self.assertEqual({a["label"]: a["in_requested_range"] for a in summary["anchors"]},
                             {"wide": True, "mid": True, "tele": False})
            self.assertEqual(trajectory["worst"]["efl_error_mm"]["sample_id"][0], "z")
            # the Project holds the anchor's configuration, marked; the table and the study hold the requested samples
            document = json.loads((tmp / "out" / "project.json").read_text(encoding="utf-8"))
            labels = {c["configuration_id"]: c["label"] for c in document["configurations"]}
            self.assertEqual(len(labels), 9)
            self.assertEqual(summary["configurations"], 9)
            self.assertIn("outside the requested range", labels["tele"])
            self.assertNotIn("outside", labels["mid"])
            rows = list(csv.DictReader(io.StringIO((tmp / "out" / "trajectory.csv").read_text(encoding="utf-8"))))
            self.assertEqual([float(r["efl_requested_mm"]) for r in rows], [35.0, 40.0, 45.0, 50.0, 55.0, 60.0, 65.0,
                                                                            70.0])
            report = (tmp / "out" / "report.md").read_text(encoding="utf-8")
            self.assertIn("## Anchors outside the requested range", report)
            self.assertIn("| a001 | tele | PASS | 105 |", report)
            self.assertIn("PASS intervals (mm): 35-70.", report)
            self.assertEqual(summary["study"]["counts"]["by_outcome"]["PASS"], 8)
            table = list(csv.DictReader(io.StringIO(
                (tmp / "out" / "study" / "table.csv").read_text(encoding="utf-8"))))
            self.assertEqual([float(r["param:efl_mm"]) for r in table], [35.0, 40.0, 45.0, 50.0, 55.0, 60.0, 65.0,
                                                                         70.0])

    def test_an_outside_anchor_does_not_decide_the_status(self):
        spec = _case_spec(efl_mm={"start": 35, "stop": 70, "step": 5})
        spec["groups"][2]["travel_mm"] = [100, 130]  # the 105 mm anchor needs the compensator at 132.36 mm
        with TempDir() as tmp:
            code, summary, stderr, trajectory = self.run_case(tmp, spec)
            self.assertEqual(code, 0, stderr)
            self.assertEqual(summary["status"], "PASS")
            self.assertEqual(summary["counts"], {"PASS": 8, "FAIL": 0, "INFEASIBLE": 0, "UNSOLVED": 0})
            self.assertEqual(summary["not_pass"], [])
            (tele,) = trajectory["anchors_outside_range"]
            self.assertEqual(tele["status"], "INFEASIBLE")
            self.assertEqual([(r["code"], r["group_id"]) for r in tele["reasons"]], [("TRAVEL_EXCEEDED", "compensator")])
            self.assertEqual({a["label"]: a["status"] for a in summary["anchors"]},
                             {"wide": "PASS", "mid": "PASS", "tele": "INFEASIBLE"})

    def test_constrained_range_with_a_gap(self):
        # 35-70 mm with the compensator held at >= 123.25 mm: near its reversal (123.17 mm at 55 mm) it leaves its
        # travel at 50 and 55 mm (reference 123.223 and 123.174 mm), and the 105 mm anchor (132.36 mm) leaves it too
        spec = _case_spec(efl_mm={"start": 35, "stop": 70, "step": 5})
        spec["groups"][2]["travel_mm"] = [123.25, 130]
        for efl in (45.0, 60.0):
            self.assertGreater(reference_positions(efl)[1], 123.25)
        for efl in (50.0, 55.0):
            self.assertLess(reference_positions(efl)[1], 123.25)
        with TempDir() as tmp:
            code, summary, stderr, trajectory = self.run_case(tmp, spec)
            self.assertEqual(code, 1, stderr)
            self.assertEqual(summary["status"], "FAIL")
            self.assertEqual(summary["counts"], {"PASS": 6, "FAIL": 0, "INFEASIBLE": 2, "UNSOLVED": 0})
            cov = trajectory["coverage"]
            self.assertEqual(cov["requested_range_mm"], [35.0, 70.0])
            self.assertEqual(cov["pass_intervals_mm"], [[35.0, 45.0], [60.0, 70.0]])
            self.assertAlmostEqual(cov["covered_fraction"], 20.0 / 35.0)
            self.assertEqual([(r["first_mm"], r["last_mm"], r["samples"], r["statuses"]) for r in cov["not_pass_runs"]],
                             [(50.0, 55.0, 2, ["INFEASIBLE"])])
            self.assertEqual((cov["samples"], cov["min_step_mm"], cov["max_step_mm"]), (8, 5.0, 5.0))
            self.assertEqual([n["efl_requested_mm"] for n in summary["not_pass"]], [50.0, 55.0])
            (tele,) = trajectory["anchors_outside_range"]
            self.assertEqual((tele["efl_requested_mm"], tele["status"]), (105.0, "INFEASIBLE"))


if __name__ == "__main__":
    unittest.main()
