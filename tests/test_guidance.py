"""Agent guidance: case images and system views route to the browser-free engine snapshot; the kit's `render` and
`present` are the presentation tools that need Chrome. The rule is stated in every entry point an agent reads first,
the case README templates come in both languages with one placeholder set, and AGENT_GUIDE.md keeps its budget."""

import math
from pathlib import Path
import re
import unittest

from os_optics_kit import _frontmatter as fm
from os_optics_kit.contrib import _common as C
from tests.workflow_fixtures import ROOT

ENGINE_SNAPSHOT = "os-optics view RUN --no-browser --snapshot"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def section(text: str, heading: str) -> str:
    """The body of the ``## heading`` section of a Markdown page ('' when absent)."""
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return match.group(1) if match else ""


def placeholders(template: str) -> set[str]:
    """The ``{{key}}`` names a template fills, its HTML comments excluded (``contrib`` drops them)."""
    return {m.group(1) for m in C.PLACEHOLDER_RE.finditer(C.COMMENT_RE.sub("", template))}


class AgentGuideTests(unittest.TestCase):
    def setUp(self):
        self.text = read("AGENT_GUIDE.md")

    def test_route_row_names_the_browser_free_engine_snapshot(self):
        rows = [line for line in section(self.text, "Route by task").splitlines() if line.startswith("| ")]
        view_rows = [row for row in rows if "`os-optics view" in row]
        self.assertTrue(view_rows, "no route row names os-optics view")
        row = view_rows[0]
        for needle in ("--no-browser", "--snapshot", "--views iso", "--section ALL", "cases images NAME"):
            self.assertIn(needle, row)
        self.assertIn("case images", row.lower())
        self.assertIn("no browser", row.lower())

    def test_render_row_is_the_presentation_route(self):
        rows = [line for line in section(self.text, "Route by task").splitlines() if "`render RUN" in line]
        self.assertEqual(len(rows), 1)
        self.assertIn("presentation", rows[0].lower())
        self.assertIn("chrome", rows[0].lower())

    def test_cases_are_found_by_classification(self):
        row = next(line for line in self.text.splitlines() if line.startswith("| Start from a worked design"))
        self.assertIn("--class", row)
        self.assertIn("cases vocabulary", row)

    def test_contribute_a_case_covers_the_image_path(self):
        body = section(self.text, "Contribute a case")
        for needle in ("contrib case", "--class", "--domain", "cases images NAME", "README.zh-CN.md",
                       "cases index --write", "--check", "cases vocabulary", "--open-pr"):
            self.assertIn(needle, body)

    def test_within_budget(self):
        budget = fm.BUDGETS["AGENT_GUIDE.md"]
        self.assertLessEqual(math.ceil(len(self.text) / 4), budget)


class SkillSplitTests(unittest.TestCase):
    def test_render_and_contrib_case_state_the_split(self):
        for name in ("render", "contrib-case"):
            with self.subTest(skill=name):
                text = read(f"skills/{name}/SKILL.md")
                self.assertIn(ENGINE_SNAPSHOT, text)
                self.assertRegex(text, r"[Cc]ase images (and plain system views )?(need no browser|come from the engine)")
                self.assertIn("no `render` extra", text)
                self.assertIn("Chrome or Chromium", text)

    def test_present_and_plot_route_system_views_to_the_engine(self):
        for name in ("present", "plot"):
            with self.subTest(skill=name):
                self.assertIn("os-optics view RUN --no-browser --snapshot", read(f"skills/{name}/SKILL.md"))

    def test_run_case_uses_the_catalog_filter(self):
        text = read("skills/run-case/SKILL.md")
        self.assertNotIn("catalog-free", text)
        for needle in ("--catalog RELEASE|none", "--class", "cases vocabulary"):
            self.assertIn(needle, text)


