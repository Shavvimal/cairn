"""Codex subagent rollouts: replay cut, lineage, child documents and parent index.

Fixtures mirror codex-cli 0.160 rollouts: a forked child writes its own
``session_meta``, then the parent's, then the replayed parent history (stamped at fork
time), then its own turns. Ids are UUIDv7 unless a test says otherwise.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cairn.sources import codex_lineage
from cairn.sources.codex_replay import ReplayCut, uuid7_key
from tests.test_sources_export import _AdapterFixture

PARENT = "01a0b0e9-58e8-79f2-bfc7-31206e1b2f90"
CHILD = "01a1073b-9c35-7033-ac53-2a16b1c36c5e"
SIBLING = "01a1073c-0000-7000-8000-00000000beef"
GRANDCHILD = "01a1074f-1111-7111-8111-000000000001"
PARENT_TURN = "01a0b0ea-189e-7f33-9b0e-a27faf2d6863"
SPAWN_TURN = "01a1073b-1e32-7d12-8519-2f005bf0b5a9"
CHILD_TURN = "01a1073b-9c77-7673-a080-a3995502daf1"
CHILD_TURN_2 = "01a10740-0000-7000-8000-000000000002"
GRANDCHILD_TURN = "01a1074f-2222-7222-8222-000000000003"

PARENT_PROMPT = "Plan the wedding venues"
FORK_TS = "2026-10-04T14:05:09.614Z"
OLD = time.time() - 7200


def _rec(rtype: str, payload: dict, ts: str | None = FORK_TS) -> dict:
    record: dict = {"type": rtype, "payload": payload}
    if ts:
        record["timestamp"] = ts
    return record


def _meta(thread_id: str, ts: str, **extra) -> dict:
    payload = {"id": thread_id, "timestamp": ts, "cwd": "/tmp/nowhere/demo", **extra}
    payload.setdefault("originator", "codex-tui")
    payload.setdefault("cli_version", "0.160.0")
    return _rec("session_meta", payload, ts)


def _spawn(
    parent: str, depth: int, nickname: str, role: str | None = "worker", path: str = "/root/w"
) -> dict:
    return {
        "source": {
            "subagent": {
                "thread_spawn": {
                    "parent_thread_id": parent,
                    "depth": depth,
                    "agent_path": path,
                    "agent_nickname": nickname,
                    "agent_role": role,
                }
            }
        },
        "thread_source": "subagent",
        "forked_from_id": parent,
    }


def _task(turn: str, ts: str | None = FORK_TS) -> dict:
    return _rec("event_msg", {"type": "task_started", "turn_id": turn}, ts)


def _user(text: str, ts: str | None = FORK_TS) -> dict:
    return _rec("event_msg", {"type": "user_message", "message": text}, ts)


def _assistant(text: str, ts: str | None = FORK_TS, phase: str | None = None) -> dict:
    payload = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }
    if phase:
        payload["phase"] = phase
    return _rec("response_item", payload, ts)


def _role_marker(ts: str = FORK_TS) -> dict:
    payload = {
        "type": "message",
        "role": "developer",
        "content": [{"type": "input_text", "text": "<multi_agent_role>You are an agent"}],
    }
    return _rec("response_item", payload, ts)


def _settings(thread_id: str | None, ts: str = FORK_TS) -> dict:
    payload: dict = {"type": "thread_settings_applied", "thread_settings": {}}
    if thread_id:
        payload["thread_id"] = thread_id
    return _rec("event_msg", payload, ts)


def _parent_history() -> list[dict]:
    """The parent's own records (F6 body, also replayed inside a forked child)."""
    return [
        _task(PARENT_TURN, "2026-10-04T13:00:00Z"),
        _user(PARENT_PROMPT, "2026-10-04T13:00:01Z"),
        _rec(
            "response_item",
            {
                "type": "function_call",
                "name": "spawn_agent",
                "arguments": "gAAAAABspawn",
                "call_id": "call_spawn",
            },
            "2026-10-04T13:00:02Z",
        ),
        _rec(
            "response_item",
            {"type": "function_call_output", "call_id": "call_spawn", "output": "spawned"},
            "2026-10-04T13:00:03Z",
        ),
    ]


