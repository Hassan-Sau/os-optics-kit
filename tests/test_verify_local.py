"""The local verification harness: one run per baseline Python version, one summary."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts import verify_local

ROOT = Path(__file__).resolve().parents[1]


class VerifyLocalTests(unittest.TestCase):
    def test_python_versions_come_from_the_baseline(self):
        baseline = json.loads((ROOT / "baseline.json").read_text(encoding="utf-8"))
        self.assertEqual(verify_local.python_versions(), baseline["python"])
        self.assertEqual(verify_local.python_versions(), ["3.11", "3.14"])

    def test_given_interpreter_wins_and_missing_uv_is_none(self):
        self.assertEqual(verify_local.find_interpreter("3.11", {"3.11": "/x/python"}), "/x/python")

        def failing(cmd, **_kwargs):
            return subprocess.CompletedProcess(cmd, 1, "", "not found")
        found = verify_local.find_interpreter("3.99", {}, run=failing)
        self.assertIsNone(found)

    def test_case_outcomes_count_cases_and_name_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "cases-verify.stdout.txt"
            out.write_text(json.dumps({"status": "MISMATCH", "cases": [
                {"name": "beam-expander", "status": "MATCH"},
                {"name": "aspheric-lens", "status": "MISMATCH"},
                {"name": "ideal-lens-focus", "status": "MATCH"}]}), encoding="utf-8")
            report = {"checks": [{"name": "cases-verify", "status": "FAIL", "stdout": str(out)}]}
            self.assertEqual(verify_local.case_outcomes(report),
                             {"cases": 3, "failed": ["aspheric-lens"], "status": "MISMATCH"})
        self.assertEqual(verify_local.case_outcomes({"checks": []})["cases"], None)

    def test_summary_names_engine_catalog_cases_and_failed_checks(self):
        report = {"python_version": "3.14.8", "status": "FAIL", "installed_version": "0.14.0",
                  "catalog_source": {"catalog_version": "2026-12-01.1", "schema_version": "3.6"},
                  "checks": [{"name": "pytest", "status": "PASS"}, {"name": "render", "status": "FAIL"}]}
        identity = {"version": "0.14.0", "build_id": "0.14.0+local", "schema_version": "1.12.0"}
        row = verify_local.summarize("3.14", Path("out"), report, identity)
        self.assertEqual((row["python"], row["engine_version"], row["engine_build"]), ("3.14.8", "0.14.0", "0.14.0+local"))
        self.assertEqual((row["catalog_release"], row["catalog_schema"]), ("2026-12-01.1", "3.6"))
        self.assertEqual(row["schema"], {"schema_version": "1.12.0"})
        self.assertEqual(row["failed_checks"], ["render"])


if __name__ == "__main__":
    unittest.main()
