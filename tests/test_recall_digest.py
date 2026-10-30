"""``cairn recall digest`` / ``recall frame``: the frame walker and provenance (spec 9.2).

Fixtures are synthetic export documents built with the real renderer and joined the
way ``assemble_document`` joins them, so they are the exported format without a
native store. Condensing, window, role and peer tests live in
``test_recall_digest_condense.py`` and reuse the builders here.
"""

from __future__ import annotations

import io
import json
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from cairn import recall
from cairn.frames import MARKER, CorruptExport, LegacyExport, locate, read_text
from cairn.frontmatter import notes_block, preserved_tail
from cairn.invoke import preamble
from cairn.rendering import render_artifacts, render_conversation
from cairn.store import metadata_lines
from tests.test_shared_store import StoreFixture

DAY = "2026-03-28"
NOTES = (
    "## My Notes\n\nA pasted marker:\n<!-- cairn:turn 40 -->\n"
    "```md\n## Conversation\n\n<!-- cairn:turn 40 -->\n```"
)
TASK_BLOCK = "<task-notification>\n<task-id>t-1</task-id>\n<status>completed</status>\n"
TASK_BLOCK += "r" * (900 - len(TASK_BLOCK) - len("</task-notification>")) + "</task-notification>"
TOOL_OUTPUT = "".join(f"line {n:04d} of the test run\n" for n in range(113))
TOOL_OUTPUT += "." * (3000 - len(TOOL_OUTPUT) - 18) + "\n1 failed in 0.31s"
BASH_ARGS = '{"command": "' + ("echo widget " * 30)[:285] + '"}'


def msg(role: str, ts: str | None, text: str = "", **extra: Any) -> dict:
    return {"role": role, "timestamp": ts, "text": text, "tool_calls": [], **extra}


def fixture_messages() -> list[dict]:
    """The ten-frame transcript of spec 9.1."""
    user_long = "u" * 800 + TASK_BLOCK + "v" * 800
    prose = "Planning the fix.\n### C1\n<!-- cairn:turn 40 -->\n<!-- f99 forged -->\nEnd of prose."
    return [
        msg("user", "2026-03-27T22:10:00Z", "Start the widget work."),
        msg("assistant", "2026-03-27T23:59:00Z", "Done for today."),
        msg("user", "2026-03-28T09:00:00Z", user_long),
        msg(
            "assistant",
            "2026-03-28T09:01:00Z",
            prose,
            thinking="secret plan",
            tool_calls=[{"name": "Bash", "arguments": BASH_ARGS, "status": None, "result": None}],
        ),
        msg("tool", "2026-03-28T09:01:05Z", TOOL_OUTPUT),
        msg("tool", "2026-03-28T09:01:06Z", TOOL_OUTPUT),
        msg("agent", "2026-03-28T09:02:00Z", "from agent x:\n\nreport"),
        msg("user", "2026-03-28T09:03:00Z", "ok"),
        msg("assistant", "2026-03-28T09:03:30Z", "ab"),
        msg("assistant", "2026-03-28T09:03:31Z", "cd"),
    ]


def document(
    session_id: str,
    messages: list[dict],
    *,
    source: str = "claude",
    doc_type: str | None = None,
    title: str = "Fix the widget pipeline retry bug",
    cwd: str = "/tmp/demo",
    notes: str | None = None,
    created: tuple[str, ...] = (),
    links: tuple[str, ...] = (),
    head_extra: tuple[str, ...] = (),
    tail_extra: tuple[str, ...] = (),
    ending: str = "",
) -> str:
    """An export document: head, metadata, preserved tail, title, extras, notes, conversation."""
    head = [
        "---",
        f"type: {doc_type or source + '-session'}",
        f"session_id: {session_id}",
        f'title: "{title}"',
        *head_extra,
    ]
    data: Any = {"messages": messages, "cwd": cwd, "project": "demo"}
    head += metadata_lines(source, session_id, data)
    head += preserved_tail({"links": list(links)} if links else None)
    lines = [*head, *tail_extra, "---", "", f"# {title}", ""]
    lines += render_artifacts(list(created), [])
    lines += notes_block(notes, "exports")
    rendered: list[Any] = messages
    lines += render_conversation(rendered)
    return "\n".join(lines) + ending


