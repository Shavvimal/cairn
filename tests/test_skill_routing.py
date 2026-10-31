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
import tempfile
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
# The umbrella cairn skill carries about 26 quoted phrases, one row each.
MAX_ROWS = 30
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
        ("cairn", "recall"),
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
    """Content tokens of a trigger. A trigger left with fewer than two content tokens keeps
    all its words (the weak-trigger rule), so "what was I doing" does not reduce to
    ``{doing}`` and a one-word stopword trigger does not reduce to nothing."""
    words = normalise(trigger).split()
    kept = frozenset(stem(w) for w in words if w not in STOPWORDS)
    if len(kept) < 2:
        return frozenset(stem(w) for w in words)
    return kept


def trigger_matches(trigger: str, intent: str) -> bool:
    """An empty trigger matches nothing; ``load_skills`` reports it."""
    tokens = trigger_tokens(trigger)
    return bool(tokens) and tokens <= intent_tokens(intent)


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
    key = ""
    for line in lines[1:]:
        if line.strip() == "---":
            return head
        if line and not line[0].isspace() and ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            # A folded or literal block starts on the next line.
            head[key] = "" if value in (">", "|", ">-", "|-") else value
        elif line.strip() and key:
            # An indented continuation line folds into the value with one space.
            head[key] = f"{head[key]} {line.strip()}".strip()
    raise FrontmatterError("frontmatter not closed")


