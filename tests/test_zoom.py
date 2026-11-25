"""Zoom study without the engine: spec validation, the continuation solver on closed-form models, constraint
classification, coverage and the command's help.

The solver is model-agnostic (``model(positions) -> (efl_mm, focus_mm)``), so these tests drive it with an
independent thin-lens ABCD model of a three-group zoom (G1 f = 100 mm fixed at 0, G2 f = -30 mm, G3 f = 50 mm,
object at infinity, image plane at 200 mm) and with a model whose focal length folds back.
"""

from __future__ import annotations

import math
import subprocess
import sys
import unittest

from os_optics_kit import zoom_study as zs
from os_optics_kit import workflow_common as wc

F1, F2, F3, IMAGE = 100.0, -30.0, 50.0, 200.0
ANCHORS = {35.0: (26.6323, 124.7022), 70.0: (47.9331, 124.2553), 105.0: (59.9205, 132.3609)}
SOLVER = dict(zs.SOLVER_DEFAULTS)


def _mul(a, b):
    return [[a[0][0] * b[0][0] + a[0][1] * b[1][0], a[0][0] * b[0][1] + a[0][1] * b[1][1]],
            [a[1][0] * b[0][0] + a[1][1] * b[1][0], a[1][0] * b[0][1] + a[1][1] * b[1][1]]]


def thin_lens_zoom(positions):
    """(EFL, rear focal point) of G1 at 0, G2 at x2, G3 at x3 by ray-transfer matrices."""
    x2, x3 = positions

    def lens(f):
        return [[1.0, 0.0], [-1.0 / f, 1.0]]

    def gap(d):
        return [[1.0, d], [0.0, 1.0]]

    m = _mul(lens(F3), _mul(gap(x3 - x2), _mul(lens(F2), _mul(gap(x2), lens(F1)))))
    c = m[1][0]
    if c == 0:
        raise zs.ModelFailure("AFOCAL", "afocal")
    return -1.0 / c, x3 - m[0][0] / c


def fold_model(positions):
    """EFL = 60 - (x0 - 40)^2 / 10 (largest 60 at x0 = 40, two branches below it); focus = x1."""
    x0, x1 = positions
    return 60.0 - (x0 - 40.0) ** 2 / 10.0, x1


def _anchors(table):
    return [{"efl_mm": e, "declared_mm": list(p), "label": str(e), "configuration_id": None, "source": "test"}
            for e, p in sorted(table.items())]


def _base():
    def comp(cid, role="lens"):
        return {"instance_id": cid, "optical_role": role,
                "nominal_pose": {"position_mm": [0.0, 0.0, 0.0], "quaternion_wxyz": [0.5, 0.5, 0.5, 0.5]}}
    return {"components": [comp("SRC", "source"), comp("G1"), comp("G2"), comp("G3")],
            "observations": [{"observation_id": "image",
                              "pose": {"position_mm": [200.0, 0.0, 0.0], "quaternion_wxyz": [0.5, 0.5, 0.5, 0.5]}}],
            "configurations": [{"configuration_id": "wide", "component_poses": []}]}


def _spec(**changes):
    spec = {"wavelength_nm": 633,
            "groups": [{"group_id": "front", "components": ["G1"]},
                       {"group_id": "variator", "components": ["G2"], "moving": True, "travel_mm": [10, 70]},
                       {"group_id": "compensator", "components": ["G3"], "moving": True, "travel_mm": [100, 150],
                        "envelope_mm": [3, 3]}],
            "image_plane": {"observation_id": "image"},
            "efl_mm": {"start": 35, "stop": 105, "step": 2.5},
            "anchors": [{"efl_mm": 35, "configuration_id": "wide"},
                        {"efl_mm": 70, "positions_mm": {"variator": 47.9331, "compensator": 124.2553}}],
            "tolerances": {"efl_mm": 0.01, "focus_mm": 0.005}}
    spec.update(changes)
    return spec