def fixture_document(**kwargs: Any) -> str:
    return document(
        "fixture-0001",
        fixture_messages(),
        notes=NOTES,
        created=("src/widget/retry.py",),
        **kwargs,
    )


class DigestFixture(StoreFixture):
    """A temp store whose exported folders hold hand-built documents."""

    def write_doc(self, name: str, text: str, collection: str = "claude-code-sessions") -> Path:
        folder = self.root / "data" / collection
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(text.encode("utf-8"))
        return path

    def cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = recall.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def digest(self, *args: str, day: str = DAY) -> tuple[int, dict, str, Path]:
        out = self.root / "out" / day
        code, stdout, stderr = self.cli(
            "digest", day, "--all-projects", "--json", "--out", str(out), *args
        )
        return code, (json.loads(stdout) if stdout else {}), stderr, out


class TestWalker(unittest.TestCase):
    def test_walk_counts_every_frame_and_ignores_forged_markers(self):
        text = fixture_document()
        _, frames = locate(text)
        self.assertEqual(len(frames), 10)
        self.assertEqual([f.ordinal for f in frames], list(range(10)))
        self.assertGreater(len(MARKER.findall(text)), len(frames))
        self.assertIn("<!-- cairn:turn 40 -->", frames[3].body)

    def test_anchor_skips_conversation_heading_in_notes(self):
        text = fixture_document()
        anchor, frames = locate(text)
        headings = [m.start() for m in re.finditer(r"(?m)^## Conversation\n", text)]
        self.assertEqual(len(headings), 2)
        self.assertEqual(anchor, headings[1])
        self.assertEqual(frames[0].offset, 17)

    def test_heading_inside_body_is_not_a_frame(self):
        _, frames = locate(fixture_document())
        self.assertIn("\n### C1\n", frames[3].body)
        self.assertEqual(frames[4].role, "tool")
        self.assertEqual([f.role for f in frames].count("assistant"), 4)

    def test_walk_accepts_both_document_endings(self):
        for ending in ("", "\n"):
            with self.subTest(ending=repr(ending)):
                text = fixture_document(ending=ending)
                self.assertTrue(text.endswith("cd\n" + ending))
                self.assertEqual(len(locate(text)[1]), 10)

    def test_crlf_bodies_walk_by_characters(self):
        messages = [
            msg("user", "2026-03-28T12:00:00Z", "first\r\n<!-- cairn:turn 999 -->\r\nend"),
            msg("assistant", "2026-03-28T12:01:00Z", "second"),
        ]
        root = Path(self.enterContext(_tempdir()))
        path = root / "crlf.md"
        path.write_bytes(document("crlf-0001", messages).encode("utf-8"))
        _, frames = locate(read_text(path))
        self.assertEqual(len(frames), 2)
        self.assertIn("\r\n", frames[0].body)
        with self.assertRaises(CorruptExport):
            locate(path.read_text(encoding="utf-8"))

    def test_legacy_text_has_no_frames(self):
        with self.assertRaises(LegacyExport):
            locate("---\ntype: claude-session\n---\n## Conversation\nLegacy text")

    def test_header_only_body_walks(self):
        # A message with no text renders a bare header: the body has no newline.
        text = document("bare-0001", [msg("user", "2026-03-28T12:00:00Z"), msg("assistant", None)])
        self.assertEqual([f.role for f in locate(text)[1]], ["user", "assistant"])


def _tempdir():
    from tempfile import TemporaryDirectory

    return TemporaryDirectory()


