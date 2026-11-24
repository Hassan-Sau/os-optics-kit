"""An engine older than the kit's requirement is refused with a message, never a traceback.

A stub ``os_optics`` module stands in for an older release: it lacks the 0.14 API the command calls, so the command
raises ``AttributeError``. The CLI must turn that into ENGINE_UNSUPPORTED naming the required range, the installed
version and the upgrade command.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import types
import unittest
from unittest.mock import patch

from os_optics_kit import _engine, cli

STUB_COMMAND = "_version_guard_stub"


def _stub_command_module():
    module = types.ModuleType(f"os_optics_kit.{STUB_COMMAND}")

    def main(argv):
        import os_optics.local as local

        local.EvaluationSession  # absent before 0.14: AttributeError
        return 0

    module.main = main
    return module


def _stub_engine(version):
    engine = types.ModuleType("os_optics")
    engine.__version__ = version
    local = types.ModuleType("os_optics.local")
    engine.local = local
    return {"os_optics": engine, "os_optics.local": local}


class VersionGuardTests(unittest.TestCase):
    def run_cli(self, installed):
        modules = dict(_stub_engine(installed), **{f"os_optics_kit.{STUB_COMMAND}": _stub_command_module()})
        commands = dict(cli.COMMANDS, guard=(STUB_COMMAND, "stub"))
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(sys.modules, modules), patch.object(cli, "COMMANDS", commands), \
                patch.object(_engine, "engine_version", return_value=installed), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["guard"])
        return code, out.getvalue(), err.getvalue()

    def test_older_engine_is_refused_with_range_installed_version_and_upgrade_command(self):
        code, _out, err = self.run_cli("0.13.0")
        self.assertEqual(code, 4)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("AttributeError", err)
        error = json.loads(err)["error"]
        self.assertEqual(error["code"], "ENGINE_UNSUPPORTED")
        self.assertEqual(error["requirement"], "os-optics>=0.14,<0.15")
        self.assertEqual(error["installed"], "0.13.0")
        self.assertEqual(error["upgrade"], 'python -m pip install --upgrade "os-optics>=0.14,<0.15"')
        for part in ("os-optics>=0.14,<0.15", "0.13.0", "pip install --upgrade"):
            self.assertIn(part, error["message"])

    def test_supported_engine_errors_are_not_masked(self):
        with self.assertRaises(AttributeError):
            self.run_cli("0.14.0")


if __name__ == "__main__":
    unittest.main()