def quotes_balanced(description: str) -> bool:
    """True if the text after the marker holds an even number of double quotes."""
    index = description.find(MARKER)
    return index < 0 or description[index:].count('"') % 2 == 0


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
        description = head.get("description", "")
        if not quotes_balanced(description):
            problems.append(f'skill {directory}: unbalanced quotes after "{MARKER}"')
        found = tuple(triggers(description))
        if any(not trigger_tokens(t) for t in found):
            problems.append(f"skill {directory}: a quoted trigger has no words")
        skills[directory] = Skill(
            name=directory,
            triggers=found,
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
        if owner not in skills:
            reason = f"bad row: fixture of unknown skill {owner!r}"
            bad.append(Row(path=path, line=1, intent=reason))
            continue
        lines = path.read_text(encoding="utf-8").split("\n")
        last = max((n for n, raw in enumerate(lines, 1) if raw.strip()), default=0)
        for number, raw in enumerate(lines, 1):
            if not raw.strip():
                if number < last:
                    bad.append(Row(path=path, line=number, intent="bad row: blank line"))
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
    if not isinstance(intent, str) or not normalise(intent):
        return "intent must be a non-empty string"
    if "expected" not in data:
        return "expected is required"
    expected = data["expected"]
    if expected is not None and not isinstance(expected, str):
        return "expected must be a skill name or null"
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
    if ambiguous and (expected is None or not skills[expected].routable):
        return "ambiguous_with must be empty when expected is null or not routable"
    if not isinstance(data.get("note", ""), str):
        return "note must be a string"
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


def overlap_problems(
    pairs: dict[tuple[str, str], list[tuple[Row, dict[str, list[str]]]]],
    declared: frozenset[tuple[str, str]] = DECLARED_OVERLAPS,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Pairs no declaration covers, and declared pairs no row exercises."""
    return sorted(set(pairs) - declared), sorted(declared - set(pairs))


def uncovered_triggers(rows: list[Row], skills: dict[str, Skill]) -> list[str]:
    """Triggers whose deletion no row would notice.

    A trigger is covered when a row that names its skill (``expected`` or
    ``ambiguous_with``) matches that skill through this trigger alone, so deleting the
    trigger makes the row fail ``missed`` or ``ambiguous_with not matched``. A trigger
    whose tokens hold all the tokens of another trigger of the same skill is subsumed:
    every intent it matches also matches the shorter one, so no row can isolate it.
    Two triggers of one skill with the same tokens are reported, because each hides
    the other.
    """
    problems = []
    for name, skill in sorted(skills.items()):
        if not skill.routable:
            continue
        sole: set[str] = set()
        for row in rows:
            if name != row.expected and name not in row.ambiguous_with:
                continue
            hits = [t for t in skill.triggers if trigger_matches(t, row.intent)]
            if len(hits) == 1:
                sole.add(hits[0])
        for trig in skill.triggers:
            tokens = trigger_tokens(trig)
            others = [trigger_tokens(t) for t in skill.triggers if t != trig]
            if tokens in others:
                problems.append(f'skill {name}: "{trig}" has the tokens of another trigger')
            elif not any(o < tokens for o in others) and trig not in sole:
                problems.append(f'skill {name}: no row matches "{trig}" alone')
    return problems


def _describe(hits: dict[str, list[str]]) -> str:
    return "; ".join(f'{n} <- "{t}"' for n, ts in sorted(hits.items()) for t in ts) or "-"


def route_problems(row: Row, skills: dict[str, Skill]) -> list[str]:
    hits = matched(row.intent, skills)
    problems = []
    expected = row.expected
    if expected is not None and skills[expected].routable and expected not in hits:
        problems.append("missed")
    # A null or non-routable row may match no routable skill; no exemption applies.
    routable = expected is not None and skills[expected].routable
    allowed = set(row.ambiguous_with) if routable else set()
    extra = sorted(set(hits) - {expected} - allowed)
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
        self.assertEqual(len(recall), 15, recall)
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
        # A single-word trigger keeps its word, even a stopword; an empty one matches nothing.
        self.assertEqual(trigger_tokens("recall"), {"recall"})
        self.assertEqual(trigger_tokens("all"), {"all"})
        self.assertFalse(trigger_matches("all", "export my sessions"))
        self.assertFalse(trigger_matches(", ", "export my sessions"))

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


# Inline skills for the synthetic checks below, so a description edit cannot break them.
_SYN = {
    "alpha": Skill("alpha", ("export widgets", "widget report", "export widgets daily"), True),
    "beta": Skill("beta", ("ask beta",), True),
    "setup": Skill("setup", ("install gadget",), False),
}


def _row(intent: str, expected: str | None, ambiguous: list[str] | None = None) -> Row:
    return Row(
        path=Path("/tmp/x.jsonl"),
        line=1,
        intent=intent,
        expected=expected,
        ambiguous_with=ambiguous or [],
    )


class TestRowChecks(unittest.TestCase):
    def test_parse_rejects_each_bad_shape(self) -> None:
        cases = [
            ("[1]", "alpha", "not a JSON object"),
            ('{"intent": "x", "expected": null, "why": 1}', "alpha", "unknown keys ['why']"),
            ('{"intent": " ", "expected": null}', "alpha", "intent must be a non-empty string"),
            ('{"intent": 3, "expected": null}', "alpha", "intent must be a non-empty string"),
            ('{"intent": "???", "expected": null}', "alpha", "intent must be a non-empty string"),
            ('{"intent": "x"}', "alpha", "expected is required"),
            ('{"intent": "x", "expected": []}', "alpha", "expected must be a skill name or null"),
            ('{"intent": "x", "expected": {}}', "alpha", "expected must be a skill name or null"),
            ('{"intent": "x", "expected": "gamma"}', "alpha", "unknown skill: gamma"),
            (
                '{"intent": "x", "expected": "beta"}',
                "alpha",
                "expected 'beta' in the fixture of 'alpha'",
            ),
            (
                '{"intent": "x", "expected": "alpha", "ambiguous_with": "beta"}',
                "alpha",
                "ambiguous_with must be a list of strings",
            ),
            (
                '{"intent": "x", "expected": "alpha", "ambiguous_with": ["gamma"]}',
                "alpha",
                "unknown skill: gamma",
            ),
            (
                '{"intent": "x", "expected": "alpha", "ambiguous_with": ["beta", "beta"]}',
                "alpha",
                "ambiguous_with has duplicates",
            ),
            (
                '{"intent": "x", "expected": "alpha", "ambiguous_with": ["alpha"]}',
                "alpha",
                "ambiguous_with contains expected",
            ),
            (
                '{"intent": "x", "expected": null, "ambiguous_with": ["beta"]}',
                "alpha",
                "ambiguous_with must be empty when expected is null or not routable",
            ),
            (
                '{"intent": "x", "expected": "setup", "ambiguous_with": ["beta"]}',
                "setup",
                "ambiguous_with must be empty when expected is null or not routable",
            ),
            ('{"intent": "x", "expected": null, "note": 5}', "alpha", "note must be a string"),
        ]
        for raw, owner, want in cases:
            row = _row("", None)
            self.assertEqual(_parse_row(raw, owner, _SYN, row), want, raw)
        self.assertTrue(_parse_row("{", "alpha", _SYN, _row("", None)))
        good = _row("", None)
        self.assertEqual(
            _parse_row('{"intent": "x", "expected": null, "note": "n"}', "alpha", _SYN, good), ""
        )
        self.assertEqual((good.intent, good.expected), ("x", None))

    def test_triggers_need_the_marker_and_balanced_quotes(self) -> None:
        self.assertEqual(triggers('Says "one two" here. No marker.'), [])
        good = f'Before "not this". {MARKER} "one two", "three four".'
        self.assertEqual(triggers(good), ["one two", "three four"])
        self.assertTrue(quotes_balanced(good))
        self.assertFalse(quotes_balanced(f'{MARKER} "one two", or "three four.'))

    def test_load_skills_reports_bad_descriptions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, head in (
                ("open", f'name: open\ndescription: x {MARKER} "one two", "three.'),
                ("empty", f'name: empty\ndescription: x {MARKER} "one two", "?!".'),
                ("named", f'name: other\ndescription: x {MARKER} "one two".'),
                (
                    "wrapped",
                    f'name: wrapped\ndescription: >\n  x {MARKER} "one two",\n  "three four".',
                ),
            ):
                (root / name).mkdir()
                (root / name / "SKILL.md").write_text(f"---\n{head}\n---\nbody\n", encoding="utf-8")
            skills, problems = load_skills(root)
        self.assertEqual(
            problems,
            [
                "skill empty: a quoted trigger has no words",
                "skill named: name is 'other'",
                f'skill open: unbalanced quotes after "{MARKER}"',
            ],
        )
        self.assertEqual(skills["wrapped"].triggers, ("one two", "three four"))

    def test_fixture_of_an_unknown_skill_is_a_bad_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "gone" / FIXTURE_NAME
            fixture.parent.mkdir()
            fixture.write_text('{"intent": "x y", "expected": null}\n', encoding="utf-8")
            rows, bad = load_rows(_SYN, root=Path(tmp))
        self.assertEqual(rows, [])
        self.assertEqual([r.intent for r in bad], ["bad row: fixture of unknown skill 'gone'"])

    def test_blank_line_inside_a_fixture_is_a_bad_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "alpha" / FIXTURE_NAME
            fixture.parent.mkdir()
            first = '{"intent": "x", "expected": "alpha"}'
            second = '{"intent": "y", "expected": "alpha"}'
            fixture.write_text(f"{first}\n\n{second}\n\n", encoding="utf-8")
            rows, bad = load_rows(_SYN, root=Path(tmp))
        self.assertEqual(len(rows), 2)
        self.assertEqual([(r.line, r.intent) for r in bad], [(2, "bad row: blank line")])

    def test_route_problems(self) -> None:
        cases = [
            (_row("send me the widget numbers", "alpha"), ["missed"]),
            (_row("export the widgets", "alpha"), []),
            (_row("export the widgets then ask beta", "alpha"), ["also matched: beta"]),
            (_row("export the widgets then ask beta", "alpha", ["beta"]), []),
            (_row("export the widgets", "alpha", ["beta"]), ["ambiguous_with not matched: beta"]),
            (_row("ask beta about the weather", None), ["also matched: beta"]),
            (_row("ask beta about the weather", None, ["beta"]), ["also matched: beta"]),
            (_row("install the gadget and ask beta", "setup", ["beta"]), ["also matched: beta"]),
            (_row("install the gadget", "setup"), []),
        ]
        for row, want in cases:
            self.assertEqual(route_problems(row, _SYN), want, row.intent)

    def test_read_head_needs_both_delimiters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "SKILL.md"
            for text, want in (
                ("name: x\n---\n", "frontmatter not opened"),
                ("---\nname: x\nbody\n", "frontmatter not closed"),
            ):
                path.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(FrontmatterError, want):
                    read_head(path)
            path.write_text("---\nname: x\ndescription: one\n  two\n---\nbody\n", encoding="utf-8")
            self.assertEqual(read_head(path), {"name": "x", "description": "one two"})

    def test_overlap_problems(self) -> None:
        rows = [_row("export the widgets then ask beta", "alpha", ["beta"])]
        pairs = overlap_pairs(rows, _SYN)
        self.assertEqual(list(pairs), [("alpha", "beta")])
        self.assertEqual(overlap_problems(pairs, frozenset()), ([("alpha", "beta")], []))
        self.assertEqual(overlap_problems(pairs, frozenset({("alpha", "beta")})), ([], []))
        self.assertEqual(
            overlap_problems({}, frozenset({("alpha", "beta")})), ([], [("alpha", "beta")])
        )

    def test_uncovered_triggers(self) -> None:
        both = _row("export a widget report", "alpha")
        self.assertEqual(
            uncovered_triggers([both], _SYN),
            [
                'skill alpha: no row matches "export widgets" alone',
                'skill alpha: no row matches "widget report" alone',
                'skill beta: no row matches "ask beta" alone',
            ],
        )
        rows = [
            both,
            _row("export the widgets", "alpha"),
            _row("show the widget report", "alpha"),
            _row("export widgets then ask beta", "alpha", ["beta"]),
        ]
        # "export widgets daily" holds the tokens of "export widgets": subsumed, exempt.
        self.assertEqual(uncovered_triggers(rows, _SYN), [])
        twins = {"alpha": Skill("alpha", ("export widgets", "widgets export"), True)}
        self.assertEqual(
            uncovered_triggers([_row("export the widgets", "alpha")], twins),
            [
                'skill alpha: "export widgets" has the tokens of another trigger',
                'skill alpha: "widgets export" has the tokens of another trigger',
            ],
        )


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
        undeclared, stale = overlap_problems(overlap_pairs(self.rows, self.skills))
        if undeclared:
            self.fail(
                "undeclared overlap pairs\n"
                + overlap_table(self.rows, self.skills, only_undeclared=True)
                + "\n\nall overlap pairs\n"
                + overlap_table(self.rows, self.skills, only_undeclared=False)
            )
        self.assertEqual(stale, [], "DECLARED_OVERLAPS lists a pair no row exercises")

    def test_every_trigger_has_its_own_row(self) -> None:
        problems = uncovered_triggers(self.rows, self.skills)
        if problems:
            self.fail("\n".join(problems))

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