class TestProvenance(DigestFixture):
    def setUp(self):
        super().setUp()
        self.doc = self.write_doc("fixture.md", fixture_document())

    def frame(self, *args: str) -> tuple[int, str, str]:
        return self.cli("frame", *args)

    def test_equal_lengths_get_distinct_ordinals_and_hashes(self):
        _, _, _, out = self.digest()
        rows = [
            json.loads(x)
            for x in (out / "claude-fixture0001.frames.jsonl").read_text().splitlines()
        ]
        self.assertEqual(rows[8]["length"], rows[9]["length"])
        self.assertEqual((rows[8]["ordinal"], rows[9]["ordinal"]), (8, 9))
        self.assertNotEqual(rows[8]["sha1_8"], rows[9]["sha1_8"])
        code, stdout, stderr = self.frame("fixture-0001", "9")
        self.assertEqual(code, 0)
        self.assertTrue(stdout.endswith("\n\ncd\n"))
        self.assertRegex(stderr, r"^sha1_8 [0-9a-f]{8}\n$")
        self.assertIn(rows[9]["sha1_8"], stderr)

    def test_offsets_survive_annotation(self):
        _, _, _, out = self.digest()
        sidecar = str(out / "claude-fixture0001.frames.jsonl")
        before = self.frame("fixture-0001", "4", "--frames", sidecar)
        self.run_cli("claude", "note", "Keep this note", "--session-id", "fixture-0001")
        self.assertIn("Keep this note", self.doc.read_text(encoding="utf-8"))
        code, stdout, stderr = self.frame("fixture-0001", "4", "--frames", sidecar)
        self.assertEqual(code, 0)
        self.assertNotIn("changed since the digest", stderr)
        self.assertEqual(stdout, before[1])
        self.assertIn("1 failed in 0.31s", stdout)

    def test_frame_warns_on_rerender(self):
        _, _, _, out = self.digest()
        sidecar = str(out / "claude-fixture0001.frames.jsonl")
        text = self.doc.read_text(encoding="utf-8")
        anchor, frames = locate(text)
        old = frames[4]
        start = anchor + old.offset
        marker_line = f"<!-- cairn:turn {old.length} -->\n"
        self.assertTrue(text.startswith(marker_line, start))
        body = old.body.replace("line 0000", "line 00000", 1)
        new = f"<!-- cairn:turn {old.length + 1} -->\n" + body
        end = start + len(marker_line) + len(old.body)
        self.doc.write_bytes((text[:start] + new + text[end:]).encode("utf-8"))
        code, stdout, stderr = self.frame("fixture-0001", "4", "--frames", sidecar)
        self.assertEqual(code, 0)
        self.assertIn("line 00000", stdout)
        self.assertIn("changed since the digest", stderr)

    def test_frame_ordinal_out_of_range(self):
        code, _, stderr = self.frame("fixture-0001", "10")
        self.assertEqual(code, 1)
        self.assertIn("out of range", stderr)
        # With a sidecar, the range check still comes first.
        _, _, _, out = self.digest()
        sidecar = str(out / "claude-fixture0001.frames.jsonl")
        code, _, stderr = self.frame("fixture-0001", "10", "--frames", sidecar)
        self.assertEqual(code, 1)
        self.assertIn("out of range", stderr)
        self.assertNotIn("no row", stderr)

    def test_legacy_file_is_skipped_not_guessed(self):
        self.write_doc(
            "old.md",
            "---\ntype: claude-session\nsession_id: legacy\ndate: 2026-03-28\nmessages: 12\n"
            "---\n## Conversation\nLegacy text",
        )
        code, data, stderr, out = self.digest()
        self.assertEqual(code, 0)
        self.assertIn("claude:legacy: legacy export without frames", stderr)
        self.assertFalse(any(p.name.startswith("claude-legacy") for p in out.iterdir()))
        self.assertEqual(data["skipped"], [{"ref": "claude:legacy", "reason": "legacy"}])
        self.assertEqual([r["ref"] for r in data["sessions"]], ["claude:fixture-0001"])
        self.assertEqual(self.frame("legacy", "0")[0], 1)

    def test_corrupt_frame_is_skipped_with_exit_0(self):
        messages = [msg("user", "2026-03-28T10:00:00Z", "x"), msg("assistant", None, "y")]
        text = document("broken-0002", messages)
        first = MARKER.search(text, text.index("\n## Conversation\n"))
        assert first is not None
        broken = text.replace(first[0], f"<!-- cairn:turn {int(first[1]) + 5} -->\n", 1)
        self.write_doc("broken.md", broken)
        code, data, stderr, out = self.digest()
        self.assertEqual(code, 0)  # another session was written
        self.assertIn("claude:broken-0002: frame walk failed at character", stderr)
        self.assertEqual([r["ref"] for r in data["sessions"]], ["claude:fixture-0001"])
        self.assertEqual(data["skipped"], [{"ref": "claude:broken-0002", "reason": "corrupt"}])
        self.assertTrue((out / "claude-fixture0001.md").is_file())
        # A skipped session is a known omission: found, but never clean coverage.
        self.assertEqual(data["coverage"]["verdict"], "found")
        self.assertEqual(data["coverage"]["status"], "degraded")
        gap = data["coverage"]["gaps"][0]
        self.assertEqual((gap["kind"], gap["severity"]), ("unreadable-exports", "warn"))
        self.assertEqual(gap["facts"], {"count": 1, "skipped": ["claude:broken-0002"]})
        self.assertIsNone(gap["fix"])

    def test_only_skipped_sessions_is_an_empty_day(self):
        self.doc.unlink()
        self.write_doc(
            "old.md",
            "---\ntype: claude-session\nsession_id: legacy\ndate: 2026-03-28\n"
            "---\n## Conversation\nLegacy text",
        )
        code, data, _, _ = self.digest()
        self.assertEqual(code, 3)
        self.assertEqual(data["sessions"], [])
        self.assertEqual(data["skipped"], [{"ref": "claude:legacy", "reason": "legacy"}])
        self.assertEqual(data["coverage"]["verdict"], "degraded-miss")
        kinds = [(g["kind"], g["fix"]) for g in data["coverage"]["gaps"]]
        self.assertIn(("unreadable-exports", "cairn claude export --all"), kinds)