class SpecTests(unittest.TestCase):
    def refused(self, code, spec, base=None):
        with self.assertRaises(wc.WorkflowError) as caught:
            zs.normalize_spec(spec, base or _base())
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        return caught.exception

    def test_valid_spec_is_normalized(self):
        normalized = zs.normalize_spec(_spec(), _base())
        self.assertEqual(normalized["moving_groups"], ["variator", "compensator"])
        self.assertEqual(len(normalized["efl_mm"]), 29)
        self.assertEqual(normalized["efl_mm"][0], 35.0)
        self.assertEqual(normalized["efl_mm"][-1], 105.0)
        self.assertEqual(normalized["image_plane"]["position_mm"], [200.0, 0.0, 0.0])
        self.assertEqual(normalized["groups"][2]["envelope_mm"], [3.0, 3.0])
        self.assertEqual(normalized["groups"][0]["envelope_mm"], [0.0, 0.0])
        self.assertIsNone(normalized["anchors"][0]["declared_mm"])  # read from configuration 'wide' later
        self.assertEqual(normalized["anchors"][1]["declared_mm"], [47.9331, 124.2553])
        self.assertEqual(normalized["solver"], zs.SOLVER_DEFAULTS)

    def test_range_includes_its_stop(self):
        self.assertEqual(zs.sample_efls({"start": 35, "stop": 40, "step": 2}), [35.0, 37.0, 39.0, 40.0])
        self.assertEqual(zs.sample_efls({"values": [70, 35, 70]}), [35.0, 70.0])

    def test_two_moving_groups_are_required(self):
        groups = _spec()["groups"]
        groups[2] = {"group_id": "compensator", "components": ["G3"]}
        error = self.refused("SPEC_INVALID", _spec(groups=groups))
        self.assertIn("exactly two", str(error))

    def test_unknown_component_and_configuration(self):
        groups = _spec()["groups"]
        groups[0] = {"group_id": "front", "components": ["G9"]}
        self.refused("UNKNOWN_ID", _spec(groups=groups))
        self.refused("UNKNOWN_ID", _spec(anchors=[{"efl_mm": 35, "configuration_id": "nope"}]))
        self.refused("UNKNOWN_ID", _spec(image_plane={"observation_id": "nope"}))

    def test_refusals(self):
        self.refused("SPEC_INVALID", _spec(anchors=[]))
        self.refused("SPEC_INVALID", _spec(tolerances={"efl_mm": 0.01}))
        self.refused("SPEC_INVALID", _spec(efl_mm={"start": 35, "stop": 105, "step": 0}))
        self.refused("SPEC_INVALID", _spec(efl_mm={"values": [-10, 10]}))
        self.refused("SPEC_INVALID", _spec(solver={"newton": 1}))
        self.refused("SPEC_INVALID", _spec(solver={"tolerance_mm": 0.1}))
        self.refused("SPEC_INVALID", _spec(anchors=[{"efl_mm": 35, "positions_mm": {"variator": 1}}]))
        groups = _spec()["groups"]
        groups[1] = {"group_id": "variator", "components": ["G2"], "moving": True}
        self.refused("SPEC_INVALID", _spec(groups=groups))
        groups = _spec()["groups"]
        groups[0] = {"group_id": "front", "components": ["SRC"]}
        self.refused("SPEC_INVALID", _spec(groups=groups))


