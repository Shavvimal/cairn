"""The ``## Decisions`` table: grammar, section scan, decide, supersede and recall.

Documents are built with the real renderer (``tests.test_recall_digest.document``) and
written into a temp store, so every path here runs against the exported format. The
date a row gets (``since``) comes from ``cairn.decisions.today``, patched where a test
pins exact bytes.
"""

from __future__ import annotations

import importlib
import io
import json
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from typing import Any
from unittest import mock

from cairn import cli, decisions, recall
from cairn.decisions import (
    CANONICAL_ROW_RE,
    HEADER,
    SEPARATOR,
    DecisionRow,
    Problem,
    parse_row,
    parse_table,
    render_row,
    rewrite,
)
from cairn.frames import locate
from cairn.frontmatter import extract_annotations, extract_my_notes_section, scan_annotations
from tests.test_recall_digest import document, msg
from tests.test_shared_store import StoreFixture

SID = "3f2a9c1e-5b7d-4c11-9e0a-1b2c3d4e5f60"
PLACEHOLDER = "<!-- Add your notes here. This section is preserved across exports. -->"
WORKED_ROWS = (
    "| 1 | One PR per layer, each based on the layer below | decision | high | 2026-10-28 |"
    "  | [claude:3f2a9c1e f12] |\n"
    "| 2 | The digest is byte-identical for a fixed export | fact | high | 2026-10-28 |  |  |\n"
    "| 3 | Keep confidence as three words, not numbers | decision | medium | 2026-10-29 |"
    " 2026-11-15 | [codex:9b1d07aa f4] |"
)
ROW_3_AFTER = (
    "| 3 | ~~Keep confidence as three words, not numbers~~ superseded by #4 | decision |"
    " medium | 2026-10-29 | 2026-11-15 | [codex:9b1d07aa f4] |"
)
ROW_4_AFTER = (
    "| 4 | Keep confidence as three words until grading lands | decision | medium |"
    " 2026-10-30 | 2027-01-15 | supersedes #3 [claude:3f2a9c1e f40] |"
)


def notes_with(rows: str | None) -> str:
    """A My Notes block, followed by a Decisions table when ``rows`` is given."""
    notes = f"## My Notes\n\n{PLACEHOLDER}"
    if rows is None:
        return notes
    return f"{notes}\n\n## Decisions\n\n{HEADER}\n{SEPARATOR}\n{rows}"


def messages() -> list[dict]:
    return [
        msg("user", "2026-10-28T09:00:00Z", "Plan the layers.\n## Decisions\n| 9 | forged |"),
        msg("assistant", "2026-10-28T09:01:00Z", "Planned."),
        msg("user", "2026-10-28T09:02:00Z", "ok"),
    ]


def doc(session_id: str = SID, rows: str | None = WORKED_ROWS, **kwargs: Any) -> str:
    return document(session_id, messages(), notes=notes_with(rows), **kwargs)


