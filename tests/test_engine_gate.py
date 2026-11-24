"""The CI engine gate: present/missing from a dry-run resolution, visible marker when missing."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts import engine_gate

ROOT = Path(__file__).resolve().parents[1]


def fake_pip(version=None, *, returncode=0, stderr=""):
    """A stand-in for subprocess.run that writes a pip --report file, as pip --dry-run does."""

    def run(cmd, **_kwargs):
        report = Path(cmd[cmd.index("--report") + 1])
        if version is not None:
            report.write_text(json.dumps({"install": [{"metadata": {"name": "os-optics", "version": version}}]}),
                              encoding="utf-8")
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    return run


class EngineGateTests(unittest.TestCase):
    def test_requirement_comes_from_the_baseline(self):
        engine = json.loads((ROOT / "baseline.json").read_text(encoding="utf-8"))["engine"]
        self.assertEqual(engine_gate.requirement(), "os-optics" + engine["requirement"])

    def test_resolved_version_is_present(self):
        self.assertEqual(engine_gate.resolve("os-optics>=0.14,<0.15", fake_pip("0.14.0")), "0.14.0")
        # pip may exit non-zero after writing the report; the report decides
        self.assertEqual(engine_gate.resolve("os-optics>=0.14,<0.15", fake_pip("0.14.2", returncode=1)), "0.14.2")

    def test_unresolvable_range_is_missing(self):
        run = fake_pip(None, returncode=1, stderr="ERROR: No matching distribution found for os-optics<0.15,>=0.14")
        self.assertIsNone(engine_gate.resolve("os-optics>=0.14,<0.15", run))

    def test_other_pip_failures_are_errors_not_skips(self):
        with self.assertRaises(RuntimeError):
            engine_gate.resolve("os-optics>=0.14,<0.15", fake_pip(None, returncode=2, stderr="network unreachable"))

    def run_main(self, version):
        with tempfile.TemporaryDirectory() as tmp:
            output, summary = Path(tmp) / "out", Path(tmp) / "summary"
            out, err = io.StringIO(), io.StringIO()
            original = engine_gate.resolve
            engine_gate.resolve = lambda spec: version
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = engine_gate.main(["--github-output", str(output), "--summary", str(summary)])
            finally:
                engine_gate.resolve = original
            return (code, output.read_text(encoding="utf-8"),
                    summary.read_text(encoding="utf-8") if summary.exists() else "", err.getvalue())

    def test_missing_writes_the_marker_and_exits_zero(self):
        code, output, summary, err = self.run_main(None)
        self.assertEqual(code, 0)
        self.assertIn("engine_candidate=missing", output)
        self.assertIn(engine_gate.MARKER, err)
        self.assertIn(engine_gate.MARKER, summary)

    def test_present_writes_the_version_and_no_marker(self):
        code, output, summary, err = self.run_main("0.14.0")
        self.assertEqual(code, 0)
        self.assertIn("engine_candidate=present\nversion=0.14.0", output)
        self.assertNotIn(engine_gate.MARKER, err + summary)


class WorkflowGateTests(unittest.TestCase):
    """ci.yml gates every engine job on the gate job and names the skip."""

    def test_engine_jobs_depend_on_the_gate(self):
        text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("run: python scripts/engine_gate.py", text)
        engine = text[text.index("\n  engine:\n"):]
        # the required check keeps its exact name and is never skipped at job level
        self.assertIn("    name: Engine checks (published os-optics)\n", engine)
        self.assertNotRegex(engine.split("\n    steps:")[0], r"\n    if:")
        self.assertIn("::warning title=ENGINE_CANDIDATE_NOT_PUBLISHED::", engine)
        self.assertIn("echo \"run=false\" >> \"$GITHUB_OUTPUT\"", engine)
        self.assertIn('name: "Engine checks skipped: ENGINE_CANDIDATE_NOT_PUBLISHED"', text)
        self.assertNotIn("pull_request_target", text)
        self.assertNotIn("self-hosted", text)
        self.assertNotIn("${{ secrets", text)


if __name__ == "__main__":
    unittest.main()
