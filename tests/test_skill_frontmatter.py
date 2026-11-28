"""Frontmatter limits of every shipped skill.

Codex and Antigravity parse a skill's frontmatter as YAML, and Claude Code cuts a long
description. A value outside the checked subset can drop the metadata without an error,
so the skill stops being offered. The standard library has no YAML parser: these tests
check the subset that cairn's skills use, and a value outside it needs a real parser
before it ships.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from tests.test_skill_routing import read_head

ROOT = Path(__file__).resolve().parents[1]
DESCRIPTION_LIMIT = 1024

_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_BLOCK = ("", ">", "|", ">-", "|-", ">+", "|+")
# A plain scalar may not start with a YAML indicator (``- `` and ``? `` only with a space).
_INDICATORS = tuple("[]{}&*!|>'\"%@`,#")


def plain_line_hazard(value: str) -> bool:
    """True when a one-line frontmatter value falls outside the checked subset: a block
    indicator alone, a whole quoted string, a one-line flow sequence, or a plain scalar
    that starts with no indicator and holds no ``: `` or `` #``."""
    if value in _BLOCK:
        return False
    if value[0] == "[" and value[-1] == "]":
        # A one-line flow sequence, as `argument-hint: [TOPIC]` is: valid YAML (a list).
        inner = value[1:-1]
        return any(c in inner for c in "[]{}") or ": " in inner or " #" in inner
    if value[0] in "'\"":
        return len(value) < 2 or value[-1] != value[0] or value[0] in value[1:-1]
    return (
        value.startswith(_INDICATORS)
        or value.startswith(("- ", "? "))
        or ": " in value
        or " #" in value
        or value.endswith(":")
    )


def frontmatter_hazards(path: Path) -> list[str]:
    """Keys (or ``line N``) of the top-level frontmatter lines outside the checked subset."""
    hazards: list[str] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    key, plain = "", False  # plain: the open key is a plain scalar that may continue
    for number, line in enumerate(lines[1:], start=2):
        if line.strip() == "---":
            break
        if not line.strip():
            continue
        if line[0].isspace():
            # A plain scalar folded onto the next line still may not hold `: ` or ` #`.
            text = line.strip()
            if plain and (": " in text or " #" in text or text.endswith(":")):
                hazards.append(key)
            continue
        key, sep, value = line.partition(":")
        value = value.strip()
        plain = bool(value) and value[0] not in "'\"[" and value not in _BLOCK
        if not sep or not _KEY.fullmatch(key) or (line[len(key) + 1 :][:1] not in ("", " ")):
            hazards.append(f"line {number}")
        elif plain_line_hazard(value):
            hazards.append(key)
    return list(dict.fromkeys(hazards))


class TestSkillFrontmatter(unittest.TestCase):
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
            if len(description) > DESCRIPTION_LIMIT:
                problems.append(f"{where}: description is {len(description)} characters")
            if not raw.startswith(("'", '"')):
                for hazard in (": ", " #"):
                    if hazard in raw:
                        problems.append(f"{where}: plain-scalar description holds {hazard!r}")
            if "<" in raw or ">" in raw:
                problems.append(f"{where}: description holds an angle bracket")
        self.assertEqual(problems, [])

    def test_every_skill_frontmatter_is_in_the_yaml_subset(self) -> None:
        paths = sorted((ROOT / "skills").glob("*/SKILL.md"))
        self.assertGreaterEqual(len(paths), 7)
        problems = {
            str(path.relative_to(ROOT)): hazards
            for path in paths
            if (hazards := frontmatter_hazards(path))
        }
        self.assertEqual(problems, {})

    def test_hazards_name_the_key_outside_the_subset(self) -> None:
        cases = {
            "description: [broken": ["description"],
            "argument-hint: [prompt] [model]": ["argument-hint"],
            "description: Does a thing.\n  Note: folded": ["description"],
            'description: "unclosed': ["description"],
            "description: Runs it: twice": ["description"],
            "description: Does a thing #1": ["description"],
            "not a key line": ["line 3"],
            "description: Does a thing.": [],
            "argument-hint: [TOPIC]": [],
            'description: "Runs it: twice"': [],
        }
        for frontmatter, expected in cases.items():
            with self.subTest(frontmatter=frontmatter), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "SKILL.md"
                path.write_text(
                    f"---\nname: example-skill\n{frontmatter}\n---\n\n# Example\n",
                    encoding="utf-8",
                )
                self.assertEqual(frontmatter_hazards(path), expected)


if __name__ == "__main__":
    unittest.main()