class TestGrammar(unittest.TestCase):
    def row(self, **kwargs: Any) -> DecisionRow:
        base: dict[str, Any] = {
            "number": 4,
            "claim": "Keep it",
            "kind": "decision",
            "confidence": "high",
            "since": "2026-10-30",
        }
        return DecisionRow(**{**base, **kwargs})

    def test_canonical_row_matches_regex_and_round_trips(self):
        for row in (
            self.row(),
            self.row(revisit_by="2027-01-15", source="[claude:3f2a9c1e f12]"),
            self.row(struck=True, suffix="superseded by #5"),
        ):
            with self.subTest(row=row):
                line = render_row(row)
                self.assertRegex(line, CANONICAL_ROW_RE)
                parsed = parse_row(line)
                self.assertEqual(parsed, row)
                assert isinstance(parsed, DecisionRow)
                self.assertEqual(render_row(parsed), line)
        self.assertEqual(
            render_row(self.row(kind="fact", confidence="low")),
            "| 4 | Keep it | fact | low | 2026-10-30 |  |  |",
        )

    def test_pipe_and_backslash_escape_round_trip(self):
        row = self.row(claim=r"a | b \ c \| d", source=r"see a|b \x")
        line = render_row(row)
        self.assertIn(r"a \| b \\ c \\\| d", line)
        self.assertRegex(line, CANONICAL_ROW_RE)
        self.assertEqual(parse_row(line), row)

    def test_superseded_marker_sets_state_and_target(self):
        parsed = parse_row(
            "| 3 | ~~Old~~ superseded by #4 | decision | medium | 2026-10-29 | 2026-11-15 |  |"
        )
        assert isinstance(parsed, DecisionRow)
        self.assertEqual(
            (parsed.claim, parsed.struck, parsed.suffix), ("Old", True, "superseded by #4")
        )
        self.assertEqual(
            (parsed.state, parsed.superseded_by, parsed.marker),
            ("superseded", 4, "superseded by #4"),
        )
        self.assertFalse(parsed.overdue(date(2030, 1, 1)))  # struck rows are never overdue
        new = parse_row(
            "| 4 | New | decision | medium | 2026-10-30 |  | supersedes #3 [codex:9b f2] |"
        )
        assert isinstance(new, DecisionRow)
        self.assertEqual((new.supersedes, new.citations), (3, ["[codex:9b f2]"]))

    def test_unknown_marker_is_struck_and_kept_verbatim(self):
        # The contract for a later writer: any marker is kept and classed as struck.
        for cell, marker in (
            ("~~Use the queue~~ withdrawn: wrong", "withdrawn: wrong"),
            (
                "~~Use the queue~~ superseded by #4, withdrawn: private",
                "superseded by #4, withdrawn: private",
            ),
            ("~~Use the queue~~", None),
        ):
            with self.subTest(cell=cell):
                line = f"| 2 | {cell} | fact | low | 2026-10-29 |  |  |"
                parsed = parse_row(line)
                assert isinstance(parsed, DecisionRow)
                self.assertEqual(
                    (parsed.state, parsed.marker, parsed.claim), ("struck", marker, "Use the queue")
                )
                self.assertIsNone(parsed.superseded_by)
                self.assertEqual(render_row(parsed), line)

    def test_unknown_kind_is_a_problem_with_line_number(self):
        lines = ["## Decisions", "", HEADER, SEPARATOR, "| 1 | x | bet | high | 2026-10-30 |  |  |"]
        rows, problems = parse_table(lines, (0, len(lines)))
        self.assertEqual(rows, [])
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0].line, 5)
        self.assertIn("unknown kind 'bet'", problems[0].reason)

    def test_duplicate_number_is_a_problem(self):
        row = "| 1 | x | fact | high | 2026-10-30 |  |  |"
        lines = ["## Decisions", HEADER, SEPARATOR, row, row.replace("x", "y")]
        rows, problems = parse_table(lines, (0, len(lines)))
        self.assertEqual([r.claim for r in rows], ["x"])
        self.assertEqual(problems, [Problem(5, "duplicate row #1")])

    def test_dangling_superseded_by_is_a_problem(self):
        lines = [
            "## Decisions",
            HEADER,
            SEPARATOR,
            "| 1 | ~~x~~ superseded by #7 | fact | high | 2026-10-30 |  |  |",
            "| 2 | y | fact | high | 2026-10-30 |  |  |",
        ]
        rows, problems = parse_table(lines, (0, len(lines)))
        self.assertEqual([r.number for r in rows], [2])
        self.assertEqual(problems, [Problem(4, "row #1 is superseded by #7, which does not exist")])

    def test_lenient_whitespace_strict_values(self):
        lenient = "  |1|  Keep it   |decision|high|2026-10-30||  |  "
        self.assertEqual(parse_row(lenient), self.row(number=1))
        lines = ["## Decisions", f"{HEADER}   ", "| --- |:---|---:|---|---|---|---|", lenient]
        rows, problems = parse_table(lines, (0, len(lines)))
        self.assertEqual((len(rows), problems), (1, []))
        for line, reason in (
            ("| 1 | x | fact | High | 2026-10-30 |  |  |", "unknown confidence 'High'"),
            ("| 1 | x | fact | high | 2026-02-30 |  |  |", "since must be a date"),
            ("| 1 | x | fact | high | 2026-10-30 | soon |  |", "revisit_by must be a date"),
            ("| 01 | x | fact | high | 2026-10-30 |  |  |", "positive integer"),
            ("| 1 | x ~~y~~ | fact | high | 2026-10-30 |  |  |", "'~~' outside a strike"),
            ("| 1 |  | fact | high | 2026-10-30 |  |  |", "claim is empty"),
            ("| 1 | x | fact | high | 2026-10-30 |  |", "expected 7 cells, got 6"),
            ("1 | x | fact | high | 2026-10-30 |  |  |", "expected a table row"),
        ):
            with self.subTest(line=line):
                parsed = parse_row(line, 9)
                assert isinstance(parsed, Problem)
                self.assertEqual(parsed.line, 9)
                self.assertIn(reason, parsed.reason)


