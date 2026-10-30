"""The repo-local skill-review skill and the description limits it enforces.

``.agents/skills/skill-review`` is a developer skill that Claude Code (through the
``.claude/skills/skill-review`` symlink), Codex and Antigravity find in this repository.
It does not ship in the plugin. These tests keep its references dated and cited, its
rubric citations resolvable, its measuring script honest, and every shipped skill
description inside the 1,024-character limit and safe for a YAML parser. The design is
in ``docs/skills-refresh-spec.md``.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_skill_routing import read_head

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / ".agents" / "skills" / "skill-review"
REFERENCES = SKILL_DIR / "references"
MEASURE = SKILL_DIR / "scripts" / "measure.py"
CLAUDE_LINK = ROOT / ".claude" / "skills" / "skill-review"

_SOURCE = re.compile(r"^Source: https://\S+")
_FETCHED = re.compile(r"^Fetched: (\d{4}-\d{2}-\d{2})$")
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s#]+)(?:#[^)]*)?\)")
# A rubric source cell cites ``file.md "Section"`` pairs and ``cairn: `path` `` paths.
_SECTION = re.compile(r'"([^"]+)"')
_CAIRN_PATH = re.compile(r"cairn: `([^`]+)`")


def headings(path: Path) -> set[str]:
    return {
        line[3:].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("## ")
    }


def rubric_sources() -> list[str]:
    """The last cell of every rubric table row (the Source column)."""
    cells = []
    for line in (REFERENCES / "rubric.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("| ") and not line.startswith(("| ID", "|---")):
            cells.append(line.rstrip(" |").rsplit(" | ", 1)[-1])
    return cells


def citations(cell: str) -> list[tuple[str, str]]:
    """``(file, section)`` pairs in one source cell, for ``a.md "X", "Y"; b.md "Z"``."""
    pairs: list[tuple[str, str]] = []
    for part in cell.split(";"):
        part = part.strip()
        match = re.match(r"([a-z0-9-]+\.md) (.*)", part)
        if match:
            pairs.extend((match.group(1), section) for section in _SECTION.findall(match[2]))
    return pairs


def run_measure(skill_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(MEASURE), str(skill_dir)],
        capture_output=True,
        text=True,
        check=False,
    )


class TestSkillReview(unittest.TestCase):
    def test_reference_files_carry_source_and_date(self) -> None:
        files = sorted(p for p in REFERENCES.glob("*.md") if p.name != "rubric.md")
        self.assertGreaterEqual(len(files), 7)
        today = dt.datetime.now(dt.UTC).date()
        for path in files:
            head = path.read_text(encoding="utf-8").splitlines()[:6]
            self.assertTrue(any(_SOURCE.match(line) for line in head), f"{path.name}: no Source")
            dates = [m.group(1) for line in head if (m := _FETCHED.match(line))]
            self.assertEqual(len(dates), 1, f"{path.name}: no Fetched: YYYY-MM-DD line")
            fetched = dt.date.fromisoformat(dates[0])
            self.assertLessEqual(fetched, today, f"{path.name}: fetch date in the future")

    def test_skill_frontmatter_parses(self) -> None:
        head = read_head(SKILL_DIR / "SKILL.md")
        self.assertEqual(head["name"], "skill-review")
        self.assertTrue(head["description"])
        self.assertLessEqual(len(head["description"]), 1024)

    def test_skill_body_under_limit(self) -> None:
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        body = text.split("\n---\n", 1)[1]
        self.assertLess(len(body.splitlines()), 500)
        targets = _LINK.findall(body)
        self.assertIn("references/rubric.md", targets)
        for target in targets:
            self.assertTrue((SKILL_DIR / target).is_file(), f"SKILL.md links missing {target}")

    def test_rubric_citations_resolve(self) -> None:
        cells = rubric_sources()
        self.assertGreaterEqual(len(cells), 40)
        problems = []
        for cell in cells:
            pairs = citations(cell)
            paths = _CAIRN_PATH.findall(cell)
            if not pairs and not paths:
                problems.append(f"no citation: {cell}")
            for name, section in pairs:
                path = REFERENCES / name
                if not path.is_file():
                    problems.append(f"{name}: no such reference file")
                elif section not in headings(path):
                    problems.append(f'{name}: no "## {section}" heading')
            for relative in paths:
                if not (ROOT / relative).exists():
                    problems.append(f"cairn: {relative} does not exist")
        self.assertEqual(problems, [])

    def test_claude_link_resolves(self) -> None:
        self.assertTrue(CLAUDE_LINK.is_symlink(), ".claude/skills/skill-review is not a symlink")
        self.assertEqual(str(CLAUDE_LINK.readlink()), "../../.agents/skills/skill-review")
        self.assertEqual(CLAUDE_LINK.resolve(), SKILL_DIR.resolve())
        self.assertTrue((CLAUDE_LINK / "SKILL.md").is_file())

    def test_not_shipped(self) -> None:
        self.assertFalse((ROOT / "skills" / "skill-review").exists())
        codex = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(codex["skills"], "./skills/")

    def test_shipped_descriptions_fit(self) -> None:
        problems = []
        for path in sorted((ROOT / "skills").glob("*/SKILL.md")):
            # A YAML-free read of the raw line: the value as a YAML plain scalar would see it.
            raw = next(
                line.split(":", 1)[1].strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.startswith("description:")
            )
            description = read_head(path)["description"]
            where = path.relative_to(ROOT)
            if len(description) > 1024:
                problems.append(f"{where}: description is {len(description)} characters")
            if not raw.startswith(("'", '"')):
                for hazard in (": ", " #"):
                    if hazard in raw:
                        problems.append(f"{where}: plain-scalar description holds {hazard!r}")
            if "<" in raw or ">" in raw:
                problems.append(f"{where}: description holds an angle bracket")
        self.assertEqual(problems, [])

    def test_measure_reports_a_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            skill = Path(temporary) / "example-skill"
            (skill / "docs").mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\n"
                "name: other-name\n"
                'description: Does a thing. Use when the user says "do the thing" or '
                '"run it". Note: invented.\n'
                "---\n\n"
                "# Example\n\n"
                "Read [the guide](docs/guide.md) and [gone](docs/gone.md).\n"
                "Pass $ARGUMENTS through.\n",
                encoding="utf-8",
            )
            guide = ["# Guide", "", "See [more](more.md).", *[f"line {n}" for n in range(120)]]
            (skill / "docs" / "guide.md").write_text("\n".join(guide) + "\n", encoding="utf-8")
            (skill / "docs" / "more.md").write_text("# More\n", encoding="utf-8")
            result = run_measure(skill)
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = result.stdout.splitlines()
            self.assertIn("name: other-name", lines)
            self.assertIn("triggers: 2", lines)
            self.assertIn("  - do the thing", lines)
            self.assertIn("body: 5 lines", lines)
            self.assertIn("  docs/guide.md: 123 lines", lines)
            self.assertIn("  docs/gone.md: missing", lines)
            flags = lines[lines.index("flags:") + 1 :]
            self.assertEqual(
                flags,
                [
                    "  NO_CONTENTS docs/guide.md",
                    "  NESTED_LINK docs/guide.md -> more.md",
                    "  MISSING_LINK docs/gone.md",
                    "  YAML_HAZARD",
                    "  CLAUDE_ONLY $ARGUMENTS",
                    "  NAME_MISMATCH other-name != example-skill",
                ],
            )

    def test_measure_refuses_a_directory_without_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = run_measure(Path(temporary))
            self.assertEqual(result.returncode, 2)
            self.assertIn("no SKILL.md", result.stderr)


if __name__ == "__main__":
    unittest.main()