class TestRoutineExclusion(DigestFixture):
    """Sessions owned by a routine stay out of the next digest (journal-verify spec 4.6)."""

    def setUp(self):
        super().setUp()
        self.write_doc("fixture.md", fixture_document())
        self.messages = [
            msg("user", "2026-03-28T08:00:00Z", "Write the journal."),
            msg("assistant", "2026-03-28T08:01:00Z", "Done."),
        ]

    def owned(self, session_id: str, owner: str) -> None:
        tail = (f'owner: "{owner}"',)
        self.write_doc(f"{session_id}.md", document(session_id, self.messages, tail_extra=tail))

    def test_routine_owner_excluded_by_default(self):
        self.owned("run-0001", "routine:daily")
        code, data, _, out = self.digest()
        self.assertEqual(code, 0)
        self.assertEqual([r["ref"] for r in data["sessions"]], ["claude:fixture-0001"])
        self.assertEqual(data["excluded"], [{"ref": "claude:run-0001", "owner": "routine:daily"}])
        self.assertFalse((out / "claude-run0001.md").exists())

    def test_human_output_names_the_exclusion(self):
        self.owned("run-0001", "routine:daily")
        out = self.root / "human"
        code, stdout, _ = self.cli("digest", DAY, "--all-projects", "--out", str(out))
        self.assertEqual(code, 0)
        self.assertIn(
            "1 routine sessions excluded; pass --include-routine-sessions to keep them\n",
            stdout,
        )

    def test_include_routine_sessions_keeps_them(self):
        self.owned("run-0001", "routine:daily")
        code, data, _, out = self.digest("--include-routine-sessions")
        self.assertEqual(code, 0)
        refs = sorted(r["ref"] for r in data["sessions"])
        self.assertEqual(refs, ["claude:fixture-0001", "claude:run-0001"])
        self.assertEqual(data["excluded"], [])
        self.assertTrue((out / "claude-run0001.md").is_file())

    def test_other_owner_kept(self):
        self.owned("review-0001", "review")
        code, data, _, _ = self.digest()
        self.assertEqual(code, 0)
        self.assertIn("claude:review-0001", [r["ref"] for r in data["sessions"]])
        self.assertEqual(data["excluded"], [])

    def test_subagent_of_routine_session_excluded(self):
        self.owned("run-0001", "routine:daily")
        child = document(
            "agent-0009",
            self.messages,
            doc_type="claude-subagent",
            head_extra=("parent_session_id: run-0001",),
        )
        self.write_doc("child.md", child)
        code, data, _, _ = self.digest("--include-subagents")
        self.assertEqual(code, 0)
        self.assertEqual([r["ref"] for r in data["sessions"]], ["claude:fixture-0001"])
        self.assertEqual(
            data["excluded"],
            [
                {"ref": "claude:agent-0009", "owner": "routine:daily"},
                {"ref": "claude:run-0001", "owner": "routine:daily"},
            ],
        )

    def child(self, parent: str) -> None:
        child = document(
            "agent-0009",
            self.messages,
            doc_type="claude-subagent",
            head_extra=(f"parent_session_id: {parent}",),
        )
        self.write_doc("child.md", child)

    def child_refs(self, *args: str) -> tuple[list[str], list[dict]]:
        code, data, _, _ = self.digest("--include-subagents", *args)
        self.assertEqual(code, 0)
        return [r["ref"] for r in data["sessions"]], data["excluded"]

    def test_subagent_excluded_when_routine_parent_is_out_of_window(self):
        """The routine ran before midnight; its subagent was still active after it."""
        late = [
            msg("user", "2026-03-27T23:50:00Z", "Write the journal."),
            msg("assistant", "2026-03-27T23:51:00Z", "Started."),
        ]
        tail = ('owner: "routine:daily"',)
        self.write_doc("run-0001.md", document("run-0001", late, tail_extra=tail))
        self.child("run-0001")
        refs, excluded = self.child_refs()
        self.assertEqual(refs, ["claude:fixture-0001"])
        self.assertEqual(excluded, [{"ref": "claude:agent-0009", "owner": "routine:daily"}])

    def test_subagent_excluded_when_min_msgs_drops_routine_parent(self):
        self.owned("run-0001", "routine:daily")
        self.child("run-0001")
        refs, excluded = self.child_refs("--min-msgs", "2")
        self.assertEqual(refs, ["claude:fixture-0001"])
        self.assertEqual(excluded, [{"ref": "claude:agent-0009", "owner": "routine:daily"}])

    def test_subagent_excluded_when_routine_parent_is_in_another_project(self):
        tail = ('owner: "routine:daily"',)
        parent = document("run-0001", self.messages, tail_extra=tail, cwd="/tmp/elsewhere")
        self.write_doc("run-0001.md", parent)
        self.child("run-0001")
        out = self.root / "out" / "project"
        code, stdout, _ = self.cli(
            "digest", DAY, "--project", "/tmp/demo", "--include-subagents", "--json",
            "--out", str(out),
        )  # fmt: skip
        self.assertEqual(code, 0)
        data = json.loads(stdout)
        self.assertNotIn("claude:agent-0009", [r["ref"] for r in data["sessions"]])
        self.assertEqual(data["excluded"], [{"ref": "claude:agent-0009", "owner": "routine:daily"}])

    def test_subagent_excluded_by_out_of_window_parent_preamble(self):
        """The parent has no ``owner`` key, only the owner-form preamble, and is not
        digested itself."""
        head = preamble("claude", None, 1, 1800, "routine:daily")
        late = [
            msg("user", "2026-03-27T23:50:00Z", head + "\n\n---\n\nWrite the journal."),
            msg("assistant", "2026-03-27T23:51:00Z", "Started."),
        ]
        self.write_doc("run-0001.md", document("run-0001", late))
        self.child("run-0001")
        refs, excluded = self.child_refs()
        self.assertEqual(refs, ["claude:fixture-0001"])
        self.assertEqual(excluded, [{"ref": "claude:agent-0009", "owner": "routine:daily"}])

    def test_subagent_of_other_out_of_window_parent_kept(self):
        late = [
            msg("user", "2026-03-27T23:50:00Z", "Start the work."),
            msg("assistant", "2026-03-27T23:51:00Z", "Started."),
        ]
        self.write_doc("run-0001.md", document("run-0001", late, tail_extra=('owner: "review"',)))
        self.child("run-0001")
        refs, excluded = self.child_refs()
        self.assertEqual(refs, ["claude:agent-0009", "claude:fixture-0001"])
        self.assertEqual(excluded, [])

    def test_only_routine_sessions_is_empty_day(self):
        (self.root / "data" / "claude-code-sessions" / "fixture.md").unlink()
        self.owned("run-0001", "routine:daily")
        code, data, _, _ = self.digest()
        self.assertEqual(code, 3)
        self.assertEqual(data["sessions"], [])
        self.assertEqual(data["excluded"], [{"ref": "claude:run-0001", "owner": "routine:daily"}])