class TestSections(unittest.TestCase):
    def test_decisions_directly_after_my_notes_is_captured(self):
        text = doc()
        lines = text.splitlines()
        notes, section = scan_annotations(lines)
        assert notes is not None and section is not None
        self.assertEqual(lines[notes[0]], "## My Notes")
        self.assertEqual(lines[section[0]], "## Decisions")
        self.assertEqual(lines[section[1]], "## Conversation")
        self.assertEqual(extract_annotations(text), notes_with(WORKED_ROWS))
        # The same ranges with line breaks kept (the writer's view).
        self.assertEqual(scan_annotations(text.splitlines(keepends=True)), (notes, section))

    def test_decisions_heading_in_summary_or_conversation_is_ignored(self):
        table = f"## Decisions\n\n{HEADER}\n{SEPARATOR}\n| 1 | x | fact | high | 2026-10-30 |  |  |"
        meeting = f"# M\n\n## Summary\n\n{table}\n\n## My Notes\n\nmine\n\n## Transcript\n\nhi\n"
        self.assertEqual(scan_annotations(meeting.splitlines(), conversation_heading=None)[1], None)
        self.assertEqual(
            extract_annotations(meeting, conversation_heading=None), "## My Notes\n\nmine"
        )
        # A section after another heading is not directly after My Notes.
        gap = f"## My Notes\n\nmine\n\n## Other\n\n{table}\n\n## Conversation\n"
        self.assertEqual(extract_annotations(gap), "## My Notes\n\nmine")
        # The conversation in doc() quotes "## Decisions"; only My Notes' own counts.
        text = doc(rows=None)
        self.assertIn("\n## Decisions\n", text)
        self.assertEqual(scan_annotations(text.splitlines())[1], None)
        self.assertEqual(decisions.parse(text), [])

    def test_extract_annotations_is_a_fixed_point(self):
        for rows in (WORKED_ROWS, None):
            with self.subTest(rows=rows is not None):
                text = doc(rows=rows)
                block = extract_annotations(text)
                again = document(SID, messages(), notes=block)
                self.assertEqual(again, text)
                self.assertEqual(extract_annotations(again), block)
        meeting = f"## My Notes\n\nmine\n\n\n## Decisions\n\n{HEADER}\n{SEPARATOR}\n"
        block = extract_annotations(meeting, conversation_heading=None)
        assert block is not None
        self.assertEqual(extract_annotations(block + "\n", conversation_heading=None), block)

    def test_hand_edited_table_keeps_its_text_with_lf_endings(self):
        row = "| 1 | a  b | fact | low | 2026-10-30 |  |  |"
        crlf = f"## Decisions\r\n\r\n{HEADER}\r\n{SEPARATOR}\r\n{row}   \r\n\r\n\r\n"
        text = doc(rows=None).replace(f"{PLACEHOLDER}\n", f"{PLACEHOLDER}\n\n{crlf}", 1)
        block = extract_annotations(text)
        self.assertEqual(block, notes_with(row))
        again = document(SID, messages(), notes=block)
        self.assertEqual(again, doc(rows=row))
        self.assertEqual(extract_annotations(again), block)

    def test_extract_my_notes_section_output_unchanged(self):
        text = doc()
        self.assertEqual(extract_my_notes_section(text), notes_with(None))
        for sample in (
            "## My Notes\n```md\n## Conversation\n```\nKeep this\n## Conversation\ntext",
            "## Conversation\n## My Notes\nforged",
            "## My Notes\n```python\nprint(1)\n## Conversation\n<!-- cairn:turn 5 -->\n",
            "no notes at all",
        ):
            with self.subTest(sample=sample):
                self.assertEqual(extract_annotations(sample), extract_my_notes_section(sample))