class PublicPagesTests(unittest.TestCase):
    def test_install_notes_name_the_engine_snapshot_in_both_languages(self):
        for rel in ("README.md", "README.zh-CN.md", "docs/en/presentation.md", "docs/zh-CN/presentation.md"):
            with self.subTest(page=rel):
                self.assertIn("os-optics view RUN --no-browser --snapshot DIR --views iso", read(rel))

    def test_contributor_pages_name_the_case_image_command(self):
        for rel in ("CONTRIBUTING.md", "docs/contributing-with-an-agent.md", "docs/workflow-recipes.md"):
            with self.subTest(page=rel):
                text = read(rel)
                self.assertIn("cases images", text)
                self.assertIn("--no-browser --snapshot", text)


FENCED = re.compile(r"^```.*?^```", re.M | re.S)
MD_LINK = re.compile(r"\]\(([^)\s]+)\)")
ADAPTER_MODULE = "os_optics_kit.study.evaluation_adapter"
ADAPTER_GUIDE = "docs/en/evaluation-adapter.md"
STUDY_PAGES = {"en": "docs/en/studies.md", "zh-CN": "docs/zh-CN/studies.md"}
WORKFLOW_PAGES = {"en": "docs/en/workflows.md", "zh-CN": "docs/zh-CN/workflows.md"}
COMPENSATION_HEADINGS = {"en": "Tolerance studies with compensation", "zh-CN": "带补偿的公差研究"}
CANDIDATE_HEADINGS = {"en": "Candidate designs and external search code", "zh-CN": "候选设计与外部搜索代码"}


def heading_anchors(text: str) -> set[str]:
    """GitHub's anchors of the Markdown headings of ``text``: lower case, punctuation dropped, spaces to hyphens."""
    return {re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")
            for heading in re.findall(r"^#{1,6} (.+)$", FENCED.sub("", text), re.M)}


def page_links(text: str) -> list[str]:
    """The Markdown link targets of ``text`` outside fenced code."""
    return MD_LINK.findall(FENCED.sub("", text))