OVERDUE_NOTES = (
    "## My Notes\n\nmine\n\n## Decisions\n\n"
    "| # | claim | kind | confidence | since | revisit_by | source |\n"
    "|---|-------|------|------------|-------|------------|--------|\n"
    "| 1 | Due on the day | commitment | high | 2026-03-01 | 2026-03-28 |  |\n"
    "| 2 | Due the day after | commitment | high | 2026-03-01 | 2026-03-29 |  |\n"
    "| 3 | ~~Struck~~ superseded by #4 | fact | low | 2026-03-01 | 2026-03-02 |  |\n"
    "| 4 | Undated | fact | low | 2026-03-02 |  | supersedes #3 |"
)


class TestDigestOverdue(DigestFixture):
    def test_digest_json_lists_overdue_relative_to_day_after(self):
        messages = [msg("user", "2026-03-28T10:00:00Z", "x"), msg("assistant", None, "y")]
        self.write_doc("due.md", document("due-0001", messages, notes=OVERDUE_NOTES))
        code, data, _stderr, _out = self.digest()
        self.assertEqual(code, 0)
        self.assertLessEqual({"sessions", "skipped", "overdue"}, set(data))
        self.assertEqual([r["ref"] for r in data["overdue"]], ["claude:due-0001#1"])
        self.assertEqual(data["overdue"][0]["revisit_by"], "2026-03-28")
        self.assertTrue(data["overdue"][0]["overdue"])
        code, data, _stderr, _out = self.digest(day="2026-03-29")
        self.assertEqual([r["number"] for r in data["overdue"]], [1, 2])
        out = self.root / "human"
        code, stdout, _ = self.cli("digest", DAY, "--all-projects", "--out", str(out))
        self.assertEqual(code, 0)
        self.assertIn("Overdue: claude:due-0001#1  revisit_by 2026-03-28  Due on the day\n", stdout)

    def test_overdue_does_not_change_exit_3(self):
        messages = [msg("user", "2026-03-20T10:00:00Z", "x"), msg("assistant", None, "y")]
        self.write_doc("due.md", document("due-0001", messages, notes=OVERDUE_NOTES))
        code, data, _stderr, out = self.digest()
        self.assertEqual((code, data["sessions"]), (3, []))
        self.assertEqual([r["number"] for r in data["overdue"]], [1])
        self.assertEqual(sorted(p.name for p in out.iterdir()), [".cairn-digest"])

    def routine_family(self) -> None:
        """A routine run outside the day and its subagent, each with an overdue row."""
        earlier = [msg("user", "2026-03-20T10:00:00Z", "x"), msg("assistant", None, "y")]
        tail = ('owner: "routine:daily"',)
        self.write_doc(
            "run.md", document("run-0001", earlier, notes=OVERDUE_NOTES, tail_extra=tail)
        )
        child = document(
            "agent-0009",
            earlier,
            doc_type="claude-subagent",
            notes=OVERDUE_NOTES,
            head_extra=("parent_session_id: run-0001",),
        )
        self.write_doc("child.md", child)
        today = [msg("user", "2026-03-28T10:00:00Z", "x"), msg("assistant", None, "y")]
        self.write_doc("due.md", document("due-0001", today, notes=OVERDUE_NOTES))

    def test_routine_sessions_and_subagents_give_no_overdue_rows(self):
        self.routine_family()
        code, data, _stderr, _out = self.digest("--include-subagents")
        self.assertEqual(code, 0)
        self.assertEqual([r["ref"] for r in data["overdue"]], ["claude:due-0001#1"])

    def test_include_routine_sessions_keeps_their_overdue_rows(self):
        self.routine_family()
        code, data, _stderr, _out = self.digest("--include-subagents", "--include-routine-sessions")
        self.assertEqual(code, 0)
        self.assertEqual(
            sorted(r["ref"] for r in data["overdue"]),
            ["claude:agent-0009#1", "claude:due-0001#1", "claude:run-0001#1"],
        )


if __name__ == "__main__":
    unittest.main()
