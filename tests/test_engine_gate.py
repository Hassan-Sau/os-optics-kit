"""The CI engine gate: which engine the engine job tests, present/missing from an exact dry-run resolution, the
installed engine in the job summary, and the guard for a pull request that moves the verified baseline."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest

from scripts import check_alignment, engine_gate

ROOT = Path(__file__).resolve().parents[1]
WHEEL = "os_optics-0.14.0-py3-none-any.whl"
SHA = "fc1b5dc31ee29488da666f785d81ff08409bbdceddb2a5c98cae8707f07bf069"
OTHER_SHA = "58f78421186dc655e043f31f9fabeae1b1c17f703d6a84ed0d3a37b443960084"


def baseline(verified="0.14.0", candidate=None, requirement=">=0.14,<0.15"):
    engine = {"distribution": "os-optics", "requirement": requirement, "verified_version": verified}
    if candidate is not None:
        engine["candidate_version"] = candidate
    return {"schema": "os-optics-kit.baseline", "schema_version": 1, "kit_version": "0.5.0", "engine": engine,
            "catalog": {"version": "2026-10-28.1", "schema_version": "3.2", "sha256": "a" * 64},
            "python": ["3.11", "3.14"]}


def fake_pip(version=None, *, returncode=0, stderr="", name="os-optics", sha256=SHA, calls=None):
    """A stand-in for subprocess.run that writes a pip --report file, as pip --dry-run does."""

    def run(cmd, **_kwargs):
        if calls is not None:
            calls.append(cmd[-1])
        report = Path(cmd[cmd.index("--report") + 1])
        if version is not None:
            url = f"https://files.pythonhosted.org/packages/aa/bb/os_optics-{version}-py3-none-any.whl"
            report.write_text(json.dumps({"version": "1", "install": [{
                "metadata": {"name": name, "version": version},
                "download_info": {"url": url, "archive_info": {"hashes": {"sha256": sha256}}}}]}),
                encoding="utf-8")
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    return run


class EngineUnderTestTests(unittest.TestCase):
    def test_the_candidate_is_tested_while_one_is_set(self):
        # PR #42: verified 0.13.0, candidate 0.14.0 -> the job tests 0.14.0, not the previous verified engine
        selected = engine_gate.engine_under_test(baseline("0.13.0", "0.14.0"))
        self.assertEqual((selected["version"], selected["role"], selected["spec"]),
                         ("0.14.0", "candidate", "os-optics==0.14.0"))

    def test_the_verified_engine_is_tested_after_the_baseline_moves(self):
        selected = engine_gate.engine_under_test(baseline("0.14.0"))
        self.assertEqual((selected["version"], selected["role"], selected["spec"]),
                         ("0.14.0", "verified", "os-optics==0.14.0"))

    def test_an_exact_pin_never_a_range(self):
        # the range would install the newest 0.14.x, which is not the engine the cases were verified with
        self.assertEqual(engine_gate.engine_under_test(baseline("0.14.0"))["spec"], "os-optics==0.14.0")
        self.assertEqual(engine_gate.engine_under_test(baseline("0.14.0", "0.14.2"))["spec"], "os-optics==0.14.2")

    def test_an_engine_outside_the_requirement_is_an_error(self):
        # the kit refuses it (ENGINE_UNSUPPORTED): installing it can only produce the failure of run 45676235040
        for document in (baseline("0.13.0"), baseline("0.13.0", "0.15.0")):
            with self.subTest(engine=document["engine"]), self.assertRaisesRegex(ValueError, "outside"):
                engine_gate.engine_under_test(document)

    def test_malformed_versions_are_errors(self):
        for document in (baseline("latest"), baseline("0.14.0", "v0.14.0"), baseline("0.14.0", "--index-url=x"),
                         {"engine": "0.14.0"}):
            with self.subTest(document=document), self.assertRaises(ValueError):
                engine_gate.engine_under_test(document)

    def test_requirement_comes_from_the_baseline(self):
        engine = json.loads((ROOT / "baseline.json").read_text(encoding="utf-8"))["engine"]
        version = engine.get("candidate_version") or engine["verified_version"]
        self.assertEqual(engine_gate.requirement(), f"os-optics=={version}")


class ResolveTests(unittest.TestCase):
    def test_resolved_version_and_artifact(self):
        found = engine_gate.resolve("os-optics==0.14.0", fake_pip("0.14.0"))
        self.assertEqual(found, {"version": "0.14.0", "sha256": SHA, "filename": WHEEL,
                                 "url": f"https://files.pythonhosted.org/packages/aa/bb/{WHEEL}"})
        # pip may exit non-zero after writing the report; the report decides
        self.assertEqual(engine_gate.resolve("os-optics==0.14.2", fake_pip("0.14.2", returncode=1))["version"],
                         "0.14.2")
        # an exact pin (==) names the distribution; the name is normalized as pip normalizes it
        self.assertEqual(engine_gate.resolve("os-optics==0.14.0", fake_pip("0.14.0", name="os_optics"))["version"],
                         "0.14.0")

    def test_unresolvable_version_is_missing(self):
        run = fake_pip(None, returncode=1, stderr="ERROR: No matching distribution found for os-optics==0.15.0")
        self.assertIsNone(engine_gate.resolve("os-optics==0.15.0", run))

    def test_other_pip_failures_are_errors_not_skips(self):
        with self.assertRaises(RuntimeError):
            engine_gate.resolve("os-optics==0.14.0", fake_pip(None, returncode=2, stderr="network unreachable"))


class GateMainTests(unittest.TestCase):
    def run_main(self, document, pip):
        with tempfile.TemporaryDirectory() as tmp:
            output, summary = Path(tmp) / "out", Path(tmp) / "summary"
            out, err = io.StringIO(), io.StringIO()
            original_read, original_resolve = engine_gate.read_baseline, engine_gate.resolve
            engine_gate.read_baseline = lambda root=None: document
            engine_gate.resolve = lambda spec: original_resolve(spec, pip)
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = engine_gate.main(["--github-output", str(output), "--summary", str(summary)])
            finally:
                engine_gate.read_baseline, engine_gate.resolve = original_read, original_resolve
            outputs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines()) \
                if output.exists() else {}
            return (code, outputs, summary.read_text(encoding="utf-8") if summary.exists() else "", err.getvalue(),
                    out.getvalue())

    def test_present_writes_the_exact_version_and_artifact(self):
        calls = []
        code, outputs, summary, err, _ = self.run_main(baseline("0.13.0", "0.14.0"), fake_pip("0.14.0", calls=calls))
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["os-optics==0.14.0"])
        self.assertEqual(outputs, {"engine_candidate": "present", "version": "0.14.0", "role": "candidate",
                                   "filename": WHEEL, "sha256": SHA})
        self.assertNotIn(engine_gate.MARKER, err + summary)
        self.assertIn("### Engine under test: os-optics 0.14.0", summary)
        self.assertIn(SHA, summary)
        self.assertIn("engine.verified_version is 0.13.0", summary)

    def test_missing_writes_the_marker_and_exits_zero(self):
        run = fake_pip(None, returncode=1, stderr="ERROR: No matching distribution found for os-optics==0.14.1")
        code, outputs, summary, err, _ = self.run_main(baseline("0.14.1"), run)
        self.assertEqual(code, 0)
        self.assertEqual((outputs["engine_candidate"], outputs["version"], outputs["sha256"]),
                         ("missing", "0.14.1", ""))
        self.assertIn(engine_gate.MARKER, err)
        self.assertIn(engine_gate.MARKER, summary)

    def test_a_resolution_to_another_version_or_an_inconsistent_baseline_fails_the_gate(self):
        for document, pip in ((baseline("0.14.0"), fake_pip("0.14.3")), (baseline("0.13.0"), fake_pip("0.13.0"))):
            with self.subTest(engine=document["engine"]):
                code, outputs, _, _, out = self.run_main(document, pip)
                self.assertEqual(code, 1)
                self.assertEqual(outputs, {})
                self.assertEqual(json.loads(out)["status"], "ERROR")


class InstalledTests(unittest.TestCase):
    def report(self, version, sha256=SHA, verified=True):
        return {"installed_version": version,
                "engine_source": {"mode": "pypi", "publication_verified": verified, "sha256": sha256,
                                  "filename": f"os_optics-{version}-py3-none-any.whl",
                                  "url": f"https://files.pythonhosted.org/packages/x/os_optics-{version}.whl"}}

    def run_installed(self, report, *arguments):
        with tempfile.TemporaryDirectory() as tmp:
            path, summary = Path(tmp) / "report.json", Path(tmp) / "summary"
            if report is not None:
                path.write_text(json.dumps(report), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = engine_gate.main(["--summary", str(summary), "installed", str(path), *arguments])
            return code, summary.read_text(encoding="utf-8"), out.getvalue()

    def test_the_selected_engine_is_shown_with_its_wheel_hash(self):
        code, summary, _ = self.run_installed(self.report("0.14.0"), "--expect", "0.14.0", "--expect-sha256", SHA)
        self.assertEqual(code, 0)
        self.assertIn("### Engine installed: os-optics 0.14.0", summary)
        self.assertIn(f"| SHA-256 | {SHA} |", summary)
        self.assertIn(f"| Wheel | {WHEEL} |", summary)
        self.assertNotIn(engine_gate.MISMATCH, summary)

    def test_the_run_45676235040_environment_fails(self):
        # the gate selected 0.14.0, the alignment runner installed the previous verified engine 0.13.0
        code, summary, out = self.run_installed(self.report("0.13.0", OTHER_SHA), "--expect", "0.14.0",
                                                "--expect-sha256", SHA)
        self.assertEqual(code, 1)
        self.assertIn(engine_gate.MISMATCH, summary)
        self.assertIn("installed os-optics 0.13.0, the gate selected 0.14.0", out)
        self.assertIn(f"::error title={engine_gate.MISMATCH}::", out)

    def test_another_artifact_no_pypi_evidence_or_no_report_fails(self):
        for report, expect_sha in ((self.report("0.14.0", OTHER_SHA), SHA), (self.report("0.14.0", verified=False), ""),
                                   (None, SHA), ({"installed_version": None}, SHA)):
            with self.subTest(report=report):
                code, summary, _ = self.run_installed(report, "--expect", "0.14.0", "--expect-sha256", expect_sha)
                self.assertEqual(code, 1)
                self.assertIn(engine_gate.MISMATCH, summary)


class BaselineMovesTests(unittest.TestCase):
    def test_moves(self):
        self.assertEqual(engine_gate.baseline_moves(baseline("0.13.0"), baseline("0.13.0")), [])
        # naming a candidate does not move the verified baseline
        self.assertEqual(engine_gate.baseline_moves(baseline("0.13.0"), baseline("0.13.0", "0.14.0")), [])
        self.assertEqual(engine_gate.baseline_moves(baseline("0.13.0", "0.14.0"), baseline("0.14.0")),
                         ["engine.verified_version"])
        moved = baseline("0.13.0")
        moved["catalog"]["version"] = "2026-11-24.1"
        self.assertEqual(engine_gate.baseline_moves(baseline("0.13.0"), moved), ["catalog"])


def _job(text, name):
    match = re.search(rf"(?ms)^  {re.escape(name)}:\n.*?(?=^  [\w-]+:\n|\Z)", text)
    assert match, name
    return match.group(0)


class WorkflowGateTests(unittest.TestCase):
    """ci.yml gates every engine job on the gate job, names the skip and installs the gate's engine."""

    text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    def test_engine_jobs_depend_on_the_gate(self):
        text = self.text
        self.assertIn("run: python scripts/engine_gate.py", text)
        engine = _job(text, "engine")
        # the required check keeps its exact name and is never skipped at job level
        self.assertIn("    name: Engine checks (published os-optics)\n", engine)
        self.assertNotRegex(engine.split("\n    steps:")[0], r"\n    if:")
        self.assertIn("::warning title=ENGINE_CANDIDATE_NOT_PUBLISHED::", engine)
        self.assertIn("echo \"run=false\" >> \"$GITHUB_OUTPUT\"", engine)
        self.assertIn('name: "Engine checks skipped: ENGINE_CANDIDATE_NOT_PUBLISHED"', text)
        self.assertNotIn("pull_request_target", text)
        self.assertNotIn("self-hosted", text)
        self.assertNotIn("${{ secrets", text)
        gate = _job(text, "engine-gate")
        for output in ("engine_candidate", "version", "role", "sha256"):
            self.assertIn(f"      {output}: ${{{{ steps.gate.outputs.{output} }}}}\n", gate)

    def test_the_engine_job_installs_exactly_the_gate_version(self):
        """Run 45676235040: the gate said 0.14.0, the alignment runner installed baseline's verified 0.13.0."""
        engine = _job(self.text, "engine")
        commands = [line.strip()[len("run: "):] for line in engine.splitlines()
                    if "scripts/check_alignment.py" in line]
        self.assertEqual(len(commands), 1)
        self.assertIn("ENGINE: ${{ needs.engine-gate.outputs.version }}", engine)
        gate_version = engine_gate.engine_under_test(baseline("0.13.0", "0.14.0"))["version"]
        argv = shlex.split(commands[0].replace("$ENGINE", gate_version))
        engine_arguments = [argv[index + 1] for index, value in enumerate(argv) if value in ("--engine", "--candidate")]
        self.assertEqual(engine_arguments, [gate_version])
        # what check_alignment.py then installs: the requested version, not engine.verified_version
        with tempfile.TemporaryDirectory() as tmp:
            for requested, expected in ((gate_version, "0.14.0"), ("baseline", "0.13.0")):
                runner = check_alignment.AlignmentCheck(ROOT, Path(tmp) / requested, requested)
                runner.report["baseline_version"] = "0.13.0"
                self.assertTrue(runner._artifacts(requested))
                self.assertEqual(runner.target, expected)
                self.assertEqual(runner._engine_requirement(), f"os-optics[protected]=={expected}")

    def test_the_installed_engine_is_checked_and_shown_before_cases_are_verified(self):
        engine = _job(self.text, "engine")
        step = engine[engine.index("      - name: Installed engine (version and wheel SHA-256)\n"):]
        step = re.sub(r"\\\n\s+", "", step[:step.index("\n      - name:", 1)])  # join continued lines
        self.assertIn("id: installed", step)
        self.assertIn("!cancelled()", step)
        self.assertIn("ENGINE_SHA256: ${{ needs.engine-gate.outputs.sha256 }}", step)
        self.assertIn('engine_gate.py installed run-alignment/report.json --expect "$ENGINE" --expect-sha256 '
                      '"$ENGINE_SHA256"', step)
        self.assertLess(engine.index("scripts/check_alignment.py"), engine.index("id: installed"))
        verify = engine[engine.index("      - name: Verify cases\n"):]
        self.assertIn("steps.installed.outcome == 'success'", verify[:verify.index("run: |")])


if __name__ == "__main__":
    unittest.main()