class StudyDiscoveryTests(unittest.TestCase):
    """The evaluation adapter, tolerance compensation and zoom studies are reachable from the study skill and the
    study and workflow guides, in both languages."""

    def test_study_skill_names_the_adapter_where_a_sequential_optimiser_looks(self):
        text = read("skills/study/SKILL.md")
        not_to_use = section(text, "When not to use")
        self.assertIn("sequential optimiser", not_to_use)
        self.assertIn(ADAPTER_MODULE, not_to_use)
        self.assertIn(ADAPTER_GUIDE, not_to_use)
        python = next(line for line in section(text, "Inputs").split("\n- ") if line.startswith("Python:"))
        self.assertIn("evaluation_adapter", python)
        self.assertIn("`zoom --study`", section(text, "Inputs"))
        self.assertIn(ADAPTER_MODULE, read("skills/study/recipe.md"))

    def test_named_guide_and_module_exist(self):
        self.assertTrue((ROOT / ADAPTER_GUIDE).is_file())
        self.assertTrue((ROOT / "tools" / Path(*ADAPTER_MODULE.split("."))).with_suffix(".py").is_file())

    def test_studies_guides_link_the_adapter_from_the_candidates_section(self):
        for language, rel in STUDY_PAGES.items():
            with self.subTest(language=language):
                body = section(read(rel), CANDIDATE_HEADINGS[language])
                self.assertIn("evaluation-adapter.md", page_links(body))
                self.assertIn(ADAPTER_MODULE, body)

    def test_studies_guides_describe_compensation_and_its_evaluator(self):
        from os_optics_kit.study import compensation

        for language, rel in STUDY_PAGES.items():
            with self.subTest(language=language):
                body = section(read(rel), COMPENSATION_HEADINGS[language])
                self.assertTrue(body, f"no compensation section in {rel}")
                for needle in ("skills show tolerance compensation.md", "tolerance_expander_refocus.json",
                               f"--evaluator {compensation.EVALUATOR}", "--plan-only", "--no-compensation",
                               "NOT_EVALUATED", "COMPENSATION_NOT_RUN", "compensated:*"):
                    self.assertIn(needle, body)
        # the page it points to is the tolerance skill's own reference page
        self.assertTrue((ROOT / "skills" / "tolerance" / "compensation.md").is_file())
        self.assertTrue((ROOT / "cases" / "expansion-focus" / "studies" / "tolerance_expander_refocus.json").is_file())

    def test_study_and_workflow_guides_link_zoom(self):
        for pages in (STUDY_PAGES, WORKFLOW_PAGES):
            for language, rel in pages.items():
                with self.subTest(page=rel):
                    self.assertIn("zoom.md", page_links(read(rel)))

    def test_workflow_guides_point_to_the_compensation_section(self):
        for language, rel in WORKFLOW_PAGES.items():
            with self.subTest(language=language):
                anchor = "studies.md#" + next(iter(heading_anchors("## " + COMPENSATION_HEADINGS[language])))
                self.assertIn(anchor, page_links(read(rel)))

    def test_english_and_chinese_pages_link_the_same_pages(self):
        for pages in (STUDY_PAGES, WORKFLOW_PAGES):
            files = {language: sorted(link.split("#", 1)[0] for link in page_links(read(rel))
                                      if "://" not in link and not link.startswith("#"))
                     for language, rel in pages.items()}
            # the language switch line links the other language's page
            files["en"].remove("../zh-CN/" + Path(pages["en"]).name)
            files["zh-CN"].remove("../en/" + Path(pages["zh-CN"]).name)
            files["en"].remove("../../README.md")
            files["zh-CN"].remove("../../README.zh-CN.md")
            with self.subTest(page=pages["en"]):
                self.assertEqual(files["en"], files["zh-CN"])

    def test_document_anchors_resolve(self):
        pages = sorted((ROOT / "docs").rglob("*.md")) + [ROOT / name for name in
                                                         ("README.md", "README.zh-CN.md", "AGENT_GUIDE.md")]
        for page in pages:
            for link in page_links(page.read_text(encoding="utf-8")):
                if "://" in link or "#" not in link:
                    continue
                path, fragment = link.split("#", 1)
                target = (page.parent / path) if path else page
                if target.suffix != ".md" or not target.is_file():
                    continue
                with self.subTest(page=page.relative_to(ROOT).as_posix(), link=link):
                    self.assertIn(fragment, heading_anchors(target.read_text(encoding="utf-8")))

    def test_heading_anchor_rule(self):
        self.assertEqual(heading_anchors("## 2. Sweep the lens spacing\n## 带补偿的公差研究\n```\n# not a heading\n```"),
                         {"2-sweep-the-lens-spacing", "带补偿的公差研究"})


class CaseReadmeTemplateTests(unittest.TestCase):
    def test_both_templates_exist_with_the_same_placeholders(self):
        english = read("templates/README.case.md")
        chinese = read("templates/README.case.zh-CN.md")
        expected = {"title", "language_links", "summary", "images", "name", "results_rows", "engine", "catalog_text",
                    "status"}
        self.assertEqual(placeholders(english), expected)
        self.assertEqual(placeholders(chinese), expected)

    def test_chinese_template_fills_completely(self):
        values = {key: f"<{key}>" for key in placeholders(read("templates/README.case.md"))}
        text = C.fill_template(read("templates/README.case.zh-CN.md"), values)
        self.assertNotIn("{{", text)
        self.assertNotIn("<!--", text)
        self.assertNotIn("\n\n\n", text)
        # the language line under the title: the two READMEs link each other
        self.assertTrue(text.startswith("# <title>\n\n<language_links>\n\n<summary>\n\n<images>\n"))

    def test_template_comments_name_the_engine_snapshot(self):
        for rel in ("templates/README.case.md", "templates/README.case.zh-CN.md"):
            with self.subTest(template=rel):
                comment = "".join(re.findall(r"<!--(.*?)-->", read(rel), re.S))
                self.assertIn("--no-browser --snapshot", comment)
                self.assertIn("cases images NAME", comment)


if __name__ == "__main__":
    unittest.main()