def _parent_rollout() -> list[dict]:
    """F6: the parent thread."""
    return [_meta(PARENT, "2026-10-04T13:00:00Z", source="cli"), *_parent_history()]


def _child_own(nickname: str = "Bohr") -> list[dict]:
    """The forked child's own turn: context, agent message, custom tool, final answer."""
    return [
        _task(CHILD_TURN, "2026-10-04T14:05:10Z"),
        _rec(
            "turn_context",
            {
                "turn_id": CHILD_TURN,
                "model": "gpt-6-astra",
                "effort": "high",
                "sandbox_policy": {"type": "read-only"},
                "approval_policy": "never",
            },
            "2026-10-04T14:05:10Z",
        ),
        _rec(
            "response_item",
            {
                "type": "agent_message",
                "author": "/root",
                "recipient": "/root/w",
                "content": [
                    {"type": "input_text", "text": "Message Type: NEW_TASK\nSender: /root"},
                    {"type": "encrypted_content", "encrypted_content": "gAAAAABsecret"},
                ],
            },
            "2026-10-04T14:05:11Z",
        ),
        _rec(
            "response_item",
            {
                "type": "custom_tool_call",
                "name": "exec",
                "input": "const res = await tools.exec_command({cmd: 'pwd'})",
                "call_id": "call_exec",
            },
            "2026-10-04T14:05:12Z",
        ),
        _rec(
            "response_item",
            {
                "type": "custom_tool_call_output",
                "call_id": "call_exec",
                "output": [
                    {"type": "input_text", "text": "Script completed"},
                    {"type": "input_text", "text": "/tmp/nowhere/demo"},
                ],
            },
            "2026-10-04T14:05:13Z",
        ),
        _assistant(f"{nickname} found three venues", "2026-10-04T14:05:20Z", "final_answer"),
    ]


def _child_rollout(nickname: str = "Bohr") -> list[dict]:
    """F5: a forked child with the replayed parent history before its own turn."""
    return [
        _meta(CHILD, FORK_TS, **_spawn(PARENT, 1, nickname)),
        _meta(PARENT, "2026-10-04T13:00:00Z", source="cli"),
        *[{**r, "timestamp": FORK_TS} for r in _parent_history()],
        _task(SPAWN_TURN),
        _role_marker(),
        _settings(CHILD),
        *_child_own(nickname),
    ]


class CodexSubagentFixture(_AdapterFixture):
    module_name = "cairn.sources.codex"

    def setUp(self):
        super().setUp()
        self._cache_patch = patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")})
        self._cache_patch.start()
        self.addCleanup(self._cache_patch.stop)
        self.store = self.root / "codex-store"
        self.store.mkdir()
        self.mod = self._activate({"codex-sessions": str(self.store)})
        self.out = self.root / ".context" / "codex-sessions"

    def write(self, thread_id: str, records: list[dict], mtime: float | None = OLD) -> Path:
        folder = self.store / "2026" / "10" / "04"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"rollout-2026-10-04T15-05-09-{thread_id}.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def source(self):
        return self.mod.CodexSource()

    def doc(self, thread_id: str) -> Path:
        found = self.source().find_file(thread_id)
        self.assertIsNotNone(found, thread_id)
        assert found is not None
        return found