class ContinuationTests(unittest.TestCase):
    def test_thin_lens_zoom_reaches_every_sample_on_one_branch(self):
        efls = zs.sample_efls({"start": 35, "stop": 105, "step": 2.5})
        results, anchors = zs.continuation(thin_lens_zoom, efls, _anchors(ANCHORS), IMAGE, SOLVER)
        self.assertEqual(sorted(results), efls)
        for efl, result in results.items():
            self.assertTrue(result["ok"], (efl, result.get("code")))
            value, focus = thin_lens_zoom(result["positions"])
            self.assertAlmostEqual(value, efl, delta=1e-6)
            self.assertAlmostEqual(focus, IMAGE, delta=1e-6)
        for anchor, declared in zip(anchors, sorted(ANCHORS.items())):
            self.assertTrue(anchor["ok"])
            for got, want in zip(anchor["positions"], declared[1]):
                self.assertAlmostEqual(got, want, delta=1e-3)  # the declared positions are rounded to 0.1 um
        self.assertIsNone(anchors[0].get("branch_check"))
        for anchor in anchors[1:]:
            self.assertTrue(anchor["branch_check"]["ok"], anchor["branch_check"])
            self.assertLess(anchor["branch_check"]["deviation_mm"], 1e-6)
        compensator = [results[e]["positions"][1] for e in efls]
        turn = compensator.index(min(compensator))
        self.assertTrue(0 < turn < len(efls) - 1, "the compensator reverses inside the range")
        self.assertTrue(all(a > b for a, b in zip(compensator[:turn], compensator[1:turn + 1])))
        self.assertTrue(all(a < b for a, b in zip(compensator[turn:-1], compensator[turn + 1:])))

    def test_samples_beyond_a_fold_are_unsolved_and_kept(self):
        efls = [40.0, 45.0, 50.0, 55.0, 59.0, 62.0, 65.0]
        results, _ = zs.continuation(fold_model, efls, _anchors({50.0: (30.0, 200.0)}), 200.0, SOLVER)
        self.assertEqual(sorted(results), efls)
        for efl in (40.0, 45.0, 50.0, 55.0, 59.0):
            self.assertTrue(results[efl]["ok"], efl)
            self.assertLess(results[efl]["positions"][0], 40.0)  # the branch of the anchor
        for efl in (62.0, 65.0):
            self.assertFalse(results[efl]["ok"], efl)
            self.assertIn(results[efl]["code"], ("NOT_CONVERGED", "SINGULAR_JACOBIAN", "BRANCH_JUMP"))
            self.assertTrue(results[efl]["message"])

    def test_anchor_on_another_branch_is_a_branch_mismatch(self):
        lower = 40.0 - math.sqrt(10.0 * 15.0)  # EFL 45 on the branch x0 < 40
        upper = 40.0 + math.sqrt(10.0 * 10.0)  # EFL 50 on the branch x0 > 40
        results, anchors = zs.continuation(fold_model, [45.0, 47.5, 50.0],
                                           _anchors({45.0: (lower, 200.0), 50.0: (upper, 200.0)}), 200.0, SOLVER)
        check = anchors[1]["branch_check"]
        self.assertFalse(check["ok"])
        self.assertGreater(check["deviation_mm"], 10.0)
        self.assertAlmostEqual(results[50.0]["positions"][0], upper, delta=1e-6)  # the anchor's own solve stands

    def test_no_converging_anchor_leaves_every_sample_unsolved(self):
        results, anchors = zs.continuation(fold_model, [70.0, 75.0], _anchors({70.0: (30.0, 200.0)}), 200.0, SOLVER)
        self.assertFalse(anchors[0]["ok"])
        self.assertEqual({r["code"] for e, r in results.items() if e != 70.0}, {"NO_SEED"})
        self.assertFalse(results[70.0]["ok"])

    def test_newton_reports_a_singular_jacobian(self):
        result = zs.newton(fold_model, 65.0, 200.0, [40.0, 200.0], SOLVER)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "SINGULAR_JACOBIAN")

    def test_tangent_of_the_solution_curve(self):
        self.assertEqual(zs.tangent([[2.0, 0.0], [0.0, 1.0]]), [0.5, 0.0])
        self.assertIsNone(zs.tangent([[1.0, 1.0], [1.0, 1.0]]))
        self.assertIsNone(zs.tangent(None))


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.normalized = zs.normalize_spec(_spec(clearance={"min_mm": 1.0}), _base())

    def test_travel_and_clearance_are_reported_unclipped(self):
        gaps = [{"between": ["front", "variator"], "clearance_mm": -0.2},
                {"between": ["variator", "compensator"], "clearance_mm": 50.0},
                {"between": ["compensator", "image_plane"], "clearance_mm": 60.0}]
        reasons = zs.constraint_reasons(self.normalized, {"variator": 5.8, "compensator": 128.6}, gaps, 0.0, 0.0)
        self.assertEqual([r["code"] for r in reasons], ["TRAVEL_EXCEEDED", "CLEARANCE_VIOLATED"])
        self.assertEqual(reasons[0]["position_mm"], 5.8)
        self.assertAlmostEqual(reasons[0]["excess_mm"], 4.2)
        self.assertAlmostEqual(reasons[1]["shortfall_mm"], 1.2)
        self.assertEqual(zs.sample_status(True, reasons), "INFEASIBLE")
        self.assertIn("variator 5.800 mm outside [10, 70]", zs.reason_label(reasons[0]))

    def test_tolerances_and_status(self):
        gaps = [{"between": ["compensator", "image_plane"], "clearance_mm": 60.0}]
        positions = {"variator": 40.0, "compensator": 125.0}
        self.assertEqual(zs.constraint_reasons(self.normalized, positions, gaps, 0.009, -0.004), [])
        reasons = zs.constraint_reasons(self.normalized, positions, gaps, 0.02, 0.006)
        self.assertEqual([r["code"] for r in reasons], ["EFL_OUT_OF_TOLERANCE", "FOCUS_OUT_OF_TOLERANCE"])
        self.assertEqual(zs.sample_status(True, reasons), "FAIL")
        self.assertEqual(zs.sample_status(True, []), "PASS")
        self.assertEqual(zs.sample_status(False, [{"code": "NOT_CONVERGED"}]), "UNSOLVED")

    def test_coverage_counts_only_spans_between_pass_samples(self):
        statuses = {20.0: "INFEASIBLE", 25.0: "PASS", 30.0: "PASS", 35.0: "UNSOLVED", 40.0: "PASS", 45.0: "PASS",
                    50.0: "PASS", 55.0: "INFEASIBLE", 60.0: "INFEASIBLE"}
        samples = [{"efl_requested_mm": e, "status": s} for e, s in statuses.items()]
        cov = zs.coverage(samples, list(statuses))
        self.assertAlmostEqual(cov["covered_fraction"], 15.0 / 40.0)
        self.assertEqual(cov["pass_intervals_mm"], [[25.0, 30.0], [40.0, 50.0]])
        self.assertEqual([(r["first_mm"], r["last_mm"], r["samples"]) for r in cov["not_pass_runs"]],
                         [(20.0, 20.0, 1), (35.0, 35.0, 1), (55.0, 60.0, 2)])
        self.assertEqual(cov["max_step_mm"], 5.0)

    def test_configuration_ids_are_unique_and_valid(self):
        used = {"wide"}
        self.assertEqual(zs.configuration_id(42.5, used), "efl_042p50")
        self.assertEqual(zs.configuration_id(42.5, used), "efl_042p50_2")
        self.assertEqual(zs.configuration_id(-35.0, used), "efl_m035p00")


class CommandTests(unittest.TestCase):
    def test_help_needs_no_engine(self):
        code = ("import sys; sys.modules['os_optics'] = None\n"
                "from os_optics_kit.cli import main\n"
                "raise SystemExit(main(['zoom', '--help']))")
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("os-optics-kit zoom", done.stdout)
        self.assertIn("UNSOLVED", done.stdout)

    def test_unreadable_spec_is_refused_with_exit_4(self):
        done = subprocess.run([sys.executable, "-m", "os_optics_kit", "zoom", "missing-spec.json", "--out", "x"],
                              capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(done.returncode, 4)
        self.assertIn('"error"', done.stderr)


if __name__ == "__main__":
    unittest.main()
