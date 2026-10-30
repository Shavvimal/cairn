"""Routing eval for the skill descriptions: each fixture intent names one skill.

This test checks the trigger text in each ``SKILL.md`` description, not the harness
router. Claude Code and Codex pick a skill with a model that reads the whole description
and the conversation, so a row that passes here can still route wrongly in a harness; the
test only guards the quoted trigger phrases against a regression.

Each ``skills/<name>/routing-eval.jsonl`` row holds a paraphrased user ``intent``, the
``expected`` skill (or ``null`` for "no skill") and an optional ``ambiguous_with`` list.
A trigger matches an intent when every content token of the trigger is in the intent's
token set (order-free, crude stemming). An intent may not copy a trigger phrase. Every
pair of skills that both match one row must be listed in ``DECLARED_OVERLAPS``. The
design is in ``docs/skill-routing-eval-spec.md``.
"""

from __future__ import annotations

import json
import re
import unittest
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = _REPO_ROOT / "skills"
FIXTURE_NAME = "routing-eval.jsonl"

MARKER = "Use when the user says"
_QUOTED = re.compile(r'"([^"]+)"')
_NON_WORD = re.compile(r"[\W_]+")
MIN_ROWS = 8
MAX_ROWS = 15
ROW_KEYS = frozenset({"intent", "expected", "ambiguous_with", "note"})

# Words dropped from a trigger before matching (58 words). An intent keeps every word.
STOPWORDS = frozenset(
    """a an the to from of for on in at by with about and or
    my me i we us our you your it its this that these those what which who
    is are was were be been am did do does can could would will should please
    some any all just so up out again there here""".split()  # noqa: SIM905
)

# Skill pairs that may both match one row. A new pair needs a reviewed edit here as well
# as an ``ambiguous_with`` entry on the row.
DECLARED_OVERLAPS: frozenset[tuple[str, str]] = frozenset(
    {
        ("cairn", "invoke-codex"),
        ("invoke-claude", "invoke-codex"),
        ("invoke-antigravity", "invoke-codex"),
        ("recall", "search"),
    }
)


def normalise(text: str) -> str:
    """Lowercase, collapse every run of non-word characters to one space, strip."""
    return " ".join(_NON_WORD.sub(" ", text.lower()).split())


def stem(word: str) -> str:
    """Crude suffix strip: plural ``s``, then ``ing`` or ``ed``."""
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 4 and word.endswith("ed"):
        return word[:-2]
    return word


def intent_tokens(intent: str) -> frozenset[str]:
    return frozenset(stem(w) for w in normalise(intent).split())


def trigger_tokens(trigger: str) -> frozenset[str]:
    """Content tokens of a trigger. A multi-word trigger left with fewer than two content
    tokens keeps all its words (the weak-trigger rule), so "what was I doing" does not
    reduce to ``{doing}``."""
    words = normalise(trigger).split()
    kept = frozenset(stem(w) for w in words if w not in STOPWORDS)
    if len(words) > 1 and len(kept) < 2:
        return frozenset(stem(w) for w in words)
    return kept


def trigger_matches(trigger: str, intent: str) -> bool:
    return trigger_tokens(trigger) <= intent_tokens(intent)


def copied(intent: str, trigger: str) -> bool:
    """True if the intent equals the trigger, or contains a multi-word trigger as a
    contiguous word sequence. A single-word trigger is exempt from the second half."""
    n_intent, n_trigger = normalise(intent), normalise(trigger)
    if n_intent == n_trigger:
        return True
    return " " in n_trigger and f" {n_trigger} " in f" {n_intent} "


class FrontmatterError(ValueError):
    pass


def read_head(path: Path) -> dict[str, str]:
    """Unindented ``key: value`` lines of the frontmatter. ``parse_frontmatter`` is not
    used because it rejects the ``argument-hint: [...]`` values some skills carry."""
    lines = path.read_text(encoding="utf-8").split("\n")
    if not lines or lines[0].strip() != "---":
        raise FrontmatterError("frontmatter not opened")
    head: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return head
        if line and not line[0].isspace() and ":" in line:
            key, value = line.split(":", 1)
            head[key.strip()] = value.strip()
    raise FrontmatterError("frontmatter not closed")