class TestReplayCut(unittest.TestCase):
    def test_uuid7_key_orders_by_timestamp_and_rejects_other_versions(self):
        spawn, child, child_turn = uuid7_key(SPAWN_TURN), uuid7_key(CHILD), uuid7_key(CHILD_TURN)
        assert spawn is not None and child is not None and child_turn is not None
        self.assertLess(spawn, child)
        self.assertGreater(child_turn, child)
        self.assertIsNone(uuid7_key("6f1c2d3e-0000-4000-8000-000000000000"))  # v4
        self.assertIsNone(uuid7_key("turn-1"))
        self.assertIsNone(uuid7_key(None))

    def test_uuid7_rule_cuts_at_the_child_turn(self):
        cut = ReplayCut.start(CHILD)
        cut = cut.observe("event_msg", {"type": "task_started", "turn_id": SPAWN_TURN})
        self.assertFalse(cut.ended)
        cut = cut.observe("event_msg", {"type": "task_started", "turn_id": CHILD_TURN})
        self.assertEqual(cut.rule, "uuid7-turn")

    def test_exact_marker_wins_after_an_ancestor_uuid7_turn(self):
        # Regression: a UUIDv7 ancestor turn used to lock out the marker fallback.
        cut = ReplayCut.start(CHILD)
        cut = cut.observe("event_msg", {"type": "task_started", "turn_id": SPAWN_TURN})
        cut = cut.observe("event_msg", {"type": "thread_settings_applied", "thread_id": CHILD})
        self.assertEqual(cut.rule, "thread-settings")

    def test_marker_rule_counts_one_group_per_fork_level(self):
        marker = _role_marker()["payload"]
        cut = ReplayCut.start("legacy-child").with_meta()  # nested: two marker groups
        cut = cut.observe("response_item", marker)
        cut = cut.observe("event_msg", {"type": "thread_settings_applied"})  # same group
        self.assertFalse(cut.ended)
        cut = cut.observe("response_item", {"type": "message", "role": "assistant"})
        cut = cut.observe("response_item", marker)
        self.assertEqual(cut.rule, "role-marker")

    def test_thread_settings_for_the_child_cuts_exactly(self):
        cut = ReplayCut.start("legacy-child")
        cut = cut.observe("event_msg", {"type": "thread_settings_applied", "thread_id": "x"})
        self.assertFalse(cut.ended)
        cut = cut.observe(
            "event_msg", {"type": "thread_settings_applied", "thread_id": "legacy-child"}
        )
        self.assertEqual(cut.rule, "thread-settings")


