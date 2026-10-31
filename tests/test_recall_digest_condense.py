"""``cairn recall digest``: window, condensing, cap, roles, peers and output (spec 9.3, 9.4)."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import Any, cast

from cairn import provenance
from cairn.digest import front_matter
from cairn.digest_condense import select
from cairn.frontmatter import parse_frontmatter
from cairn.invoke import preamble
from tests.test_recall_digest import (
    DAY,
    TOOL_OUTPUT,
    DigestFixture,
    document,
    fixture_document,
    msg,
)

FRAME_LINE = re.compile(r"^<!-- f(\d+) (\S+) (\w+)( in_window: false)? -->$", re.MULTILINE)
PREAMBLE = (
    "[cairn invoke] You are codex being invoked by {who} via cairn (round {n}). "
    "Reply budget: 30 minutes of wall-clock time; the run is killed after that."
)


def split(markdown: str) -> tuple[dict, str]:
    """The parsed front matter and the body of a digest file."""
    return parse_frontmatter(markdown), markdown.split("\n---\n", 1)[1].removesuffix("\n")


def blocks(body: str) -> dict[int, str]:
    """Each frame block's condensed body by ordinal."""
    marks = list(FRAME_LINE.finditer(body))
    return {
        int(m[1]): body[m.end() + 1 : nxt.start() - 1 if nxt else len(body)]
        for m, nxt in zip(marks, [*marks[1:], None], strict=True)
    }


def peer_messages(who: str = "claude session claude:fixture-0001", rounds: int = 1) -> list[dict]:
    messages = []
    for n in range(1, rounds + 1):
        hour = 10 + n
        prompt = PREAMBLE.format(who=who, n=n) + f"\n\n---\n\nReview the widget patch, round {n}."
        tool = {"name": "exec_command", "arguments": "make test", "status": None, "result": "ok"}
        messages += [
            msg("user", f"2026-03-28T{hour}:00:00Z", prompt),
            msg("assistant", f"2026-03-28T{hour}:01:00Z", "", tool_calls=[tool]),
            msg("assistant", f"2026-03-28T{hour}:02:00Z", "First look."),
            msg(
                "assistant", f"2026-03-28T{hour}:03:00Z", "Interim note.\nVERDICT: accept - fixture"
            ),
            msg("assistant", f"2026-03-28T{hour}:04:00Z", f"Final answer {n}."),
            msg("assistant", f"2026-03-28T{hour}:05:00Z", "", tool_calls=[tool]),
        ]
    return messages