def triggers(description: str) -> list[str]:
    """Every double-quoted span after the marker, normalised. Text before it is ignored."""
    index = description.find(MARKER)
    if index < 0:
        return []
    return [normalise(q) for q in _QUOTED.findall(description[index + len(MARKER) :])]


@dataclass(frozen=True)
class Skill:
    name: str
    triggers: tuple[str, ...]
    routable: bool


@dataclass
class Row:
    path: Path
    line: int
    intent: str = ""
    expected: str | None = None
    ambiguous_with: list[str] = field(default_factory=list)

    @property
    def where(self) -> str:
        return f"{self.path.relative_to(_REPO_ROOT)}:{self.line}"


def load_skills(root: Path = SKILLS_ROOT) -> tuple[dict[str, Skill], list[str]]:
    skills: dict[str, Skill] = {}
    problems: list[str] = []
    for path in sorted(root.glob("*/SKILL.md")):
        directory = path.parent.name
        try:
            head = read_head(path)
        except FrontmatterError as exc:
            problems.append(f"skill {directory}: {exc}")
            continue
        if head.get("name") != directory:
            problems.append(f"skill {directory}: name is {head.get('name')!r}")
        skills[directory] = Skill(
            name=directory,
            triggers=tuple(triggers(head.get("description", ""))),
            routable=head.get("disable-model-invocation") != "true",
        )
    return skills, problems


def matched(intent: str, skills: dict[str, Skill]) -> dict[str, list[str]]:
    """Routable skills with at least one matching trigger, and those triggers."""
    out: dict[str, list[str]] = {}
    for name, skill in skills.items():
        if not skill.routable:
            continue
        hits = [t for t in skill.triggers if trigger_matches(t, intent)]
        if hits:
            out[name] = hits
    return out


def load_rows(skills: dict[str, Skill], root: Path = SKILLS_ROOT) -> tuple[list[Row], list[Row]]:
    """Well-formed rows, and malformed rows whose ``intent`` holds the ``bad row`` reason."""
    rows: list[Row] = []
    bad: list[Row] = []
    for path in sorted(root.glob(f"*/{FIXTURE_NAME}")):
        owner = path.parent.name
        for number, raw in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
            if not raw.strip():
                continue
            row = Row(path=path, line=number)
            reason = _parse_row(raw, owner, skills, row)
            if reason:
                row.intent = f"bad row: {reason}"
                bad.append(row)
            else:
                rows.append(row)
    return rows, bad


def _parse_row(raw: str, owner: str, skills: dict[str, Skill], row: Row) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return str(exc)
    if not isinstance(data, dict):
        return "not a JSON object"
    unknown = sorted(set(data) - ROW_KEYS)
    if unknown:
        return f"unknown keys {unknown}"
    intent = data.get("intent")
    if not isinstance(intent, str) or not intent.strip():
        return "intent must be a non-empty string"
    if "expected" not in data:
        return "expected is required"
    expected = data["expected"]
    if expected is not None and expected not in skills:
        return f"unknown skill: {expected}"
    if expected is not None and expected != owner:
        return f"expected {expected!r} in the fixture of {owner!r}"
    ambiguous = data.get("ambiguous_with", [])
    if not isinstance(ambiguous, list) or not all(isinstance(a, str) for a in ambiguous):
        return "ambiguous_with must be a list of strings"
    for name in ambiguous:
        if name not in skills:
            return f"unknown skill: {name}"
    if len(set(ambiguous)) != len(ambiguous):
        return "ambiguous_with has duplicates"
    if expected in ambiguous:
        return "ambiguous_with contains expected"
    row.intent, row.expected, row.ambiguous_with = intent, expected, list(ambiguous)
    return ""


def table(header: list[str], body: list[list[str]]) -> str:
    lines = [" | ".join(header), " | ".join("---" for _ in header)]
    lines.extend(" | ".join(cells) for cells in body)
    return "\n".join(lines)