class TestParseChild(CodexSubagentFixture):
    def test_forked_child_parses_as_itself_after_the_replay(self):
        data = self.mod.parse_rollout(self.write(CHILD, _child_rollout()))
        self.assertEqual(data["session_id"], CHILD)
        self.assertEqual(data["parent_session_id"], PARENT)
        self.assertEqual(data["spawn_depth"], 1)
        self.assertEqual(data["agent_type"], "worker")
        self.assertEqual(data["agent_nickname"], "Bohr")
        self.assertEqual(data["agent_path"], "/root/w")
        self.assertEqual(data["forked_from_id"], PARENT)
        self.assertEqual(data["thread_source"], "subagent")
        self.assertEqual(data["originator"], "codex-tui")
        self.assertEqual(data["title"], "worker Bohr: /root/w")
        self.assertTrue(data["finished"])
        self.assertEqual(data["first_timestamp"], FORK_TS)
        self.assertEqual(data["date"], "2026-10-04")
        self.assertEqual((data["effort"], data["sandbox"]), ("high", "read-only"))
        self.assertEqual(data["approval_policy"], "never")
        self.assertEqual(data["user_messages"], [])
        texts = json.dumps(data["messages"])
        self.assertNotIn(PARENT_PROMPT, texts)
        self.assertNotIn("spawn_agent", texts)
        self.assertNotIn("replay_unresolved", data)

    def test_custom_tool_call_and_agent_message(self):
        data = self.mod.parse_rollout(self.write(CHILD, _child_rollout()))
        agent = [m for m in data["messages"] if m["role"] == "agent"]
        self.assertEqual(len(agent), 1)
        self.assertEqual(
            agent[0]["text"],
            "from /root to /root/w\n\nMessage Type: NEW_TASK\nSender: /root\n[encrypted]",
        )
        calls = [c for m in data["messages"] for c in m.get("tool_calls", [])]
        self.assertEqual(calls[0]["name"], "exec")
        self.assertIn("exec_command", calls[0]["arguments"])
        self.assertEqual(calls[0]["result"], "Script completed\n/tmp/nowhere/demo")

        self.source().export_rollouts(quiet=True)
        text = self.doc(CHILD).read_text(encoding="utf-8")
        self.assertIn('<tool name="exec"', text)
        self.assertIn("exec_command", text)
        self.assertIn("/tmp/nowhere/demo", text)
        self.assertIn("### Agent", text)
        self.assertIn("[encrypted]", text)

    def test_unmatched_custom_output_becomes_a_tool_turn(self):
        records = [
            _meta(PARENT, "2026-10-04T13:00:00Z"),
            _rec(
                "response_item",
                {"type": "custom_tool_call_output", "call_id": "orphan", "output": "late"},
            ),
        ]
        data = self.mod.parse_rollout(self.write(PARENT, records))
        self.assertEqual(data["messages"][-1]["role"], "tool")
        self.assertEqual(data["messages"][-1]["tool_calls"][0]["result"], "late")

    def test_nested_fork_with_three_metas(self):
        records = [
            _meta(GRANDCHILD, FORK_TS, **_spawn(CHILD, 2, "Curie", "explorer", "/root/w/c")),
            _meta(CHILD, FORK_TS, **_spawn(PARENT, 1, "Bohr")),
            _meta(PARENT, "2026-10-04T13:00:00Z"),
            *[{**r, "timestamp": FORK_TS} for r in _parent_history()],
            _task(SPAWN_TURN),
            _role_marker(),
            _settings(CHILD),
            *[{**r, "timestamp": FORK_TS} for r in _child_own()],
            _task(GRANDCHILD_TURN, "2026-10-04T14:30:00Z"),
            _assistant("Curie checked the dates", "2026-10-04T14:30:05Z", "final_answer"),
        ]
        data = self.mod.parse_rollout(self.write(GRANDCHILD, records))
        self.assertEqual(data["session_id"], GRANDCHILD)
        self.assertEqual((data["parent_session_id"], data["spawn_depth"]), (CHILD, 2))
        texts = json.dumps(data["messages"])
        self.assertIn("Curie checked the dates", texts)
        self.assertNotIn("Bohr found three venues", texts)
        self.assertNotIn(PARENT_PROMPT, texts)
        self.assertEqual(data["title"], "explorer Curie: /root/w/c")

    def test_legacy_ids_fall_back_to_the_thread_settings_marker(self):
        records = [
            _meta("legacy-child", FORK_TS, **_spawn("legacy-parent", 1, "Bohr")),
            _meta("legacy-parent", "2026-10-04T13:00:00Z"),
            _task("turn-a"),
            _user(PARENT_PROMPT),
            _settings("legacy-child"),
            _task("turn-b", "2026-10-04T14:06:00Z"),
            _assistant("legacy child answer", "2026-10-04T14:06:01Z", "final_answer"),
        ]
        data = self.mod.parse_rollout(self.write("legacy-child", records))
        self.assertEqual(data["session_id"], "legacy-child")
        texts = json.dumps(data["messages"])
        self.assertNotIn(PARENT_PROMPT, texts)
        self.assertIn("legacy child answer", texts)
        self.assertNotIn("replay_unresolved", data)

    def _unresolved_records(self) -> list[dict]:
        return [
            _meta(CHILD, FORK_TS, **_spawn(PARENT, 1, "Bohr")),
            _meta(PARENT, "2026-10-04T13:00:00Z"),
            *[{**r, "timestamp": FORK_TS} for r in _parent_history()],
            _task(SPAWN_TURN),
        ]

    def test_unresolved_replay_exports_whole_file_with_a_warning(self):
        path = self.write(CHILD, self._unresolved_records(), mtime=OLD)
        err = io.StringIO()
        with redirect_stderr(err):
            data = self.mod.parse_rollout(path)
            self.source().export_rollouts(quiet=True)
        self.assertIn("replayed parent history has no recognisable end", err.getvalue())
        self.assertIn("codex-cli 0.160", err.getvalue())
        self.assertTrue(data["replay_unresolved"])
        self.assertEqual(data["session_id"], CHILD)
        self.assertIn(PARENT_PROMPT, json.dumps(data["messages"]))
        text = self.doc(CHILD).read_text(encoding="utf-8")
        self.assertIn("replay_unresolved: true", text)
        self.assertIn("type: codex-subagent", text)

    def test_live_child_without_own_records_is_skipped_silently(self):
        self.write(PARENT, _parent_rollout())
        path = self.write(CHILD, self._unresolved_records(), mtime=None)
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertIsNone(self.mod.parse_rollout(path))
            exported = self.source().export_rollouts(quiet=True)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(exported, 1)
        self.assertIsNone(self.source().find_file(CHILD))

    def test_torn_final_line_is_ignored(self):
        path = self.write(CHILD, _child_rollout())
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('{"type": "response_item", "payload": {"type": "mess')
        data = self.mod.parse_rollout(path)
        self.assertEqual(data["session_id"], CHILD)
        self.assertTrue(data["finished"])

    def test_headless_exec_rollout_exports_context_head_fields(self):
        sid = "01a1321e-f850-7460-b60d-083b6c71e175"
        records = [
            _meta(sid, "2026-10-12T21:57:33Z", originator="codex_exec", source="exec"),
            _task("01a1321e-f900-7000-8000-000000000001", "2026-10-12T21:57:34Z"),
            _rec(
                "turn_context",
                {
                    "turn_id": "t",
                    "effort": "low",
                    "sandbox_policy": "workspace-write",
                    "approval_policy": "never",
                },
                "2026-10-12T21:57:34Z",
            ),
            _rec(
                "response_item",
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "<environment_context>x"},
                        {"type": "input_text", "text": "Reply with exactly PONG"},
                    ],
                    "internal_chat_message_metadata_passthrough": {
                        "content_item_kinds": ["environments.environment_context", "user.text"]
                    },
                },
                "2026-10-12T21:57:34Z",
            ),
            _assistant("PONG", "2026-10-12T21:57:40Z", "final_answer"),
        ]
        self.write(sid, records)
        self.source().export_rollouts(quiet=True)
        text = self.doc(sid).read_text(encoding="utf-8")
        for line in (
            "type: codex-session",
            "originator: codex_exec",
            "effort: low",
            "sandbox: workspace-write",
            "approval_policy: never",
            'title: "Reply with exactly PONG"',
        ):
            self.assertIn(line + "\n", text)
        self.assertNotIn("parent_session_id", text)
        self.assertIn("status: active", text)