class TestWindowAndCondensing(DigestFixture):
    def setUp(self):
        super().setUp()
        self.write_doc("fixture.md", fixture_document())
        self.code, self.rows, self.stderr, self.out = self.digest()
        self.fm, self.body = split((self.out / "claude-fixture0001.md").read_text())
        self.blocks = blocks(self.body)

    def test_only_window_frames_in_body(self):
        self.assertEqual(self.code, 0)
        in_window = {int(m[1]) for m in FRAME_LINE.finditer(self.body) if not m[4]}
        self.assertEqual(in_window, set(range(2, 10)))
        self.assertNotIn(1, self.blocks)
        # Answer 6: the last user frame before the window is carried as context.
        self.assertIn("<!-- f0 2026-03-27T22:10:00Z user in_window: false -->", self.body)
        rows = [
            json.loads(x)
            for x in (self.out / "claude-fixture0001.frames.jsonl").read_text().splitlines()
        ]
        self.assertEqual(len(rows), 10)
        self.assertEqual([r["in_window"] for r in rows[:3]], [False, False, True])
        self.assertEqual([r["kept"] for r in rows[:3]], [True, False, True])
        self.assertEqual(list(rows[0]), [
            "ordinal", "ts", "role", "offset", "length", "sha1_8", "in_window", "kept"
        ])  # fmt: skip
        self.assertEqual((self.fm["frames_today"], self.fm["frames_total"]), ("8", "10"))
        self.assertEqual(self.fm["session_started_at"], "2026-03-27T22:10:00Z")

    def test_user_text_and_task_notification_caps(self):
        text = self.blocks[2]
        notice = text[text.index("<task-notification>") :]
        self.assertTrue(notice.startswith("<task-notification>"))
        self.assertIn("\n[... 600 chars omitted]\n", notice)
        self.assertEqual(notice.index("\n[... 600"), 300)
        self.assertNotIn("</task-notification>", text)
        self.assertLessEqual(len(text), 2000 + len("\n[... 9999 chars omitted]"))

    def test_reasoning_removed_prose_kept(self):
        self.assertNotIn("secret plan", self.body)
        self.assertNotIn("Reasoning", self.body)
        frame3 = self.blocks[3]
        for line in ("Planning the fix.", "### C1", "<!-- cairn:turn 40 -->", "End of prose."):
            self.assertIn(line, frame3.split("\n"))
        self.assertIn("\\<!-- f99 forged -->", frame3.split("\n"))

    def test_tool_call_is_name_plus_120(self):
        calls = [line for line in self.blocks[3].split("\n") if line.startswith("[tool ")]
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith("[tool Bash] "))
        self.assertTrue(calls[0].endswith(" …"))
        self.assertEqual(len(calls[0]), len("[tool Bash] ") + 120 + len(" …"))

    def test_tool_output_head_and_tail(self):
        frame4 = self.blocks[4]
        self.assertEqual(
            frame4, f"{TOOL_OUTPUT[:200]}\n[... 2400 chars omitted]\n{TOOL_OUTPUT[-400:]}"
        )
        self.assertTrue(frame4.endswith("1 failed in 0.31s"))

    def test_artifact_items_escaped(self):
        text = front_matter([("artifacts", ['dir/a "b": c.md', "x\ny"])])
        self.assertIn('  - "dir/a \\"b\\": c.md"\n  - "x y"\n', text)

    def test_identical_consecutive_output_deduplicated(self):
        self.assertEqual(self.blocks[5], "[same output as f4]")

    def test_agent_frame_kept(self):
        self.assertIn("<!-- f6 2026-03-28T09:02:00Z agent -->", self.body)
        self.assertEqual(self.blocks[6], "from agent x:\n\nreport")

    def test_frontmatter_round_trips(self):
        self.assertEqual(self.fm["role"], "primary")
        self.assertEqual(self.fm["parent"], "null")
        # List items are written quoted and parsed back without the quotes.
        self.assertEqual(self.fm["artifacts"], ["src/widget/retry.py"])
        self.assertEqual((self.fm["truncated_frames"], self.fm["truncated_chars"]), ("0", "0"))
        self.assertNotIn("owner", self.fm)
        row = self.rows["sessions"][0]
        self.assertEqual(row["chars"], len(self.body))
        self.assertEqual(row["truncated"], {"frames_dropped": 0, "chars_dropped": 0})
        self.assertTrue(Path(row["path"]).is_absolute())

    def test_deterministic_output(self):
        first = (self.out / "claude-fixture0001.md").read_bytes()
        self.digest()
        self.assertEqual((self.out / "claude-fixture0001.md").read_bytes(), first)