def overlap_pairs(
    rows: list[Row], skills: dict[str, Skill]
) -> dict[tuple[str, str], list[tuple[Row, dict[str, list[str]]]]]:
    pairs: dict[tuple[str, str], list[tuple[Row, dict[str, list[str]]]]] = {}
    for row in rows:
        hits = matched(row.intent, skills)
        names = sorted(hits)
        for i, first in enumerate(names):
            for second in names[i + 1 :]:
                pairs.setdefault((first, second), []).append((row, hits))
    return pairs


def overlap_table(rows: list[Row], skills: dict[str, Skill], only_undeclared: bool) -> str:
    body = []
    for (first, second), found in sorted(overlap_pairs(rows, skills).items()):
        declared = (first, second) in DECLARED_OVERLAPS
        if only_undeclared and declared:
            continue
        row, hits = found[0]
        trig = " / ".join(", ".join(f'"{t}"' for t in hits[n]) for n in (first, second))
        status = "declared" if declared else "UNDECLARED"
        body.append([first, second, str(len(found)), row.intent, trig, status])
    return table(["skill A", "skill B", "rows", "example intent", "triggers", "status"], body)


def _describe(hits: dict[str, list[str]]) -> str:
    return "; ".join(f'{n} <- "{t}"' for n, ts in sorted(hits.items()) for t in ts) or "-"


def route_problems(row: Row, skills: dict[str, Skill]) -> list[str]:
    hits = matched(row.intent, skills)
    problems = []
    expected = row.expected
    if expected is not None and skills[expected].routable and expected not in hits:
        problems.append("missed")
    extra = sorted(set(hits) - {expected} - set(row.ambiguous_with))
    if extra:
        problems.append("also matched: " + ", ".join(extra))
    for name in row.ambiguous_with:
        if name not in hits:
            problems.append(f"ambiguous_with not matched: {name}")
    return problems