class TestReplayRegressions(CodexSubagentFixture):
    def test_mixed_ids_uuid7_ancestor_turn_then_legacy_child_turn(self):
        records = [
            _meta(CHILD, FORK_TS, **_spawn(PARENT, 1, "Bohr")),
            _meta(PARENT, "2026-10-04T13:00:00Z"),
            *[{**r, "timestamp": FORK_TS} for r in _parent_history()],
            _task(SPAWN_TURN),
            _settings(CHILD),
            _task("legacy-turn", "2026-10-04T14:06:00Z"),
            _assistant("mixed child answer", "2026-10-04T14:06:01Z", "final_answer"),
        ]
        err = io.StringIO()
        with redirect_stderr(err):
            data = self.mod.parse_rollout(self.write(CHILD, records))
        self.assertEqual(err.getvalue(), "")
        self.assertNotIn("replay_unresolved", data)
        texts = json.dumps(data["messages"])
        self.assertNotIn(PARENT_PROMPT, texts)
        self.assertIn("mixed child answer", texts)
        self.assertTrue(data["finished"])


def _plain_child(thread_id: str, parent: str, depth: int, nickname: str, turn: str) -> list:
    """A non-forked subagent: one meta, then its own turn."""
    return [
        _meta(thread_id, FORK_TS, **_spawn(parent, depth, nickname, path=f"/root/{nickname}")),
        _task(turn, "2026-10-04T14:20:00Z"),
        _assistant(f"{nickname} done", "2026-10-04T14:20:05Z", "final_answer"),
    ]


