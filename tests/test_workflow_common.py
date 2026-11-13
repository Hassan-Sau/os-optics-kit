"""Outcome classes of os_optics_kit.workflow_common.classify (no Engine run needed)."""

import unittest

from os_optics_kit import workflow_common as wc


def row(target_id, status, value=1.0):
    return {"id": target_id, "status": status, "value": value, "assessment": {"PASS": "MET", "WARNING": "MET", "FAIL": "UNMET", "UNKNOWN": "INDETERMINATE"}.get(status)}


class ClassifyTests(unittest.TestCase):
    def test_catalog_identity_uses_saved_results_only(self):
        self.assertEqual(wc.run_catalog_identity([{"result_hash": "a", "catalog_version": None},
                                                  {"result_hash": None, "catalog_version": "installed-only"}]),
                         {"catalog_version": None, "catalog_versions": [None]})
        mixed = wc.run_catalog_identity([{"result_hash": "a", "catalog_version": "release-a"},
                                         {"result_hash": "b", "catalog_version": "release-b"}])
        self.assertEqual(mixed, {"catalog_version": None, "catalog_versions": ["release-a", "release-b"]})

    def test_precedence(self):
        self.assertEqual(wc.classify(None, [], []), "ERROR")
        self.assertEqual(wc.classify("FAIL", ["MODEL_WAVELENGTH_OUTSIDE_BAND"], [row("a", "FAIL")]), "FAIL")
        self.assertEqual(wc.classify("PASS", ["MODEL_WAVELENGTH_OUTSIDE_BAND"], [row("a", "PASS")]), "PASS")
        self.assertEqual(wc.classify("FAIL", [], [row("a", "PASS"), row("b", "UNKNOWN", None)]), "UNKNOWN")
        self.assertEqual(wc.classify("FAIL", [], [row("a", "PASS"), row("b", "FAIL")]), "FAIL")
        self.assertEqual(wc.classify("WARNING", [], [row("a", "WARNING")]), "PASS")

    def test_selection_decides(self):
        rows = [row("a", "PASS"), row("b", "FAIL")]
        self.assertEqual(wc.classify("FAIL", [], rows, selected=["a"]), "PASS")
        self.assertEqual(wc.classify("FAIL", [], rows, selected=["c"]), "UNKNOWN")

    def test_nothing_evaluated_is_never_pass(self):
        for status in ("PASS", "WARNING", "NOT_APPLICABLE", "FAIL", "UNKNOWN"):
            self.assertEqual(wc.classify(status, [], []), "NOT_APPLICABLE", status)
        self.assertEqual(wc.classify("PASS", [], [row("a", "PASS")], selected=[]), "NOT_APPLICABLE")

    def test_not_applicable_target_beside_met_ones_is_not_a_pass(self):
        """A selected target assessed NOT_APPLICABLE beside met ones leaves the run UNKNOWN (exit 2), the code of
        the Engine's ``requirements`` contract for a declared target that does not apply; a target that is not
        selected is not assessed."""
        na = {"id": "n", "status": "NOT_APPLICABLE", "value": None, "assessment": "NOT_APPLICABLE"}
        mixed = [row("a", "PASS"), na]
        self.assertEqual(wc.classify("PASS", [], mixed), "UNKNOWN")
        self.assertEqual(wc.WORKFLOW_EXIT_CODES[wc.classify("PASS", [], mixed)], 2)
        self.assertEqual(wc.requirement_outcome("PASS", {"outcome": "COMPLETED"}, [], mixed), "UNKNOWN")
        self.assertEqual(wc.requirement_outcome("PASS", {"outcome": "PARTIAL"}, [], mixed), "UNKNOWN")
        self.assertEqual(wc.classify("PASS", [], mixed, selected=["a"]), "PASS")
        self.assertEqual(wc.classify("FAIL", [], [row("a", "FAIL"), na]), "FAIL")
        self.assertEqual(wc.classify("UNKNOWN", [], [row("a", "UNKNOWN"), na]), "UNKNOWN")
        self.assertEqual(wc.classify("NOT_APPLICABLE", [], [na, {**na, "id": "m"}]), "NOT_APPLICABLE")

    def test_unmet_outranks_out_of_model(self):
        scoped = {"id": "s", "status": "OUT_OF_MODEL_SCOPE", "value": None, "assessment": "INDETERMINATE",
                  "applicability_issue_codes": ["MODEL_WAVELENGTH_OUTSIDE_BAND"]}
        self.assertEqual(wc.classify("FAIL", [], [row("a", "FAIL"), scoped]), "FAIL")
        self.assertEqual(wc.classify("OUT_OF_MODEL_SCOPE", [], [row("a", "PASS"), scoped]), "OUT_OF_MODEL")

    def test_saved_record_is_classed_again(self):
        record = {"outcome": "UNKNOWN", "status": "WARNING", "error": None,
                  "execution": {"outcome": "PARTIAL", "budgets": {"exhausted": ["max_return_analysis_work"]}},
                  "targets": [row("a", "PASS"), row("b", "FAIL")], "selected_targets": ["a"]}
        self.assertEqual(wc.record_outcome(record), "PASS")
        self.assertEqual(wc.record_outcome({**record, "selected_targets": None}), "FAIL")
        self.assertEqual(wc.record_outcome({**record, "error": {"code": "NOT_RUN"}}), "ERROR")


if __name__ == "__main__":
    unittest.main()