class TestSelection(DigestFixture):
    def test_offset_timestamp_is_utc(self):
        messages = [msg("user", "2026-03-29T01:30:00+02:00", "late night")]
        self.write_doc("late.md", document("late-0001", messages))
        _, _, _, out = self.digest()
        self.assertIn(
            "<!-- f0 2026-03-28T23:30:00Z user -->", (out / "claude-late0001.md").read_text()
        )

    def test_multi_day_window_rejected(self):
        code, _, stderr = self.cli("digest", "last 3 days", "--all-projects")
        self.assertEqual(code, 1)
        self.assertIn("one-day window", stderr)

    def test_single_user_message_session_included(self):
        messages = [msg("user", "2026-03-28T08:00:00Z", "fix it"), msg("assistant", None, "done")]
        self.write_doc("short.md", document("short-0002", messages))
        self.assertEqual([r["ref"] for r in self.digest()[1]["sessions"]], ["claude:short-0002"])
        self.assertEqual(self.digest("--min-msgs", "3")[1]["sessions"], [])

    def test_max_chars_keeps_users_then_tail_then_head(self):
        self.write_doc("fixture.md", fixture_document())
        cap = 2700
        code, data, _, out = self.digest("--max-chars", str(cap))
        self.assertEqual(code, 0)
        fm, body = split((out / "claude-fixture0001.md").read_text())
        self.assertLessEqual(len(body), cap)
        kept = set(blocks(body))
        self.assertTrue({0, 2, 7} <= kept)  # every user frame, context included
        others = [3, 4, 5, 6, 8, 9]
        flags = [o in kept for o in others]
        self.assertIn(True, flags)
        self.assertIn(False, flags)
        head = flags.index(False)
        tail = len(flags) - flags[::-1].index(False)
        self.assertFalse(any(flags[head:tail]), "kept non-user frames are a head plus a tail")
        # A "[same output as fN]" line is only kept while frame N is; otherwise the
        # output itself is restored, so the digest never loses both copies.
        references = [
            int(m[1]) for m in re.finditer(r"^\[same output as f(\d+)\]$", body, re.MULTILINE)
        ]
        self.assertTrue(all(target in kept for target in references), (references, kept))
        rows = [
            json.loads(x)
            for x in (out / "claude-fixture0001.frames.jsonl").read_text().splitlines()
        ]
        dropped = [r["ordinal"] for r in rows if r["in_window"] and not r["kept"]]
        self.assertEqual(int(fm["truncated_frames"]), len(dropped))
        self.assertEqual(data["sessions"][0]["truncated"]["frames_dropped"], len(dropped))
        self.assertGreater(int(fm["truncated_chars"]), 0)

    def test_select_passes_in_order(self):
        blocks_ = [(0, True, "u" * 5), (1, False, "a" * 5), (2, False, "b" * 50)]
        blocks_ += [(3, False, "c" * 5), (4, True, "v" * 100), (5, False, "d" * 5)]
        # Users newest first (v does not fit, u does), then the tail until one fails.
        self.assertEqual(select(blocks_, 20), {0, 3, 5})
        self.assertEqual(select(blocks_, 26), {0, 1, 3, 5})
        self.assertEqual(select(blocks_, 10_000), set(range(6)))

    def test_out_dir_safety(self):
        self.write_doc("fixture.md", fixture_document())
        out = self.root / "out" / DAY
        out.mkdir(parents=True)
        (out / "keep.txt").write_text("mine")
        code, _, stderr, _ = self.digest()
        self.assertEqual(code, 1)
        self.assertIn("without .cairn-digest", stderr)
        self.assertEqual([p.name for p in out.iterdir()], ["keep.txt"])
        (out / "keep.txt").unlink()
        (out / ".cairn-digest").write_text("")
        (out / "claude-old.md").write_text("stale")
        self.assertEqual(self.digest()[0], 0)
        names = sorted(p.name for p in out.iterdir())
        self.assertEqual(
            names, [".cairn-digest", "claude-fixture0001.frames.jsonl", "claude-fixture0001.md"]
        )

    def test_owner_copied_when_present(self):
        messages = [msg("user", "2026-03-28T08:00:00Z", "routine run")]
        self.write_doc("own.md", document("own-0001", messages, tail_extra=('owner: "routine"',)))
        self.write_doc("plain.md", document("plain-0001", messages))
        _, _, _, out = self.digest()
        self.assertEqual(split((out / "claude-own0001.md").read_text())[0]["owner"], "routine")
        self.assertNotIn("owner", split((out / "claude-plain0001.md").read_text())[0])

    def test_human_summary(self):
        self.write_doc("fixture.md", fixture_document())
        out = self.root / "human"
        code, stdout, _ = self.cli("digest", DAY, "--all-projects", "--out", str(out))
        self.assertEqual(code, 0)
        self.assertIn("claude:fixture-0001  primary  tool  frames_today=8  chars=", stdout)
        self.assertIn(f"1 sessions written to {out}", stdout)