class TestFamilyRegressions(CodexSubagentFixture):
    def _nested_family(self) -> None:
        """P -> {A, B}, B -> G."""
        self.write(PARENT, _parent_rollout())
        self.write(CHILD, _child_rollout())
        self.write(SIBLING, _plain_child(SIBLING, PARENT, 1, "Noether", CHILD_TURN_2))
        self.write(GRANDCHILD, _plain_child(GRANDCHILD, SIBLING, 2, "Curie", GRANDCHILD_TURN))

    def test_targeted_exports_keep_nested_indexes(self):
        self._nested_family()
        self.assertEqual(self.source().export_rollouts(quiet=True), 4)
        sibling_doc = self.doc(SIBLING)
        self.assertEqual(self.doc(GRANDCHILD).parent, sibling_doc.with_suffix(""))
        for target in (CHILD, PARENT, SIBLING, GRANDCHILD):
            with self.subTest(target=target):
                self.mod.CodexSource().export_target(target, None)
                self.assertIn(f"`{GRANDCHILD}`", sibling_doc.read_text(encoding="utf-8"))
                parent = self.doc(PARENT).read_text(encoding="utf-8")
                self.assertIn(f"`{CHILD}`", parent)
                self.assertIn(f"`{SIBLING}`", parent)

    def test_targeted_grandchild_refreshes_every_ancestor(self):
        self.write(PARENT, _parent_rollout())
        self.write(SIBLING, _plain_child(SIBLING, PARENT, 1, "Noether", CHILD_TURN_2))
        self.source().export_rollouts(quiet=True)
        self.write(GRANDCHILD, _plain_child(GRANDCHILD, SIBLING, 2, "Curie", GRANDCHILD_TURN))
        self.mod.CodexSource().export_target(GRANDCHILD, None)
        self.assertIn(f"`{GRANDCHILD}`", self.doc(SIBLING).read_text(encoding="utf-8"))
        self.assertEqual(self.doc(GRANDCHILD).parent, self.doc(SIBLING).with_suffix(""))

    def test_short_id_collision_links_each_child_to_its_own_file(self):
        first = "01a1073b-aaaa-7000-8000-cafe00000001"
        second = "01a1073b-bbbb-7000-8000-cafe00000002"
        source = self.source()
        self.assertEqual(source.short_id(first), source.short_id(second))
        self.write(PARENT, _parent_rollout())
        self.write(
            first, _plain_child(first, PARENT, 1, "Ada", "01a10750-0000-7000-8000-000000000001")
        )
        self.write(
            second, _plain_child(second, PARENT, 1, "Bea", "01a10750-0000-7000-8000-000000000002")
        )
        self.assertEqual(source.export_rollouts(quiet=True), 3)
        parent_doc = self.doc(PARENT)
        parent = parent_doc.read_text(encoding="utf-8")
        paths = {self.doc(first), self.doc(second)}
        self.assertEqual(len(paths), 2)
        for child_id in (first, second):
            line = next(ln for ln in parent.splitlines() if f"`{child_id}`" in ln)
            rel = line.split("](", 1)[1].split(")", 1)[0]
            target = (parent_doc.parent / rel).resolve()
            self.assertEqual(target, self.doc(child_id).resolve())
            self.assertIn(f"session_id: {child_id}\n", target.read_text(encoding="utf-8"))
        stamps = {p: p.stat().st_mtime_ns for p in (parent_doc, *paths)}
        self.assertEqual(self.mod.CodexSource().export_rollouts(quiet=True), 0)
        self.assertEqual({p: p.stat().st_mtime_ns for p in stamps}, stamps)

    def test_targeted_export_fails_loud_on_an_unreadable_family_member(self):
        self.write(PARENT, _parent_rollout())
        self.write(CHILD, _child_rollout())
        bad = self.write(SIBLING, _plain_child(SIBLING, PARENT, 1, "Noether", CHILD_TURN_2))
        with open(bad, "ab") as handle:
            handle.write(b"\xff\xfe broken\n")
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaisesRegex(ValueError, "incomplete") as caught:
            self.source().export_target(PARENT, None)
        self.assertIn(str(bad), str(caught.exception))
        self.assertIn(f"Error parsing {bad}", err.getvalue())
        parent = self.doc(PARENT).read_text(encoding="utf-8")
        self.assertIn(f"`{CHILD}`", parent)
        self.assertIsNotNone(self.source().find_file(CHILD))