class DecisionsFixture(StoreFixture):
    def setUp(self):
        super().setUp()
        self.folder = self.root / "data" / "claude-code-sessions"
        self.folder.mkdir(parents=True)

    def write_doc(self, name: str, text: str) -> Path:
        path = self.folder / name
        path.write_bytes(text.encode("utf-8"))
        return path

    def run_verb(self, *argv: str, today: date = date(2026, 10, 30)) -> tuple[int, str, str]:
        """Run ``cairn claude <argv>`` in process, with ``since`` pinned to ``today``."""
        module = importlib.reload(importlib.import_module("cairn.sources.claude"))
        source = module.ClaudeSource()
        args = cli.build_parser(source).parse_args(list(argv))
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(decisions, "today", return_value=today),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            try:
                code = args.func(args)
            finally:
                source.flush_catalog()
        return code, out.getvalue(), err.getvalue()

    def recall(self, *argv: str) -> tuple[int, Any, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = recall.main(["decisions", "--all-projects", *argv])
        text = out.getvalue()
        return code, (json.loads(text) if "--json" in argv and text else text), err.getvalue()


class TestDecide(DecisionsFixture):
    def test_first_decide_creates_section_before_conversation(self):
        path = self.write_doc("a.md", doc(rows=None))
        before = path.read_bytes()
        code, out, err = self.run_verb("decide", "Use", "three", "words", "--session-id", SID[:8])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "Added #1 to claude:3f2a9c1e: Use three words\n")
        row = "| 1 | Use three words | decision | medium | 2026-10-30 |  |  |"
        old_tail = f"{PLACEHOLDER}\n\n## Conversation\n"
        new_tail = (
            f"{PLACEHOLDER}\n\n## Decisions\n\n{HEADER}\n{SEPARATOR}\n{row}\n\n## Conversation\n"
        )
        self.assertEqual(path.read_bytes(), before.replace(old_tail.encode(), new_tail.encode(), 1))

    def test_next_number_is_max_plus_one(self):
        rows = WORKED_ROWS.replace("| 2 |", "| 7 |")
        path = self.write_doc("a.md", doc(rows=rows))
        code, out, _ = self.run_verb(
            "decide", "Ship it", "--kind", "commitment", "--confidence", "low", "--revisit-by",
            "2026-11-30", "--source", "[claude:3f2a9c1e f1] [codex:9b1d07aa f0]", "--session-id",
            SID, "--json",
        )  # fmt: skip
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["ref"], f"claude:{SID}#8")
        self.assertEqual(data["path"], str(path))
        self.assertEqual(data["row"]["citations"], ["[claude:3f2a9c1e f1]", "[codex:9b1d07aa f0]"])
        self.assertEqual(
            (data["row"]["kind"], data["row"]["confidence"], data["row"]["state"]),
            ("commitment", "low", "active"),
        )
        new = (
            "| 8 | Ship it | commitment | low | 2026-10-30 | 2026-11-30 |"
            " [claude:3f2a9c1e f1] [codex:9b1d07aa f0] |"
        )
        self.assertIn(f"{rows}\n{new}\n\n## Conversation\n", path.read_text())

    def test_bytes_from_anchor_to_eof_unchanged_and_frames_locate_identically(self):
        path = self.write_doc("a.md", doc(rows=None))
        text = path.read_text()
        anchor, frames = locate(text)
        self.assertEqual(self.run_verb("decide", "x", "--session-id", SID)[0], 0)
        self.assertEqual(self.run_verb("supersede", "1", "y", "--session-id", SID)[0], 0)
        after = path.read_text()
        new_anchor, new_frames = locate(after)
        self.assertEqual(after[new_anchor:], text[anchor:])
        self.assertEqual(new_frames, frames)
        self.assertEqual(after[: text.index("## My Notes")], text[: text.index("## My Notes")])

    def test_refuses_malformed_table_without_writing(self):
        bad = WORKED_ROWS.replace("| fact |", "| bet |")
        path = self.write_doc("a.md", doc(rows=bad))
        before = path.read_bytes()
        line = path.read_text().splitlines().index(bad.splitlines()[1]) + 1
        for argv in (("decide", "x"), ("supersede", "1", "x")):
            with self.subTest(argv=argv):
                code, out, err = self.run_verb(*argv, "--session-id", SID)
                self.assertEqual((code, out), (1, ""))
                self.assertEqual(
                    err,
                    f"Error: cannot parse ## Decisions in a.md: line {line}: unknown kind 'bet'; "
                    "use one of decision, fact, commitment, hunch\n",
                )
                self.assertEqual(path.read_bytes(), before)
        no_notes = self.write_doc("b.md", doc("bbbbbbbb-0002").replace("## My Notes", "## Mine"))
        code, _, err = self.run_verb("decide", "x", "--session-id", "bbbbbbbb")
        self.assertEqual(code, 1)
        self.assertEqual(
            err,
            "Error: b.md has no ## My Notes section; re-export it with cairn claude export --all\n",
        )
        self.assertNotIn("| x |", no_notes.read_text())

    def test_rejects_strike_marker_long_claim_bad_date_bad_citation_exit_2(self):
        path = self.write_doc("a.md", doc())
        before = path.read_bytes()
        for argv, message in (
            (("decide", " \n "), "claim is empty"),
            (("decide", "a", "~~b~~"), "claim must not contain '~~' (it marks a struck row)"),
            (("decide", "x" * 501), "claim is longer than 500 characters (got 501)"),
            (
                ("decide", "x", "--revisit-by", "2026-10-29"),
                "--revisit-by must be YYYY-MM-DD on or after 2026-10-30, got '2026-10-29'",
            ),
            (
                ("decide", "x", "--revisit-by", "2026-13-01"),
                "--revisit-by must be YYYY-MM-DD on or after 2026-10-30, got '2026-13-01'",
            ),
            (
                ("supersede", "3", "x", "--source", "[claude:3f2a9c1e f12],[x]"),
                "--source must be frame citations like [claude:3f2a9c1e f12], separated by "
                "spaces, got '[claude:3f2a9c1e f12],[x]'",
            ),
            (
                ("decide", "x", "--source", "[claude:3f2a9c1e  f12]"),
                "--source must be frame citations like [claude:3f2a9c1e f12], separated by "
                "spaces, got '[claude:3f2a9c1e  f12]'",
            ),
        ):
            with self.subTest(argv=argv):
                code, out, err = self.run_verb(*argv, "--session-id", SID)
                self.assertEqual((code, out, err), (2, "", f"Error: {message}\n"))
        self.assertEqual(path.read_bytes(), before)
        # A claim at the limit, with line breaks collapsed, is accepted.
        code, out, _ = self.run_verb("decide", "a\r\nb  c", "-q", "--session-id", SID)
        self.assertEqual((code, out), (0, ""))
        self.assertIn("| 4 | a b c | decision |", path.read_text())

    def test_every_splitlines_break_in_a_claim_becomes_a_space(self):
        path = self.write_doc("a.md", doc())
        breaks = ["\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]
        for number, mark in enumerate(breaks, start=4):
            with self.subTest(mark=repr(mark)):
                code, _, err = self.run_verb(
                    "decide", f"first{mark}second", "-q", "--session-id", SID
                )
                self.assertEqual((code, err), (0, ""))
                rows = decisions.parse(path.read_bytes().decode("utf-8"))
                self.assertEqual((rows[-1].number, rows[-1].claim), (number, "first second"))

    def test_granola_section_lands_before_transcript(self):
        meeting = (
            "---\ntype: granola-meeting\nmeeting_id: m1\n---\n\n# Sync\n\n"
            "## Summary\n\n## Decisions\n\nnative heading\n\n"
            f"## My Notes\r\n\r\n{PLACEHOLDER}\r\n\r\n## Transcript\n\n## Decisions\n"
        )

        def add(rows: list[DecisionRow]) -> list[DecisionRow]:
            return [*rows, DecisionRow(1, "Hire", "decision", "high", "2026-10-30")]

        text, rows = rewrite(meeting, add, conversation_heading=None)
        self.assertEqual([r.number for r in rows], [1])
        block = (
            f"## My Notes\r\n\r\n{PLACEHOLDER}\r\n\r\n## Decisions\r\n\r\n{HEADER}\r\n{SEPARATOR}\r\n"
            "| 1 | Hire | decision | high | 2026-10-30 |  |  |\r\n\r\n## Transcript\n"
        )
        self.assertEqual(
            text, meeting.replace(f"## My Notes\r\n\r\n{PLACEHOLDER}\r\n\r\n## Transcript\n", block)
        )
        # A meeting whose My Notes ends the file gains a closing newline only.
        tail, _ = rewrite("## My Notes\n\nmine", add, conversation_heading=None)
        self.assertEqual(
            tail,
            f"## My Notes\n\nmine\n\n## Decisions\n\n{HEADER}\n{SEPARATOR}\n"
            "| 1 | Hire | decision | high | 2026-10-30 |  |  |\n",
        )


class TestSupersede(DecisionsFixture):
    def test_worked_example_exact_bytes(self):
        path = self.write_doc("a.md", doc())
        before = path.read_text()
        code, out, err = self.run_verb(
            "supersede", "3", "Keep confidence as three words until grading lands",
            "--revisit-by", "2027-01-15", "--source", "[claude:3f2a9c1e f40]",
            "--session-id", "3f2a9c1e",
        )  # fmt: skip
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            out,
            "Struck #3; added #4 to claude:3f2a9c1e: "
            "Keep confidence as three words until grading lands\n",
        )
        old_row = WORKED_ROWS.splitlines()[2]
        expected = before.replace(old_row + "\n", f"{ROW_3_AFTER}\n{ROW_4_AFTER}\n", 1)
        self.assertEqual(path.read_text(), expected)
        rows = decisions.parse(path.read_text())
        self.assertEqual([r.state for r in rows], ["active", "active", "superseded", "active"])
        self.assertEqual((rows[2].superseded_by, rows[3].supersedes), (4, 3))
        self.assertFalse(rows[2].overdue(date(2027, 6, 1)))

    def test_claim_starting_or_ending_with_a_tilde_survives_a_strike(self):
        for claim in ("ends with~", "~starts with", "~"):
            with self.subTest(claim=claim):
                path = self.write_doc("a.md", doc(rows=None))
                self.assertEqual(self.run_verb("decide", claim, "-q", "--session-id", SID)[0], 0)
                code, _, err = self.run_verb("supersede", "1", "next", "-q", "--session-id", SID)
                self.assertEqual((code, err), (0, ""))
                rows = decisions.parse(path.read_text())
                self.assertEqual(
                    [(r.claim, r.state) for r in rows], [(claim, "superseded"), ("next", "active")]
                )
                self.assertEqual(self.recall("--json", "--include-struck")[2], "")

    def test_hand_formatted_rows_keep_their_bytes_during_a_supersede(self):
        loose = "|  1|One PR per layer|decision|high|2026-10-28|| [claude:3f2a9c1e f12]  |"
        rows = WORKED_ROWS.replace(WORKED_ROWS.splitlines()[0], loose)
        path = self.write_doc("a.md", doc(rows=rows))
        self.assertEqual(self.run_verb("supersede", "3", "x", "-q", "--session-id", SID)[0], 0)
        lines = path.read_text().splitlines()
        self.assertIn(loose, lines)
        self.assertIn(WORKED_ROWS.splitlines()[1], lines)

    def test_missing_row_exit_1(self):
        path = self.write_doc("a.md", doc())
        before = path.read_bytes()
        code, _, err = self.run_verb("supersede", "9", "x", "--session-id", SID)
        self.assertEqual((code, err), (1, "Error: no row #9 in ## Decisions of a.md\n"))
        self.assertEqual(path.read_bytes(), before)
        self.write_doc("b.md", doc("bbbbbbbb-0002", rows=None))
        code, _, err = self.run_verb("supersede", "1", "x", "--session-id", "bbbbbbbb")
        self.assertEqual((code, err), (1, "Error: no row #1 in ## Decisions of b.md\n"))

    def test_already_struck_row_exit_1(self):
        rows = WORKED_ROWS.replace("| 2 | The digest", "| 2 | ~~The digest").replace(
            "fixed export |", "fixed export~~ withdrawn: wrong |"
        )
        path = self.write_doc("a.md", doc(rows=rows))
        before = path.read_bytes()
        code, _, err = self.run_verb("supersede", "2", "x", "--session-id", SID)
        self.assertEqual((code, err), (1, "Error: row #2 is already struck (withdrawn: wrong)\n"))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.run_verb("supersede", "3", "x", "--session-id", SID)[0], 0)
        code, _, err = self.run_verb("supersede", "3", "y", "--session-id", SID)
        self.assertEqual((code, err), (1, "Error: row #3 is already struck (superseded by #4)\n"))

    def test_inherits_kind_not_revisit_by(self):
        path = self.write_doc("a.md", doc())
        code, out, _ = self.run_verb("supersede", "2", "Still true", "--json", "--session-id", SID)
        self.assertEqual(code, 0)
        row = json.loads(out)["row"]
        self.assertEqual(
            (row["kind"], row["revisit_by"], row["source"]), ("fact", None, "supersedes #2")
        )
        self.assertEqual(row["supersedes"], 2)
        self.assertEqual(
            self.run_verb("supersede", "3", "x", "--kind", "hunch", "--session-id", SID)[0], 0
        )
        last = decisions.parse(path.read_text())[-1]
        self.assertEqual((last.number, last.kind, last.revisit_by), (5, "hunch", ""))


