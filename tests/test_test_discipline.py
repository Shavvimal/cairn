"""The test-discipline docs stay complete: the docs index, the PR line and the rules.

The module reads the repo and writes nothing.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
INDEX = DOCS / "README.md"
ROW = re.compile(r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*\[[^\]]+\]\(([^)]+)\)")
LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")
AUTOMATED = "Automated evidence (test name, date)"
MANUAL = "Remaining manual evidence (what, who, how to record)"


def index_rows() -> list[tuple[str, str]]:
    rows = []
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        match = ROW.match(line)
        if match:
            rows.append((match.group(1), match.group(2)))
    return rows


class DocsIndexTests(unittest.TestCase):
    def test_docs_index_lists_every_doc(self):
        listed = {target for _, target in index_rows()}
        docs = {path.relative_to(DOCS).as_posix() for path in DOCS.rglob("*.md") if path != INDEX}
        missing = sorted(docs - listed)
        self.assertEqual(missing, [], f"add a row to docs/README.md for: {missing}")

    def test_docs_index_links_resolve(self):
        text = INDEX.read_text(encoding="utf-8")
        broken = [
            target
            for target in LINK.findall(text)
            if "://" not in target and not (DOCS / target).exists()
        ]
        self.assertEqual(broken, [])

    def test_docs_index_newest_first(self):
        dates = [date for date, _ in index_rows()]
        self.assertGreater(len(dates), 0)
        self.assertEqual(dates, sorted(dates, reverse=True))


class RulesTests(unittest.TestCase):
    def test_pr_template_asks_for_discrimination_evidence(self):
        text = (ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
        self.assertIn("Discrimination evidence", text)
        self.assertIn("make discriminates", text)

    def test_claude_md_states_both_rules(self):
        text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("Never pipe a test run through", text)
        self.assertIn("discrimination evidence", text)

    def test_contributing_has_the_section(self):
        text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        self.assertIn("## A new test must fail without its fix", text)
        self.assertIn("### Save test output to a file", text)

    def test_makefile_has_discriminates_target(self):
        text = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^discriminates:")
        phony = re.search(r"(?m)^\.PHONY:(.*)$", text)
        assert phony is not None
        self.assertIn("discriminates", phony.group(1).split())

    def test_acceptance_docs_open_with_evidence_table(self):
        docs = sorted(DOCS.glob("*-acceptance.md"))
        self.assertGreater(len(docs), 0)
        for path in docs:
            text = path.read_text(encoding="utf-8")
            with self.subTest(doc=path.name):
                self.assertRegex(
                    text, r"(?m)^## Evidence \(\d{4}-\d{2}-\d{2}, cairn \d+\.\d+\.\d+, commit \w+\)"
                )
                self.assertIn(AUTOMATED, text)
                self.assertIn(MANUAL, text)
                self.assertIn("### Manual evidence log", text)


if __name__ == "__main__":
    unittest.main()