class TestExportFamily(CodexSubagentFixture):
    def test_parent_and_child_export_as_two_documents(self):
        self.write(PARENT, _parent_rollout())
        self.write(CHILD, _child_rollout())
        self.assertEqual(self.source().export_rollouts(quiet=True), 2)

        parent_doc = self.doc(PARENT)
        child_doc = self.doc(CHILD)
        short = self.source().short_id(CHILD)
        self.assertEqual(child_doc, parent_doc.with_suffix("") / f"agent-{short}.md")
        child = child_doc.read_text(encoding="utf-8")
        for line in (
            "type: codex-subagent",
            f"session_id: {CHILD}",
            f"parent_session_id: {PARENT}",
            "spawn_depth: 1",
            "agent_type: worker",
            'agent_nickname: "Bohr"',
            'agent_path: "/root/w"',
            f"forked_from_id: {PARENT}",
            "thread_source: subagent",
            'title: "worker Bohr: /root/w"',
            "status: done",
        ):
            self.assertIn(line + "\n", child)
        self.assertNotIn(PARENT_PROMPT, child.split("## Conversation", 1)[1])

        parent = parent_doc.read_text(encoding="utf-8")
        self.assertIn("type: codex-session\n", parent)
        self.assertIn("## Subagents\n", parent)
        self.assertIn(
            f"- [worker Bohr: /root/w](./{parent_doc.stem}/agent-{short}.md)"
            f" - `{CHILD}`, worker, finished\n",
            parent,
        )
        record = self.source().find_record(CHILD)
        self.assertTrue(record.is_subagent)
        self.assertEqual(record.session_type, "codex-subagent")
        self.assertNotIn(child_doc, [p for p, _ in self.source().all_sessions()])

        stamps = {p: p.stat().st_mtime_ns for p in (parent_doc, child_doc)}
        self.assertEqual(self.mod.CodexSource().export_rollouts(quiet=True), 0)
        self.assertEqual({p: p.stat().st_mtime_ns for p in stamps}, stamps)

    def test_new_child_under_unchanged_parent_rerenders_the_parent(self):
        self.write(PARENT, _parent_rollout())
        self.write(CHILD, _child_rollout())
        self.source().export_rollouts(quiet=True)
        sibling = [
            _meta(SIBLING, FORK_TS, **_spawn(PARENT, 1, "Noether")),
            _meta(PARENT, "2026-10-04T13:00:00Z", source="cli"),
            *[{**r, "timestamp": FORK_TS} for r in _parent_history()],
            _task(SPAWN_TURN),
            _settings(SIBLING),
            _task(CHILD_TURN_2, "2026-10-04T14:10:00Z"),
            _assistant("still working", "2026-10-04T14:10:05Z"),
        ]
        self.write(SIBLING, sibling)
        self.assertEqual(self.mod.CodexSource().export_rollouts(quiet=True), 2)
        parent = self.doc(PARENT).read_text(encoding="utf-8")
        self.assertIn(f"`{SIBLING}`, worker, running", parent)
        self.assertIn(f"`{CHILD}`, worker, finished", parent)

    def test_child_title_change_rerenders_the_parent(self):
        self.write(PARENT, _parent_rollout())
        self.write(CHILD, _child_rollout())
        self.source().export_rollouts(quiet=True)
        self.write(CHILD, _child_rollout("Meitner"), mtime=OLD + 60)
        self.assertEqual(self.mod.CodexSource().export_rollouts(quiet=True), 2)
        self.assertIn("[worker Meitner: /root/w]", self.doc(PARENT).read_text(encoding="utf-8"))

    def test_window_selects_an_old_parent_through_a_new_child(self):
        self.write(PARENT, _parent_rollout(), mtime=OLD)
        self.write(CHILD, _child_rollout(), mtime=time.time())
        cutoff = time.time() - 600
        self.assertEqual(self.source().export_rollouts(date_filter=cutoff, quiet=True), 2)
        self.assertIn("## Subagents", self.doc(PARENT).read_text(encoding="utf-8"))

    def test_targeted_parent_export_renders_children(self):
        self.write(PARENT, _parent_rollout())
        self.write(CHILD, _child_rollout())
        result = self.source().export_target(PARENT, None)
        self.assertEqual(result, self.doc(PARENT))
        self.assertIn(f"`{CHILD}`", result.read_text(encoding="utf-8"))
        self.assertTrue(self.doc(CHILD).is_relative_to(result.with_suffix("")))

    def test_targeted_child_export_refreshes_the_parent_index(self):
        self.write(PARENT, _parent_rollout())
        self.source().export_rollouts(quiet=True)
        self.assertNotIn("## Subagents", self.doc(PARENT).read_text(encoding="utf-8"))
        child_path = self.write(CHILD, _child_rollout())
        result = self.mod.CodexSource().export_target(CHILD, str(child_path))
        self.assertEqual(result, self.doc(CHILD))
        self.assertEqual(result.parent, self.doc(PARENT).with_suffix(""))
        self.assertIn(f"`{CHILD}`", self.doc(PARENT).read_text(encoding="utf-8"))

    def test_targeted_child_export_rejects_a_mismatched_transcript(self):
        parent_path = self.write(PARENT, _parent_rollout())
        self.write(CHILD, _child_rollout())
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.source().export_target(CHILD, str(parent_path))

    def test_unreadable_rollout_is_reported_and_others_still_export(self):
        self.write(PARENT, _parent_rollout())
        bad = self.store / "2026" / "10" / "04" / "rollout-bad.jsonl"
        bad.write_bytes(b'{"type": "session_meta"}\n\xff\xfe broken\n')
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = self.source().run_export(argparse.Namespace(all=True, since=None, quiet=True))
        self.assertEqual(code, 1)
        self.assertIn(f"Error parsing {bad}", err.getvalue())
        self.assertIsNotNone(self.source().find_file(PARENT))