@unittest.skipUnless(SKILLS_ROOT.is_dir(), "skills/ not present (sdist-only run)")
class _RepoCase(unittest.TestCase):
    skills: dict[str, Skill]
    rows: list[Row]
    bad: list[Row]
    skill_problems: list[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.skills, cls.skill_problems = load_skills()
        cls.rows, cls.bad = load_rows(cls.skills)


class TestTriggerExtraction(_RepoCase):
    def test_frontmatter_is_readable(self) -> None:
        self.assertEqual(self.skill_problems, [])

    def test_quoted_phrases_after_marker_only(self) -> None:
        recall = self.skills["recall"].triggers
        self.assertNotIn("one thing", recall)
        self.assertEqual(len(recall), 10, recall)
        self.assertIn("what was i doing", recall)

    def test_every_routable_skill_has_triggers(self) -> None:
        empty = [
            f'skill {name}: description has no quoted trigger after "{MARKER}"'
            for name, skill in sorted(self.skills.items())
            if skill.routable and not skill.triggers
        ]
        if empty:
            self.fail("\n".join(empty))

    def test_setup_is_not_routable(self) -> None:
        self.assertFalse(self.skills["setup"].routable)
        head = read_head(SKILLS_ROOT / "setup" / "SKILL.md")
        self.assertEqual(head.get("disable-model-invocation"), "true")


class TestMatcher(unittest.TestCase):
    def test_normalise_and_stem(self) -> None:
        self.assertEqual(normalise("  Exact-Match_search, NOW! "), "exact match search now")
        for word, want in (
            ("sessions", "session"),
            ("meetings", "meet"),
            ("working", "work"),
            ("exported", "export"),
            ("status", "status"),
            ("access", "access"),
            ("analysis", "analysis"),
            ("scheduled", "schedul"),
            ("schedule", "schedule"),
        ):
            self.assertEqual(stem(word), want, word)

    def test_weak_trigger_keeps_stopwords(self) -> None:
        self.assertEqual(trigger_tokens("what was I doing"), {"what", "was", "i", "doing"})
        self.assertTrue(trigger_matches("what was I doing", "so what was I doing on friday"))
        self.assertFalse(trigger_matches("what was I doing", "what is codex doing right now"))
        # A single-word trigger is never weak.
        self.assertEqual(trigger_tokens("recall"), {"recall"})

    def test_token_set_ignores_order(self) -> None:
        self.assertEqual(trigger_tokens("export sessions"), {"export", "session"})
        self.assertTrue(trigger_matches("export sessions", "sessions from today, export them"))
        self.assertTrue(trigger_matches("ask the widget", "the widget, can you ask it"))
        self.assertFalse(trigger_matches("export sessions", "export the meetings"))

    def test_copy_lint(self) -> None:
        self.assertTrue(copied("Export Sessions!", "export sessions"))
        self.assertTrue(copied("please export sessions now", "export sessions"))
        self.assertFalse(copied("export my sessions", "export sessions"))
        self.assertFalse(copied("can you recall the bug", "recall"))
        self.assertTrue(copied("recall", "recall"))
        self.assertFalse(copied("re-export sessions", "port sessions"))


class TestRoutingEval(_RepoCase):
    def _fail_with(self, problems: list[list[str]]) -> None:
        if not problems:
            return
        header = ["file:line", "intent", "expected", "matched (skill <- trigger)", "problem"]
        message = [
            f"routing-eval: {len(problems)} problem(s)",
            table(header, problems),
            "",
            "overlap pairs",
            overlap_table(self.rows, self.skills, only_undeclared=False),
        ]
        self.fail("\n".join(message))

    def test_rows_are_well_formed(self) -> None:
        problems = [[r.where, "-", "-", "-", r.intent] for r in self.bad]
        seen: dict[str, Row] = {}
        for row in self.rows:
            key = normalise(row.intent)
            if key in seen:
                problems.append(
                    [
                        row.where,
                        row.intent,
                        str(row.expected),
                        "-",
                        f"duplicate intent (also {seen[key].where})",
                    ]
                )
            else:
                seen[key] = row
        self._fail_with(problems)

    def test_no_row_copies_a_trigger(self) -> None:
        problems = []
        for row in self.rows:
            for name, skill in sorted(self.skills.items()):
                for trig in skill.triggers:
                    if copied(row.intent, trig):
                        problems.append(
                            [
                                row.where,
                                row.intent,
                                str(row.expected),
                                "-",
                                f'copied from trigger: {name} "{trig}"',
                            ]
                        )
        self._fail_with(problems)

    def test_each_row_routes_to_its_skill(self) -> None:
        problems = []
        for row in self.rows:
            hits = matched(row.intent, self.skills)
            for problem in route_problems(row, self.skills):
                problems.append(
                    [row.where, row.intent, str(row.expected), _describe(hits), problem]
                )
        self._fail_with(problems)

    def test_overlap_pairs_are_declared(self) -> None:
        pairs = overlap_pairs(self.rows, self.skills)
        undeclared = [pair for pair in pairs if pair not in DECLARED_OVERLAPS]
        if undeclared:
            self.fail(
                "undeclared overlap pairs\n"
                + overlap_table(self.rows, self.skills, only_undeclared=True)
                + "\n\nall overlap pairs\n"
                + overlap_table(self.rows, self.skills, only_undeclared=False)
            )
        stale = sorted(DECLARED_OVERLAPS - set(pairs))
        self.assertEqual(stale, [], "DECLARED_OVERLAPS lists a pair no row exercises")

    def test_fixture_counts(self) -> None:
        counts = {name: 0 for name in self.skills}
        for row in self.rows:
            if row.expected is not None:
                counts[row.expected] += 1
        wrong = [
            f"skill {name}: {count} fixture rows, need {MIN_ROWS} to {MAX_ROWS}"
            for name, count in sorted(counts.items())
            if not MIN_ROWS <= count <= MAX_ROWS
        ]
        if wrong:
            self.fail("\n".join(wrong))


if __name__ == "__main__":
    unittest.main()