class TestRolesAndPeers(DigestFixture):
    def peer(self, messages: list[dict], *args: str, **kwargs) -> tuple[dict, dict[int, str], str]:
        self.write_doc("peer.md", document("peer-0001", messages, source="codex", **kwargs),
                       "codex-sessions")  # fmt: skip
        _, _, _, out = self.digest(*args)
        fm, body = split((out / "codex-peer0001.md").read_text())
        return fm, blocks(body), body

    def test_peer_by_preamble_codex(self):
        fm, kept, body = self.peer(peer_messages())
        self.assertEqual((fm["role"], fm["parent"]), ("peer", "claude:fixture-0001"))
        self.assertEqual(sorted(kept), [0, 3, 4])
        self.assertEqual(kept[0], "Review the widget patch, round 1.")  # answer 5: prompt head
        self.assertEqual(kept[3], "VERDICT: accept - fixture")
        self.assertEqual(kept[4], "Final answer 1.")
        self.assertNotIn("First look.", body)

    def test_antigravity_mode_command_before_preamble(self):
        messages = peer_messages()
        messages[0]["text"] = "/plan " + messages[0]["text"]
        fm, kept, _ = self.peer(messages)
        self.assertEqual((fm["role"], fm["parent"]), ("peer", "claude:fixture-0001"))
        self.assertEqual(kept[0], "Review the widget patch, round 1.")

    def test_peer_by_link_wins_over_preamble(self):
        fm, _, _ = self.peer(peer_messages(), links=("parent claude:other-0002",))
        self.assertEqual((fm["role"], fm["parent"]), ("peer", "claude:other-0002"))

    def test_peer_rounds_each_keep_their_answer(self):
        _, kept, _ = self.peer(peer_messages(rounds=2))
        self.assertEqual(kept[4], "Final answer 1.")
        self.assertEqual(kept[10], "Final answer 2.")
        self.assertEqual(kept[6], "Review the widget patch, round 2.")

    def test_operator_peer_is_orphan_primary(self):
        fm, kept, _ = self.peer(peer_messages(who="an operator"))
        self.assertEqual((fm["role"], fm["parent"], fm["orphan_peer"]), ("primary", "null", "true"))
        self.assertEqual(sorted(kept), list(range(6)))
        self.assertEqual(kept[2], "First look.")

    def owner_preamble_messages(self) -> list[dict]:
        head = preamble("codex", None, 1, 1800, "routine:example-daily")
        self.assertIn("being invoked by routine:example-daily via cairn", head)
        messages = peer_messages()
        messages[0] = msg("user", "2026-03-28T11:00:00Z", head + "\n\n---\n\nWrite the journal.")
        return messages

    def test_owner_preamble_is_orphan_with_owner(self):
        """``cairn invoke --owner`` with no caller: the routine's own peer session, kept
        only with ``--include-routine-sessions``."""
        fm, _, _ = self.peer(self.owner_preamble_messages(), "--include-routine-sessions")
        self.assertEqual((fm["role"], fm["parent"], fm["orphan_peer"]), ("primary", "null", "true"))
        self.assertEqual(fm["owner"], "routine:example-daily")

    def test_routine_owner_from_preamble_excluded(self):
        """No ``owner`` key in front matter (exported before the publisher wrote it): the
        preamble alone excludes the session by default."""
        self.write_doc(
            "peer.md",
            document("peer-0001", self.owner_preamble_messages(), source="codex"),
            "codex-sessions",
        )
        code, data, _, out = self.digest()
        self.assertEqual(code, 3)
        self.assertEqual(data["sessions"], [])
        self.assertEqual(
            data["excluded"], [{"ref": "codex:peer-0001", "owner": "routine:example-daily"}]
        )
        self.assertFalse((out / "codex-peer0001.md").exists())

    def test_subagents_skipped_unless_requested(self):
        messages = [msg("user", "2026-03-28T08:00:00Z", "child task"), msg("assistant", None, "ok")]
        child = document(
            "agent-0003",
            messages,
            doc_type="claude-subagent",
            head_extra=("parent_session_id: fixture-0001",),
        )
        self.write_doc("child.md", child)
        self.assertEqual(self.digest()[1]["sessions"], [])
        code, data, _, out = self.digest("--include-subagents")
        self.assertEqual(code, 0)
        row = data["sessions"][0]
        self.assertEqual((row["role"], row["parent"]), ("subagent", "claude:fixture-0001"))
        fm = split((out / "claude-agent0003.md").read_text())[0]
        self.assertEqual((fm["role"], fm["parent"]), ("subagent", "claude:fixture-0001"))

    def test_codex_result_after_midnight_stays_with_call(self):
        records = [
            {"type": "session_meta", "payload": {"id": "midnight-0001", "cwd": self.cwd}},
            {
                "type": "event_msg",
                "timestamp": "2026-03-28T23:59:00Z",
                "payload": {"type": "user_message", "message": "run the suite"},
            },
            {
                "type": "response_item",
                "timestamp": "2026-03-28T23:59:50Z",
                "payload": {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "exec_command",
                    "arguments": '{"cmd": "make test"}',
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-03-29T00:00:10Z",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "MIDNIGHT RESULT",
                },
            },
        ]
        rollout = self.codex / "rollout-midnight-0001.jsonl"
        rollout.write_text("".join(json.dumps(r) + "\n" for r in records))
        self.run_cli("codex", "export", "--all")
        code, data, _, out = self.digest()
        self.assertEqual(code, 0)
        body = split((out / "codex-midnight0001.md").read_text())[1]
        call = blocks(body)[1]
        self.assertTrue(call.startswith('[tool exec_command] {"cmd": "make test"}\n[result]\n'))
        self.assertTrue(call.endswith("MIDNIGHT RESULT"))
        code, data, _, _ = self.digest(day="2026-03-29")
        self.assertEqual((code, data["sessions"]), (3, []))  # no input that day