class TestLineage(CodexSubagentFixture):
    def test_cache_cold_warm_and_invalidated(self):
        self.write(PARENT, _parent_rollout())
        child = self.write(CHILD, _child_rollout())
        torn = self.store / "2026" / "10" / "04" / "rollout-torn.jsonl"
        torn.write_text('{"type": "session_meta", "payl', encoding="utf-8")
        real = codex_lineage.read_first_meta
        with patch.object(codex_lineage, "read_first_meta", side_effect=real) as reader:
            lineage = codex_lineage.rollout_lineage(self.store)
            self.assertEqual(reader.call_count, 3)
            self.assertEqual(lineage.children_of(PARENT), [child])
            self.assertEqual(lineage.parent_of(CHILD), PARENT)
            self.assertIsNone(lineage.parent_of(PARENT))
            self.assertIsNone(lineage.meta_of(torn))
            meta = lineage.meta_of(child)
            assert meta is not None
            self.assertEqual(
                (meta.id, meta.parent, meta.role, meta.nickname, meta.path),
                (CHILD, PARENT, "worker", "Bohr", "/root/w"),
            )
            self.assertEqual((meta.originator, meta.thread_source), ("codex-tui", "subagent"))

            reader.reset_mock()
            warm = codex_lineage.rollout_lineage(self.store)
            self.assertEqual(reader.call_count, 0)
            self.assertEqual(warm.children_of(PARENT), [child])

            os.utime(child, (OLD + 5, OLD + 5))
            codex_lineage.rollout_lineage(self.store)
            self.assertEqual(reader.call_count, 1)

            reader.reset_mock()
            codex_lineage.lineage_cache_path(self.store).write_text("{not json")
            codex_lineage.rollout_lineage(self.store)
            self.assertEqual(reader.call_count, 3)

    def test_bad_bytes_after_the_first_line_keep_the_meta_visible(self):
        child = self.write(CHILD, _child_rollout())
        with open(child, "ab") as handle:
            handle.write(b"\xff\xfe broken\n")
        lineage = codex_lineage.rollout_lineage(self.store)
        self.assertEqual(lineage.parent_of(CHILD), PARENT)


if __name__ == "__main__":
    unittest.main()
