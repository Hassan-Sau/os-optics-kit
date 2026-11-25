"""Offline tests of the repository checks in scripts/ (no engine, no network)."""

import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

from scripts import changed_cases, check_dco, check_dist, check_repo, engine_release


ROOT = Path(__file__).resolve().parents[1]
RETIRED = "opensci-" + "optic.com"
BASELINE = {"schema": "os-optics-kit.baseline", "schema_version": 1, "kit_version": "0.1.0",
            "engine": {"distribution": "os-optics", "requirement": ">=0.12,<0.13", "verified_version": "0.12.0"},
            "catalog": {"version": "2026-10-28.1", "schema_version": "3.2", "sha256": "a" * 64},
            "python": ["3.11", "3.14"]}
PYPROJECT = """[project]
name = "os-optics-kit"
requires-python = ">=3.11"
classifiers = ["Programming Language :: Python :: 3.11", "Programming Language :: Python :: 3.14"]
dependencies = ["os-optics>=0.12,<0.13"]
"""


class RepositoryTree(unittest.TestCase):
    """A small clean repository in a temporary directory (not a Git checkout)."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.write("baseline.json", json.dumps(BASELINE))
        self.write("pyproject.toml", PYPROJECT)
        self.write("tools/os_optics_kit/_version.py", '__version__ = "0.1.0"\n')
        self.write("README.md", "# Kit\n\n[Guide](docs/en/guide.md) · [中文](README.zh-CN.md) · "
                                "![plot](cases/demo/images/plot.png) · [site](https://opensci-optics.com/)\n\n"
                                'Install `pip install "os-optics-kit==0.1.0"` with "os-optics[protected]==0.12.0".\n')
        self.write("README.zh-CN.md", "# 工具包\n\n[指南](docs/zh-CN/guide.md)\n")
        self.write("docs/en/guide.md", "See [the case](../../cases/demo/CASE.md#run) and [top](#guide).\n")
        self.write("docs/zh-CN/guide.md", "见[案例](../../cases/demo/CASE.md)。\n")
        self.write("cases/demo/CASE.md", "---\nname: demo\n---\n## Run\n")
        self.write("CHANGELOG.md", "# Changelog\n\n## 0.1.0\n\nRequires `os-optics>=0.12,<0.13`.\n\n### Added\n\n- x\n")
        (self.root / "cases/demo/images").mkdir(parents=True)
        (self.root / "cases/demo/images/plot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 100)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def problems(self, check=None):
        result = check_repo.run_checks(self.root)
        return [(item["check"], item["path"], item["line"]) for item in result["problems"]
                if check is None or item["check"] == check]


class RepositoryCheckTests(RepositoryTree):
    def test_clean_tree_passes(self):
        self.assertEqual(check_repo.run_checks(self.root)["problems"], [])

    def test_broken_and_escaping_links_are_reported_but_code_and_urls_are_not(self):
        self.write("docs/en/guide.md", "See [missing](missing.md) and [up](../../../outside.md).\n\n"
                                       "```markdown\n[example](not-a-file.md)\n```\n\n"
                                       "Inline `[x](nope.md)`, [web](https://example.com/x.md), [mail](mailto:a@b).\n"
                                       "[ref]: ./also-missing.md\n<img src=\"gone.png\">\n")
        self.assertEqual(self.problems("links"), [("links", "docs/en/guide.md", 1), ("links", "docs/en/guide.md", 1),
                                                  ("links", "docs/en/guide.md", 8), ("links", "docs/en/guide.md", 9)])

    def test_size_limits_and_forbidden_files(self):
        (self.root / "cases/demo/images/big.png").write_bytes(b"\0" * (check_repo.MAX_IMAGE_BYTES + 1))
        (self.root / "data.bin").write_bytes(b"\0" * (check_repo.MAX_FILE_BYTES + 1))
        (self.root / "cases/demo/images/ok.png").write_bytes(b"\0" * check_repo.MAX_IMAGE_BYTES)
        for name in ("cases/demo/run-1/manifest.json", "cases/demo/scene.glb", "public_optical_catalog.v2.json",
                     "cases/demo/out/x.txt", "dist.whl"):
            self.write(name, "{}")
        self.assertEqual(sorted(self.problems("size")), [("size", "cases/demo/images/big.png", None),
                                                         ("size", "data.bin", None)])
        self.assertEqual(sorted(path for _, path, _ in self.problems("forbidden")),
                         ["cases/demo/out/x.txt", "cases/demo/run-1/manifest.json", "cases/demo/scene.glb",
                          "dist.whl", "public_optical_catalog.v2.json"])

    def test_work_logs_are_not_tracked(self):
        for name in ("ROUND_LOG.md", "docs/round_log.md"):
            self.write(name, "# log\n")
        self.assertEqual(sorted(self.problems("forbidden")), [("forbidden", "ROUND_LOG.md", None),
                                                              ("forbidden", "docs/round_log.md", None)])

    def test_version_and_catalog_statements_follow_the_baseline(self):
        # BASELINE: requirement >=0.12,<0.13, verified 0.12.0, kit 0.1.0, catalog 2026-10-28.1
        self.write("docs/en/guide.md", "\n".join([
            "os-optics-kit 0.1.x works with os-optics 0.12; verified with os-optics 0.12.0 and catalog release",
            "`2026-10-28.1`. Use OS Optics 0.12 and os-optics-kit 0.1.",
            "os-optics-kit 0.1.x works with os-optics 0.11.",  # 3: series outside the requirement
            "These recipes use OS Optics 0.13 with os-optics-kit 0.0.",  # 4: engine series, kit series
            "The numbers below come from os-optics 0.11.0 and catalog release `2026-11-24.1`.",  # 5: both
            "A benchmark on os-optics 0.11.0 keeps the version it ran on; os-optics.local is a module.",
            "Was verified with os-optics",
            "0.11.0 across a line break.",  # 7
        ]) + "\n")
        self.write("docs/zh-CN/guide.md", "os-optics-kit 0.1.x 与 os-optics 0.11 配合使用；已在 os-optics 0.11.0 和"
                                          "目录版本 2026-10-15.1 上验证；下文数值来自 os-optics 0.12.0。\n")
        self.write("CHANGELOG.md", "# Changelog\n\n## 0.1.0\n\nVerified with os-optics 0.11.0 and catalog "
                                   "release 2026-10-15.1 (history is not checked).\n")
        self.write("cases/demo/README.md", "Recorded with os-optics 0.11.0.\n")  # a case states its own
        self.assertEqual(sorted(self.problems("statements")), [
            ("statements", "docs/en/guide.md", 3), ("statements", "docs/en/guide.md", 4),
            ("statements", "docs/en/guide.md", 4), ("statements", "docs/en/guide.md", 5),
            ("statements", "docs/en/guide.md", 5), ("statements", "docs/en/guide.md", 7),
            ("statements", "docs/zh-CN/guide.md", 1), ("statements", "docs/zh-CN/guide.md", 1),
            ("statements", "docs/zh-CN/guide.md", 1)])
        # a candidate engine is required but not verified: the series is the candidate's, verification the last one
        engine = dict(BASELINE["engine"], requirement=">=0.13,<0.14", verified_version="0.12.0",
                      candidate_version="0.13.0")
        self.write("baseline.json", json.dumps(dict(BASELINE, engine=engine)))
        self.write("docs/en/guide.md", "os-optics-kit 0.1.x requires os-optics 0.13; verified with os-optics 0.12.0.\n")
        self.write("docs/zh-CN/guide.md", "os-optics-kit 0.1.x 与 os-optics 0.12 配合使用。\n")
        self.write("CHANGELOG.md", "# Changelog\n\n## 0.1.0\n")
        self.assertEqual(self.problems("statements"), [("statements", "docs/zh-CN/guide.md", 1)])

    def test_cases_record_the_baseline_engine_and_catalog(self):
        case = ("---\nname: demo\nengine: {engine}\ncatalog: {catalog}\n---\n## Baseline\n\n"
                "os-optics {prose}, {where}: run status PASS (`expected.json`).\n")
        expected = {"catalog": "2026-10-28.1", "engine": "0.12.0", "schema": "os-optics-kit.case-expected"}

        def write_case(engine="0.12.0", catalog="2026-10-28.1", prose="0.12.0", where="catalog 2026-10-28.1",
                       readme="Verified with os-optics 0.12.0 and catalog 2026-10-28.1.",
                       readme_zh="已用 os-optics 0.12.0 与目录 2026-10-28.1 验证。", record=expected):
            self.write("cases/demo/CASE.md", case.format(engine=engine, catalog=catalog, prose=prose, where=where))
            self.write("cases/demo/expected.json", json.dumps(record))
            self.write("cases/demo/README.md", readme + "\n")
            self.write("cases/demo/README.zh-CN.md", readme_zh + "\n")

        write_case()
        self.assertEqual(self.problems("cases"), [])
        # prose relabelled without re-recording: the Baseline line, both READMEs and a catalog name disagree
        write_case(prose="0.13.0", readme="Verified with os-optics 0.13.0 and catalog 2026-11-24.1.",
                   readme_zh="已用 os-optics 0.13.0 与目录 2026-10-28.1 验证。")
        self.assertEqual(sorted(self.problems("cases")), [
            ("cases", "cases/demo/CASE.md", 8), ("cases", "cases/demo/README.md", 1),
            ("cases", "cases/demo/README.md", 1), ("cases", "cases/demo/README.zh-CN.md", 1)])
        # records that disagree with baseline.json and with each other
        write_case(engine="0.11.0", catalog="2026-10-15.1", prose="0.11.0", where="catalog 2026-10-15.1",
                   readme="Verified with os-optics 0.11.0.", readme_zh="已用 os-optics 0.11.0 验证。")
        messages = sorted(item["message"] for item in check_repo.run_checks(self.root)["problems"]
                          if item["check"] == "cases")
        self.assertEqual(len(messages), 4, messages)
        for fragment in ("engine.verified_version", "catalog.version", "differs from CASE.md engine",
                         "differs from CASE.md catalog"):
            self.assertTrue(any(fragment in message for message in messages), (fragment, messages))
        # a catalog-free case records none in both places
        write_case(catalog="none", where="no catalog", record=dict(expected, catalog=None),
                   readme="Verified with os-optics 0.12.0.", readme_zh="已用 os-optics 0.12.0 验证。")
        self.assertEqual(self.problems("cases"), [])

    def test_a_case_may_record_the_candidate_engine_when_its_records_agree(self):
        # The kit requires a candidate engine that is not verified yet; a case that needs it records it.
        engine = dict(BASELINE["engine"], requirement=">=0.13,<0.14", verified_version="0.12.0",
                      candidate_version="0.13.0")
        self.write("baseline.json", json.dumps(dict(BASELINE, engine=engine)))
        case = ("---\nname: {name}\nengine: {engine}\ncatalog: none\n---\n## Baseline\n\n"
                "os-optics {prose}, no catalog: run status PASS (`expected.json`).\n")

        def write_case(name, engine, prose=None, record=None, readme=None):
            self.write(f"cases/{name}/CASE.md", case.format(name=name, engine=engine, prose=prose or engine))
            expected = self.root / f"cases/{name}/expected.json"
            if record is None:
                expected.unlink(missing_ok=True)
            else:
                self.write(f"cases/{name}/expected.json", json.dumps(
                    {"catalog": None, "engine": record, "schema": "os-optics-kit.case-expected"}))
            self.write(f"cases/{name}/README.md", (readme or f"Verified with os-optics {prose or engine}.") + "\n")

        def messages():
            return sorted((item["path"], item["message"]) for item in check_repo.run_checks(self.root)["problems"]
                          if item["check"] == "cases")

        # one case on the verified engine, one on the candidate: both consistent
        write_case("demo", "0.12.0", record="0.12.0")
        write_case("zoom", "0.13.0", record="0.13.0")
        self.assertEqual(messages(), [])
        # the candidate case without its record is not accepted
        write_case("zoom", "0.13.0")
        found = messages()
        self.assertEqual([path for path, _ in found], ["cases/zoom/CASE.md"], found)
        self.assertIn("engine.candidate_version", found[0][1])
        # front matter on the candidate, record on the verified engine: the record disagrees
        write_case("zoom", "0.13.0", record="0.12.0")
        found = messages()
        self.assertEqual([path for path, _ in found], ["cases/zoom/expected.json"], found)
        self.assertIn("differs from CASE.md engine 0.13.0", found[0][1])
        # prose that names another engine than the candidate the case records
        write_case("zoom", "0.13.0", prose="0.12.0", record="0.13.0", readme="Verified with os-optics 0.12.0.")
        self.assertEqual([path for path, _ in messages()], ["cases/zoom/CASE.md", "cases/zoom/README.md"])
        # an engine that is neither the verified nor the candidate one
        write_case("zoom", "0.13.1", record="0.13.1")
        found = messages()
        self.assertEqual([path for path, _ in found], ["cases/zoom/CASE.md"], found)
        self.assertIn("engine.verified_version 0.12.0 or engine.candidate_version 0.13.0", found[0][1])
        # once no candidate is set, a case on an unverified engine is reported again
        write_case("zoom", "0.13.0", record="0.13.0")
        self.write("baseline.json", json.dumps(BASELINE))
        found = [item for item in messages() if item[0].startswith("cases/zoom/")]
        self.assertEqual([path for path, _ in found], ["cases/zoom/CASE.md"], found)
        self.assertNotIn("candidate", found[0][1])

    def test_machine_paths_and_secrets_are_reported(self):
        self.write("cases/demo/notes.md", "\n".join([
            "C:" + "\\Users\\someone\\project", "E:" + "\\\\working\\\\x", "/home/" + "alice/run",
            "/d/" + "cache/tmp", "~/.opensci/catalog is fine", "relative/home/alice/ is fine",
            "gh" + "p_" + "A" * 36, "pypi-" + "AgE" + "x" * 60, "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
            "oat_" + "Z" * 40]))
        self.assertEqual([line for _, _, line in self.problems("paths")], [1, 2, 3, 4])
        self.assertEqual([line for _, _, line in self.problems("secrets")], [7, 8, 9, 10])

    def test_invalid_json_is_reported(self):
        self.write("cases/demo/expected.json", "{not json")
        self.assertEqual(self.problems("json"), [("json", "cases/demo/expected.json", None)])

    def test_pins_must_match_the_baseline_outside_tests_and_scripts(self):
        self.write("docs/en/guide.md", 'pip install "os-optics==0.11.0" "os-optics-kit[plot]==0.0.9"\n'
                                       "os-optics>=0.12 is a range, not a pin\n" + "opensci-" + "engine==0.9.0\n"
                                       f"https://{RETIRED}/ and "
                                       f"https://img.shields.io/badge/x-{RETIRED.replace('-', '--')}-1\n")
        self.write("tests/test_x.py", '"os-optics[protected]==0.6.1"\n')
        self.write("scripts/x.py", '"os-optics==0.7.0"\n')
        self.assertEqual(self.problems("pins"), [("pins", "docs/en/guide.md", 1), ("pins", "docs/en/guide.md", 1),
                                                 ("pins", "docs/en/guide.md", 3), ("pins", "docs/en/guide.md", 4),
                                                 ("pins", "docs/en/guide.md", 4)])

    def test_engine_ranges_must_equal_the_baseline_requirement(self):
        self.write("docs/en/guide.md", 'Requires `os-optics>=0.12,<0.13`; "os-optics[protected] >= 0.12, < 0.14";\n'
                                       "the kit range os-optics-kit>=0.2,<0.3 is not an engine range;\n"
                                       'os-optics==0.12.0 is a pin; ["os-optics<0.13,>=0.12"]\n'
                                       "recover with python -m pip install \"os-optics>=0.11,<0.12\"\n")
        self.write("tests/test_x.py", '"os-optics>=0.6,<0.7"\n')
        self.write("scripts/x.py", '"os-optics>=0.7,<0.8"\n')
        self.write("CHANGELOG.md", "# Changelog\n\n## 0.1.0\n\nRequires `os-optics>=0.12,<0.13`.\n\n"
                                   "## 0.0.9\n\nRequires `os-optics>=0.11,<0.12`.\n")
        self.assertEqual(self.problems("ranges"), [("ranges", "docs/en/guide.md", 1),
                                                   ("ranges", "docs/en/guide.md", 4)])
        self.assertEqual(self.problems(), [("ranges", "docs/en/guide.md", 1), ("ranges", "docs/en/guide.md", 4)])

    def test_changelog_has_a_section_for_the_kit_version_newest_first(self):
        for text, fragment in (
            (None, "missing"),
            ("# Changelog\n\n## 0.0.9\n\n- x\n", "no section '## 0.1.0'"),
            ("# Changelog\n\n## 0.0.9\n\n## 0.1.0\n", "follows 0.0.9"),
            ("# Changelog\n\n## 0.1.0\n\n## 0.1.0\n", "appears twice"),
            ("# Changelog\n\n## Next\n\n## 0.1.0\n", "not a release version"),
            ("# Changelog\n\n## 0.1.0\n\n## Unreleased\n", "comes before every release"),
            ("# Changelog\n\n## Unreleased\n\n- x\n", "no section '## 0.1.0'"),
            ("# Changelog\n\n## 0.2.0\n\n## 0.1.0\n", "comes first"),
        ):
            with self.subTest(fragment=fragment):
                if text is None:
                    (self.root / "CHANGELOG.md").unlink()
                else:
                    self.write("CHANGELOG.md", text)
                messages = [item["message"] for item in check_repo.run_checks(self.root)["problems"]
                            if item["check"] == "changelog"]
                self.assertTrue(any(fragment in message for message in messages), messages)
                self.write("CHANGELOG.md", "# Changelog\n\n## 0.1.0\n")
                self.assertEqual(self.problems("changelog"), [])
        self.write("CHANGELOG.md", "# Changelog\n\n## 0.1.0\n\n### Added\n\n## 0.0.10\n\n## 0.0.9\n")
        self.assertEqual(self.problems("changelog"), [])
        # changes merged since the last release are listed above it, under "## Unreleased"
        self.write("CHANGELOG.md", "# Changelog\n\n## Unreleased\n\n### Added\n\n- y\n\n## 0.1.0\n\n## 0.0.9\n")
        self.assertEqual(self.problems("changelog"), [])
        # a version bump without its section fails
        self.write("baseline.json", json.dumps(dict(BASELINE, kit_version="0.1.1")))
        self.write("tools/os_optics_kit/_version.py", '__version__ = "0.1.1"\n')
        self.assertEqual(self.problems("changelog"), [("changelog", "CHANGELOG.md", None)])

    def test_language_pairs(self):
        (self.root / "README.zh-CN.md").unlink()
        self.write("docs/zh-CN/only.md", "仅中文\n")
        self.write("CONTRIBUTING.md", "English only is fine here.\n")
        self.assertEqual(sorted(self.problems("pairs")), [("pairs", "README.md", None),
                                                          ("pairs", "docs/zh-CN/only.md", None)])

    def test_baseline_consistency(self):
        for change, fragment in (
            ({"kit_version": "0.2.0"}, "_version.py"),
            ({"engine": dict(BASELINE["engine"], requirement=">=0.12,<0.14")}, "pyproject.toml os-optics"),
            ({"engine": dict(BASELINE["engine"], verified_version="0.13.0")}, "outside"),
            ({"python": ["3.10"]}, "older than requires-python"),
            ({"python": ["3.12"]}, "classifier"),
            ({"catalog": dict(BASELINE["catalog"], sha256="xyz")}, "catalog.sha256"),
            ({"schema": "other"}, "schema"),
        ):
            with self.subTest(change=change):
                self.write("baseline.json", json.dumps(dict(BASELINE, **change)))
                messages = [item["message"] for item in check_repo.run_checks(self.root)["problems"]
                            if item["check"] == "baseline"]
                self.assertTrue(any(fragment in message for message in messages), messages)

    def test_candidate_engine_keeps_the_last_verified_version(self):
        # The kit requires a candidate (>=0.12,<0.13) not yet verified: verified_version stays the older release.
        engine = dict(BASELINE["engine"], verified_version="0.11.0", candidate_version="0.12.0")
        self.write("baseline.json", json.dumps(dict(BASELINE, engine=engine)))
        self.assertEqual(self.problems("baseline"), [])
        for change, fragment in (({"candidate_version": "0.13.0"}, "candidate_version '0.13.0' is outside"),
                                 ({"verified_version": "0.12.0"}, "must precede")):
            with self.subTest(change=change):
                self.write("baseline.json", json.dumps(dict(BASELINE, engine=dict(engine, **change))))
                messages = [item["message"] for item in check_repo.run_checks(self.root)["problems"]
                            if item["check"] == "baseline"]
                self.assertTrue(any(fragment in message for message in messages), messages)

    def test_engine_module_states_the_baseline_requirement(self):
        engine = 'ENGINE_DISTRIBUTION = "os-optics"\nENGINE_REQUIREMENT = ">=0.12,<0.13"\n'
        self.write("tools/os_optics_kit/_engine.py", engine)
        self.assertEqual(self.problems("baseline"), [])
        self.write("tools/os_optics_kit/_engine.py", engine.replace("<0.13", "<0.14"))
        messages = [item["message"] for item in check_repo.run_checks(self.root)["problems"]
                    if item["check"] == "baseline"]
        self.assertEqual(len(messages), 1, messages)
        self.assertIn("ENGINE_REQUIREMENT", messages[0])

    def test_specifier_fallback_without_packaging(self):
        with patch.dict("sys.modules", {"packaging": None, "packaging.specifiers": None, "packaging.version": None}):
            self.assertTrue(check_repo.satisfies("0.12.0", ">=0.12,<0.13"))
            self.assertTrue(check_repo.satisfies("0.12.3", ">=0.12, <0.13"))
            self.assertFalse(check_repo.satisfies("0.13.0", ">=0.12,<0.13"))
            self.assertFalse(check_repo.satisfies("0.11.9", ">=0.12,<0.13"))

    def test_command_line_prints_json_and_annotations(self):
        self.write("docs/en/guide.md", "[missing](missing.md)\n")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr), \
                patch.dict(check_repo.os.environ, {"GITHUB_ACTIONS": "true"}):
            code = check_repo.main(["--root", str(self.root)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "FAIL")
        self.assertIn("::error file=docs/en/guide.md,line=1,title=check_repo links::", stderr.getvalue())

    def test_git_checkout_ignores_untracked_ignored_files(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.write(".gitignore", "/run*/\n")
        self.write("run-local/huge.json", "{not json")
        self.assertEqual(check_repo.run_checks(self.root)["problems"], [])


class RepositoryStateTests(unittest.TestCase):
    """Checks of this checkout that do not depend on documents other changes are still moving."""

    def test_this_repository_has_no_size_path_secret_json_or_baseline_problems(self):
        problems = [item for item in check_repo.run_checks(ROOT)["problems"]
                    if item["check"] in {"size", "forbidden", "paths", "secrets", "json", "baseline", "pins",
                                         "ranges", "statements", "cases", "changelog"}]
        self.assertEqual(problems, [])

    def test_the_source_archive_carries_the_changelog(self):
        import tomllib
        include = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["hatch"]["build"][
            "targets"]["sdist"]["include"]
        self.assertIn("CHANGELOG.md", include)
        self.assertIn("CHANGELOG.md", check_dist.SDIST_REQUIRED)


class DistributionCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.dist = Path(self.temporary.name)

    def wheel(self, extra=(), metadata_version="0.1.0", requirement="os-optics<0.13,>=0.12"):
        path = self.dist / "os_optics_kit-0.1.0-py3-none-any.whl"
        with zipfile.ZipFile(path, "w") as archive:
            for name in check_dist.WHEEL_REQUIRED:
                archive.writestr(name, "x")
            archive.writestr("os_optics_kit/_data/skills/summary/SKILL.md", "---\n")
            archive.writestr("os_optics_kit-0.1.0.dist-info/METADATA",
                             f"Metadata-Version: 2.4\nName: os-optics-kit\nVersion: {metadata_version}\n"
                             f"Requires-Dist: {requirement}\nRequires-Dist: matplotlib<4,>=3.10; extra == 'plot'\n")
            for name, data in extra:
                archive.writestr(name, data)
        return path

    def sdist(self, extra=()):
        path = self.dist / "os_optics_kit-0.1.0.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, data in [(name, b"x") for name in check_dist.SDIST_REQUIRED] + list(extra):
                info = tarfile.TarInfo("os_optics_kit-0.1.0/" + name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return path

    def test_clean_distributions_pass(self):
        self.wheel()
        self.sdist()
        result = check_dist.run(self.dist, "0.1.0", ">=0.12,<0.13")
        self.assertEqual(result["status"], "PASS", result)

    def test_forbidden_members_versions_and_requirements_fail(self):
        self.wheel(extra=[("os_optics_kit/_data/cases/demo/run-1/manifest.json", "{}"),
                          ("os_optics_kit/_data/cases/demo/scene.glb", "x"),
                          ("os_optics_kit/_data/cases/demo/images/big.png",
                           b"\0" * (check_dist.check_repo.MAX_IMAGE_BYTES + 1)),
                          ("tests/test_x.py", "x")],
                   requirement="os-optics>=0.11")
        self.sdist(extra=[("cases/demo/out/result.json", b"{}")])
        result = check_dist.run(self.dist, "0.1.0", ">=0.12,<0.13")
        problems = " ".join(problem for item in result["distributions"] for problem in item["problems"])
        for fragment in ("run directory", "scene.glb", "big.png", "repository-only", "Requires-Dist",
                         "cases/demo/out/result.json"):
            self.assertIn(fragment, problems)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(check_dist.run(self.dist, "0.2.0", None)["status"], "FAIL")

    def test_missing_distribution_kinds_fail(self):
        self.wheel()
        self.assertEqual(check_dist.run(self.dist, "0.1.0", None)["missing"], ["sdist"])


class DcoTests(unittest.TestCase):
    def commit(self, message, name="Ada Lovelace", email="ada@example.org", parents=("a",)):
        return {"sha": "f" * 40, "parents": list(parents), "author_name": name, "author_email": email,
                "message": message}

    def test_sign_off_must_match_the_author(self):
        signed = "feat: x\n\nSigned-off-by: Ada Lovelace <ADA@example.org>\n"
        self.assertIsNone(check_dco.commit_problem(self.commit(signed)))
        self.assertIn("no Signed-off-by", check_dco.commit_problem(self.commit("feat: x\n")))
        other = "feat: x\n\nSigned-off-by: Someone Else <else@example.org>\n"
        self.assertIn("does not match", check_dco.commit_problem(self.commit(other)))
        self.assertIsNone(check_dco.commit_problem(self.commit("Merge x", parents=("a", "b"))))
        self.assertIsNone(check_dco.commit_problem(self.commit(
            "build(deps): bump", name="dependabot[bot]", email="49699333+dependabot[bot]@users.noreply.github.com")))

    def test_range_in_a_real_repository(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            def git(*args):
                return subprocess.run(["git", "-C", temporary, "-c", "user.name=Ada Lovelace",
                                       "-c", "user.email=ada@example.org", "-c", "commit.gpgsign=false", *args],
                                      capture_output=True, check=True, text=True).stdout.strip()
            git("init", "-q")
            git("commit", "-q", "--allow-empty", "-m", "base")
            base = git("rev-parse", "HEAD")
            git("commit", "-q", "--allow-empty", "-s", "-m", "signed")
            git("commit", "-q", "--allow-empty", "-m", "unsigned")
            head = git("rev-parse", "HEAD")
            commits = check_dco.read_commits(base, head, cwd=temporary)
            self.assertEqual([commit["message"].splitlines()[0] for commit in commits], ["unsigned", "signed"])
            self.assertEqual([bool(check_dco.commit_problem(commit)) for commit in commits], [True, False])


class ChangedCaseTests(unittest.TestCase):
    def test_changed_cases_and_baseline_changes(self):
        existing = {"beam-expander", "ideal-lens-focus"}
        self.assertEqual(changed_cases.select_cases(
            ["cases/beam-expander/CASE.md", "cases/removed/CASE.md", "docs/en/x.md", "cases/README.md"], existing),
            {"all": False, "cases": ["beam-expander"]})
        self.assertEqual(changed_cases.select_cases(["baseline.json"], existing),
                         {"all": True, "cases": ["beam-expander", "ideal-lens-focus"]})
        self.assertEqual(changed_cases.select_cases([], existing), {"all": False, "cases": []})


class EngineReleaseTests(unittest.TestCase):
    def opener(self, documents):
        def open_url(request, timeout):
            self.assertEqual(timeout, 30)
            if request.full_url not in documents:
                raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
            return io.StringIO(json.dumps(documents[request.full_url]))
        return open_url

    def test_verified_release_gate(self):
        url = engine_release.PYPI + "/0.12.0/json"
        self.assertEqual(engine_release.decide("0.12.0", None, self.opener({url: {}}))["run"], True)
        missing = engine_release.decide("0.12.0", None, self.opener({}))
        self.assertEqual((missing["run"], missing["version"]), (False, "0.12.0"))
        self.assertIn("not on PyPI yet", missing["message"])

    def test_latest_runs_only_when_newer(self):
        latest = engine_release.PYPI + "/json"
        for version, run in (("0.11.0", False), ("0.12.0", False), ("0.12.1", True), ("0.13.0rc1", True)):
            with self.subTest(version=version):
                opener = self.opener({latest: {"info": {"version": version}}})
                result = engine_release.decide("0.12.0", "latest", opener)
                self.assertEqual((result["run"], result["version"]), (run, version))
        with self.assertRaises(ValueError):
            engine_release.decide("0.12.0", "latest", self.opener({latest: {"info": {"version": "x y"}}}))

    def test_outputs_and_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            output, summary = Path(temporary) / "out", Path(temporary) / "summary"
            with patch.object(engine_release, "decide", return_value={
                    "run": False, "version": "0.12.0", "verified": "0.12.0", "message": "not yet"}), \
                    patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                self.assertEqual(engine_release.main(["--github-output", str(output), "--summary", str(summary)]), 0)
            self.assertEqual(output.read_text(), "run=false\nversion=0.12.0\n")
            self.assertEqual(summary.read_text(), "not yet\n")

    def test_require_published_fails_the_release_build_for_an_unpublished_engine(self):
        for run, code in ((False, 1), (True, 0)):
            with self.subTest(run=run), patch.object(engine_release, "unverified_candidate", return_value=None), \
                    patch.object(engine_release, "decide", return_value={
                    "run": run, "version": "0.12.0", "verified": "0.12.0", "message": None if run else "not yet"}), \
                    patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()) as stderr:
                self.assertEqual(engine_release.main(["--require-published"]), code)
                self.assertEqual("publish the engine release first" in stderr.getvalue(), not run)
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            engine_release.main(["--require-published", "--candidate", "latest"])

    def test_require_published_refuses_an_unverified_candidate_engine(self):
        with patch.object(engine_release, "unverified_candidate", return_value="0.13.0"), \
                patch.object(engine_release, "decide", return_value={
                    "run": True, "version": "0.12.0", "verified": "0.12.0", "message": None}), \
                patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()) as stderr:
            self.assertEqual(engine_release.main(["--require-published"]), 1)
        self.assertIn("unverified candidate engine 0.13.0", stderr.getvalue())


class WorkflowTests(unittest.TestCase):
    """Text-level guarantees of the workflows; full validation is actionlint's job."""

    def workflows(self):
        return {path.name: path.read_text(encoding="utf-8") for path in (ROOT / ".github" / "workflows").glob("*.yml")}

    def test_expected_workflows_exist(self):
        self.assertTrue({"ci.yml", "scheduled.yml", "release.yml", "codeql.yml"} <= set(self.workflows()))

    def test_actions_are_pinned_by_commit_and_tokens_are_least_privilege(self):
        import re
        for name, text in self.workflows().items():
            with self.subTest(workflow=name):
                self.assertNotIn("pull_request_target", text)
                self.assertRegex(text, r"(?m)^permissions:\n  contents: read$")
                for use in re.findall(r"uses:\s*(\S+)", text):
                    if not use.startswith("./"):
                        self.assertRegex(use, r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
                self.assertEqual(text.count("uses: actions/checkout@"), text.count("persist-credentials: false"))
                self.assertIn("timeout-minutes:", text)

    def job(self, workflow, name):
        """The text of one job of a workflow (from its key to the next job key)."""
        import re
        text = self.workflows()[workflow]
        match = re.search(rf"(?ms)^  {re.escape(name)}:\n.*?(?=^  [\w-]+:\n|\Z)", text)
        self.assertIsNotNone(match, name)
        return match.group(0)

    def step(self, job_text, name):
        import re
        match = re.search(rf"(?ms)^      - name: {re.escape(name)}\n.*?(?=^      - |\Z)", job_text)
        self.assertIsNotNone(match, name)
        return match.group(0)

    def test_ci_verifies_cases_on_one_path_once_per_tree(self):
        engine = self.job("ci.yml", "engine")
        self.assertIn("name: Engine checks (published os-optics)", engine)
        # exactly the engine the gate selected (scripts/engine_gate.py), never the alignment runner's default
        self.assertIn('python scripts/check_alignment.py --engine "$ENGINE" --out run-alignment --skip cases-verify',
                      engine)
        self.assertEqual(engine.count("check_alignment.py"), 1)
        self.assertIn("    permissions:\n      contents: read\n", engine)
        self.assertRegex(engine, r"\n      actions: read\n")
        verify = self.step(engine, "Verify cases")
        self.assertIn("run-alignment/.venv/bin/python -m os_optics_kit cases verify \"${names[@]}\" --jobs 4", verify)
        self.assertIn("--out run-alignment/cases > run-alignment/cases-verify.json", verify)
        self.assertIn("OPENSCI_CATALOG_DIR: ${{ github.workspace }}/run-alignment/.catalog", verify)
        self.assertIn('exit "$status"', verify)
        # The summary's exit code (non-zero for a document that is not MATCH) fails the step when verify exited 0.
        self.assertIn('--exit "$status" || summary=$?', verify)
        self.assertIn("if (( status == 0 )); then status=$summary; fi", verify)
        self.assertNotIn("GITHUB_TOKEN", verify)
        self.assertEqual(engine.count("cases verify"), 1)  # no per-case loop
        lookup = self.step(engine, "Look for a full verification of this tree")
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", lookup)
        self.assertIn("github.event_name != 'pull_request'", lookup)
        self.assertEqual(engine.count("github.token"), 1)
        record = self.step(engine, "Record the full verification of this tree")
        self.assertIn("steps.cases.outputs.all == 'true'", record)
        self.assertIn("steps.verify.outcome == 'success'", record)
        upload = self.step(engine, "Upload the verified-tree record")
        self.assertIn("name: ${{ steps.record.outputs.name }}", upload)
        self.assertIn("scripts/changed_cases.py --event", self.step(engine, "Cases to verify"))
        # The token permission and the alignment check's cases-verify stay where they belong.
        for job in ("setup", "engine-gate", "engine-skipped", "ruff", "mypy", "tests", "repository", "package", "dco"):
            with self.subTest(job=job):
                self.assertNotIn("actions: read", self.job("ci.yml", job))
        # The scheduled runs keep verifying every case through the default check cases-verify.
        self.assertNotIn("cases-verify", self.workflows()["scheduled.yml"])
        self.assertIn("check_alignment.py", self.workflows()["scheduled.yml"])

    def test_required_check_names_and_the_case_table_check(self):
        text = self.workflows()["ci.yml"]
        for name in ("Read baseline", "Lint (ruff)", "Types (mypy)", "Unit tests (Python ${{ matrix.python }}, no engine)",
                     "Repository, skills and cases", "Build and install the package", "DCO sign-off",
                     "Engine candidate gate",
                     "Engine checks (published os-optics)"):
            with self.subTest(name=name):
                self.assertIn(f"    name: {name}\n", text)
        repository = self.job("ci.yml", "repository")
        table = self.step(repository, "README case tables are current")
        self.assertIn("if: ${{ !cancelled() }}", table)
        self.assertIn("run: python scripts/case_tables.py --check", table)
        self.assertLess(repository.index("cases index --check"), repository.index("case_tables.py --check"))

    def test_only_the_scheduled_reporter_writes_issues_and_only_release_gets_an_oidc_token(self):
        workflows = self.workflows()
        self.assertEqual(sum(text.count("issues: write") for text in workflows.values()), 1)
        self.assertIn("issues: write", workflows["scheduled.yml"])
        self.assertIn("github.event_name == 'schedule'", workflows["scheduled.yml"])
        self.assertEqual([name for name, text in workflows.items() if "id-token: write" in text], ["release.yml"])
        self.assertIn("environment:", workflows["release.yml"])

    def test_release_builds_only_merged_commits_with_a_published_engine(self):
        release = self.workflows()["release.yml"]
        build = release[release.index("  build:"):release.index("  publish:")]
        self.assertIn("fetch-depth: 0", build)
        self.assertIn('git merge-base --is-ancestor "$GITHUB_SHA" origin/main', build)
        self.assertIn("python scripts/engine_release.py --require-published", build)
        # both guards run before anything is built
        self.assertLess(build.index("--require-published"), build.index("python -m build"))
        self.assertLess(build.index("merge-base"), build.index("python -m build"))

    def test_a_pull_request_moving_the_verified_baseline_fails_without_the_engine(self):
        gate = self.step(self.job("ci.yml", "engine"), "Engine candidate gate result")
        self.assertIn("BASELINE_MOVED_WITHOUT_ENGINE", gate)
        # the comparison runs before the step reports a skip, and its failure ends the step
        self.assertLess(gate.index('python scripts/engine_gate.py moved "$BASE"; then'),
                        gate.index("ENGINE_CANDIDATE_NOT_PUBLISHED"))
        self.assertIn('engine_gate.py moved "$BASE"; then\n            echo "run=false" >> "$GITHUB_OUTPUT"\n'
                      "            exit 1\n", gate)
        script = str(ROOT / "scripts" / "engine_gate.py")
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def git(*args):
                return subprocess.run(["git", "-C", folder, "-c", "user.name=t", "-c", "user.email=t@example.org",
                                       *args], capture_output=True, text=True, check=True).stdout.strip()

            def commit(document):
                (root / "baseline.json").write_text(json.dumps(document), encoding="utf-8")
                git("add", "baseline.json")
                git("commit", "-q", "-m", "baseline")
                return git("rev-parse", "HEAD")

            def moved(base):
                done = subprocess.run([sys.executable, script, "moved", base], cwd=folder, capture_output=True,
                                      text=True)
                if done.returncode:
                    self.assertIn("::error title=BASELINE_MOVED_WITHOUT_ENGINE::", done.stdout)
                return done.returncode != 0

            git("init", "-q")
            first = commit(BASELINE)
            self.assertFalse(moved(first))
            candidate = commit(dict(BASELINE, engine=dict(BASELINE["engine"], candidate_version="0.13.0")))
            self.assertFalse(moved(first))  # naming a candidate does not move the verified baseline
            commit(dict(BASELINE, engine=dict(BASELINE["engine"], verified_version="0.13.0")))
            self.assertTrue(moved(candidate))
            commit(dict(BASELINE, catalog=dict(BASELINE["catalog"], version="2026-11-24.1")))
            self.assertTrue(moved(first))


if __name__ == "__main__":
    unittest.main()