class TestRecallDecisions(DecisionsFixture):
    ROWS = (
        "| 1 | Old | decision | high | 2026-10-01 | 2026-10-20 |  |\n"
        "| 2 | ~~Wrong~~ superseded by #3 | fact | low | 2026-10-02 | 2026-10-05 |  |\n"
        "| 3 | Right | fact | high | 2026-10-03 | 2026-10-30 | supersedes #2 |\n"
        "| 4 | ~~Gone~~ withdrawn: private | hunch | low | 2026-10-04 | 2026-10-05 |  |"
    )

    def setUp(self):
        super().setUp()
        self.path = self.write_doc("a.md", doc(rows=self.ROWS))

    def test_hides_struck_rows_unless_include_struck(self):
        code, data, _ = self.recall("--json")
        self.assertEqual(code, 0)
        self.assertEqual([r["number"] for r in data["decisions"]], [1, 3])
        _, data, _ = self.recall("--json", "--include-struck")
        self.assertEqual(
            [(r["number"], r["state"], r["marker"]) for r in data["decisions"]],
            [
                (1, "active", None),
                (2, "superseded", "superseded by #3"),
                (3, "active", None),
                (4, "struck", "withdrawn: private"),
            ],
        )
        _, text, _ = self.recall("--include-struck", "--as-of", "2026-10-30")
        self.assertIn(
            "claude:3f2a9c1e#4  hunch  low  2026-10-04  revisit 2026-10-05  ~~Gone~~ [withdrawn: private]\n",
            text,
        )
        self.assertIn(
            "claude:3f2a9c1e#1  decision  high  2026-10-01  revisit 2026-10-20  Old  OVERDUE\n",
            text,
        )
        self.assertTrue(text.endswith("4 decisions\n"))

    def test_date_expr_filters_on_since(self):
        _, data, _ = self.recall("2026-10-03", "--json")
        self.assertEqual([r["number"] for r in data["decisions"]], [3])
        _, data, _ = self.recall("2026-10-02", "--json", "--include-struck")
        self.assertEqual([r["number"] for r in data["decisions"]], [2])
        code, _, err = self.recall("someday")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("Error: "))

    def test_overdue_is_strictly_before_as_of(self):
        for as_of, expected in (("2026-10-20", []), ("2026-10-21", [1]), ("2026-10-31", [1, 3])):
            with self.subTest(as_of=as_of):
                _, data, _ = self.recall("--overdue", "--as-of", as_of, "--json")
                self.assertEqual([r["number"] for r in data["decisions"]], expected)
                self.assertTrue(all(r["overdue"] for r in data["decisions"]))
        code, _, err = self.recall("--as-of", "2026-10-32")
        self.assertEqual((code, err), (1, "Error: --as-of must be YYYY-MM-DD, got '2026-10-32'\n"))

    def test_json_shape_and_problems_list(self):
        bad = self.write_doc(
            "b.md",
            doc(
                "bbbbbbbb-0002",
                rows="| 1 | Fine | fact | high | 2026-10-05 |  |  |\n| 2 | x | bet | high | 2026-10-05 |  |  |",
            ),
        )
        code, data, err = self.recall("--json", "--as-of", "2026-10-30")
        self.assertEqual(code, 0)
        self.assertEqual(set(data), {"as_of", "decisions", "problems"})
        self.assertEqual(data["as_of"], "2026-10-30")
        line = bad.read_text().splitlines().index("| 2 | x | bet | high | 2026-10-05 |  |  |") + 1
        reason = "unknown kind 'bet'; use one of decision, fact, commitment, hunch"
        self.assertEqual(data["problems"], [{"path": str(bad), "line": line, "reason": reason}])
        self.assertEqual(err, f"Warning: {bad}: line {line}: {reason}\n")
        row = next(r for r in data["decisions"] if r["number"] == 3)
        self.assertEqual(
            row,
            {
                "ref": f"claude:{SID}#3",
                "session": f"claude:{SID}",
                "number": 3,
                "claim": "Right",
                "kind": "fact",
                "confidence": "high",
                "since": "2026-10-03",
                "revisit_by": "2026-10-30",
                "source": "supersedes #2",
                "citations": [],
                "supersedes": 2,
                "state": "active",
                "marker": None,
                "superseded_by": None,
                "overdue": False,
                "title": "Fix the widget pipeline retry bug",
                "project": "demo",
                "path": str(self.path),
            },
        )
        self.assertIn("claude:bbbbbbbb-0002#1", [r["ref"] for r in data["decisions"]])

    def test_oversized_numbers_are_problems_not_crashes(self):
        big = "9" * 4301
        rows = (
            f"| {big} | Huge | fact | high | 2026-10-05 |  |  |\n"
            f"| 1 | ~~Odd~~ superseded by #{big} | fact | high | 2026-10-05 |  |  |\n"
            f"| 2 | Fine | fact | high | 2026-10-05 |  | supersedes #{big} |"
        )
        bad = self.write_doc("b.md", doc("bbbbbbbb-0002", rows=rows))
        code, data, err = self.recall("--json", "--include-struck", "--as-of", "2026-10-30")
        self.assertEqual(code, 0)
        found = {r["ref"]: r for r in data["decisions"] if r["session"].startswith("claude:bbbb")}
        self.assertEqual(sorted(found), ["claude:bbbbbbbb-0002#1", "claude:bbbbbbbb-0002#2"])
        self.assertEqual(found["claude:bbbbbbbb-0002#1"]["state"], "struck")
        self.assertIsNone(found["claude:bbbbbbbb-0002#2"]["supersedes"])
        line = bad.read_text().splitlines().index(rows.splitlines()[0]) + 1
        reason = "row number must be at most 999999999"
        self.assertEqual(data["problems"], [{"path": str(bad), "line": line, "reason": reason}])
        self.assertEqual(err, f"Warning: {bad}: line {line}: {reason}\n")
        code, _, err = self.run_verb("decide", "x", "--session-id", "bbbbbbbb")
        self.assertEqual(
            (code, err), (1, f"Error: cannot parse ## Decisions in b.md: line {line}: {reason}\n")
        )

    def test_reader_stops_at_frame_anchor(self):
        text = self.path.read_text()
        prefix = decisions._read_prefix(self.path, "## Conversation")
        self.assertEqual(prefix, text[: text.index("## Conversation\n\n<!-- cairn:turn ")])
        # A fenced fake anchor in My Notes does not stop the reader early.
        fenced = notes_with(None) + "\n\n```md\n## Conversation\n<!-- cairn:turn 9 -->\n```"
        fake = doc("cccccccc-0003", rows=None).replace(notes_with(None), fenced, 1)
        fake = fake.replace(
            "```\n\n## Conversation",
            f"```\n\n## Decisions\n\n{HEADER}\n{SEPARATOR}\n| 1 | Kept | fact | high | 2026-10-09 |  |  |\n\n## Conversation",
            1,
        )
        path = self.write_doc("c.md", fake)
        rows, problems = decisions.read_table(path, "## Conversation")
        self.assertEqual(([r.claim for r in rows], problems), (["Kept"], []))
        # A meeting has no frame anchor: the reader reads to the end.
        self.assertEqual(decisions._read_prefix(self.path, None), text)

    def test_read_cost_on_two_thousand_documents(self):
        """The cost of reading every table up to the anchor, on 2,000 invented documents."""
        long_messages = [
            msg("user" if n % 2 == 0 else "assistant", f"2026-10-28T09:{n:02d}:00Z", "z" * 2000)
            for n in range(40)
        ]
        table = notes_with("| 1 | Keep | decision | high | 2026-10-01 | 2026-10-10 |  |")
        for n in range(2000):
            notes = table if n % 2 == 0 else notes_with(None)
            text = document(f"doc{n:05d}-0000", long_messages, notes=notes)
            self.write_doc(f"d{n:05d}.md", text)
        self.assertEqual(self.recall("--json")[0], 0)  # warm the catalog, as a second run is
        started = time.perf_counter()
        code, data, _ = self.recall("--json", "--overdue", "--as-of", "2026-10-30")
        elapsed = time.perf_counter() - started
        self.assertEqual(code, 0)
        self.assertEqual(len(data["decisions"]), 1000 + 1)  # plus row 1 of setUp's document
        self.assertLess(
            elapsed, 10.0, f"recall decisions over 2,000 documents took {elapsed:.2f} s"
        )


if __name__ == "__main__":
    unittest.main()