class TestProvenanceKey(DigestFixture):
    """The ``provenance`` front-matter key in digest files, rows and the filter."""

    def write_peer(self, session_id: str, who: str) -> None:
        name = session_id.replace("-", "")
        self.write_doc(f"{name}.md", document(session_id, peer_messages(who=who), source="codex"),
                       "codex-sessions")  # fmt: skip

    def test_digest_carries_provenance(self):
        self.write_doc("fixture.md", fixture_document())
        self.write_peer("owned-0001", "routine:example-daily")
        code, data, _, out = self.digest("--include-routine-sessions")
        self.assertEqual(code, 0)
        rows = {row["ref"]: row for row in data["sessions"]}
        primary, owned = rows["claude:fixture-0001"], rows["codex:owned-0001"]
        self.assertEqual((primary["provenance"], primary["owner"]), ("tool", None))
        self.assertEqual((owned["provenance"], owned["owner"]), ("agent", "routine:example-daily"))
        self.assertEqual(list(primary)[4:7], ["parent", "provenance", "owner"])
        fm = split((out / "claude-fixture0001.md").read_text())[0]
        self.assertEqual(fm["provenance"], "tool")
        fm = split((out / "codex-owned0001.md").read_text())[0]
        self.assertEqual((fm["provenance"], fm["owner"]), ("agent", "routine:example-daily"))

    def test_text_row_shows_provenance_and_owner(self):
        self.write_peer("owned-0001", "routine:example-daily")
        out = self.root / "text"
        code, stdout, _ = self.cli(
            "digest", DAY, "--all-projects", "--include-routine-sessions", "--out", str(out)
        )
        self.assertEqual(code, 0)
        self.assertIn("codex:owned-0001  primary  agent  frames_today=", stdout)
        self.assertIn("  owner=routine:example-daily", stdout)

    def test_provenance_tool_drops_peers(self):
        self.write_doc("fixture.md", fixture_document())
        self.write_peer("peer-0001", "claude session claude:fixture-0001")
        self.write_peer("owned-0001", "routine:example-daily")
        code, data, _, out = self.digest("--provenance", "tool", "--include-routine-sessions")
        self.assertEqual(code, 0)
        self.assertEqual([row["ref"] for row in data["sessions"]], ["claude:fixture-0001"])
        self.assertEqual(data["skipped"], [])
        self.assertFalse((out / "codex-peer0001.md").exists())
        code, data, _, _ = self.digest("--provenance", "agent", "--include-routine-sessions")
        refs = sorted(row["ref"] for row in data["sessions"])
        self.assertEqual(refs, ["codex:owned-0001", "codex:peer-0001"])

    def test_operator_peer_without_key_is_agent(self):
        self.write_peer("operator-0001", "an operator")
        code, data, _, out = self.digest()
        self.assertEqual(code, 0)
        self.assertEqual(data["sessions"][0]["provenance"], "agent")
        fm = split((out / "codex-operator0001.md").read_text())[0]
        self.assertEqual(
            (fm["role"], fm["orphan_peer"], fm["provenance"]), ("primary", "true", "agent")
        )

    def test_later_preamble_without_key_is_tool(self):
        # Only the first user message with text decides, as at export: a person who
        # pastes a preamble later in the session still drove it, so neither the
        # provenance nor the lineage (role, parent, orphan_peer, owner) reads it.
        for who in ("an operator", "routine:example-daily", "claude session claude:fixture-0001"):
            with self.subTest(who=who):
                messages = [
                    msg("user", "2026-03-28T09:00:00Z", "Look at this prompt"),
                    msg("assistant", "2026-03-28T09:01:00Z", "Reading it."),
                    msg("user", "2026-03-28T09:02:00Z", PREAMBLE.format(who=who, n=1)),
                ]
                self.write_doc("later.md", document("later-0001", messages, source="codex"),
                               "codex-sessions")  # fmt: skip
                code, data, _, out = self.digest("--provenance", "tool")
                self.assertEqual(code, 0)
                (row,) = data["sessions"]
                self.assertEqual(row["ref"], "codex:later-0001")
                self.assertEqual(
                    (row["role"], row["parent"], row["provenance"], row["owner"]),
                    ("primary", None, "tool", None),
                )
                fm = split((out / "codex-later0001.md").read_text())[0]
                self.assertEqual(
                    (fm["role"], fm["parent"], fm["orphan_peer"], fm["provenance"]),
                    ("primary", "null", "false", "tool"),
                )
                self.assertNotIn("owner", fm)

    def test_indented_preamble_reads_as_at_export(self):
        # ``at_export`` strips leading whitespace before the preamble; the digest
        # must read the same document the same way.
        for lead in ("  ", "\n", "\n\n  "):
            with self.subTest(lead=repr(lead)):
                text = lead + PREAMBLE.format(who="an operator", n=1)
                messages = [msg("user", "2026-03-28T09:00:00Z", text)]
                exported = provenance.at_export("codex", "codex-session", cast(Any, messages))
                self.assertEqual(exported, "agent")
                self.write_doc("lead.md", document("lead-0001", messages, source="codex"),
                               "codex-sessions")  # fmt: skip
                code, data, _, _ = self.digest()
                self.assertEqual(code, 0)
                self.assertEqual(data["sessions"][0]["provenance"], "agent")

    def test_stored_key_beats_body_evidence(self):
        for stored, expected in (("tool", "tool"), ("robot", "agent"), ("", "agent")):
            with self.subTest(stored=stored):
                text = document("stored-0001", peer_messages(who="an operator"), source="codex",
                                head_extra=(f"provenance: {stored}".rstrip(),))  # fmt: skip
                self.write_doc("stored.md", text, "codex-sessions")
                code, data, stderr, _ = self.digest()
                self.assertEqual(code, 0)
                self.assertEqual(data["sessions"][0]["provenance"], expected)
                warning = f"provenance '{stored}' is not one of user, agent, tool, imported"
                self.assertEqual(warning in stderr, stored != "tool")

    def test_filter_to_nothing_exits_3(self):
        self.write_doc("fixture.md", fixture_document())
        code, data, _, out = self.digest("--provenance", "imported")
        self.assertEqual(code, 3)
        self.assertEqual((data["sessions"], data["skipped"]), ([], []))
        self.assertEqual(data["coverage"]["verdict"], "clean-miss")
        self.assertEqual(data["excluded"], [])
        self.assertEqual([p.name for p in out.iterdir()], [".cairn-digest"])

    def test_filter_to_nothing_clears_a_populated_directory(self):
        self.write_doc("fixture.md", fixture_document())
        code, _, _, out = self.digest()
        self.assertEqual(code, 0)
        written = sorted(p.name for p in out.iterdir())
        self.assertEqual(
            written, [".cairn-digest", "claude-fixture0001.frames.jsonl", "claude-fixture0001.md"]
        )
        code, data, _, _ = self.digest("--provenance", "agent")
        self.assertEqual((code, data["sessions"], data["skipped"]), (3, [], []))
        self.assertEqual(data["coverage"]["verdict"], "clean-miss")
        self.assertEqual([p.name for p in out.iterdir()], [".cairn-digest"])


if __name__ == "__main__":
    unittest.main()
