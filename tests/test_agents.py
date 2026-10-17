"""``cairn agents list|wait`` over Claude subagents and peer runs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from cairn.agents import classify_claude_child, task_notifications
from tests.test_shared_store import StoreFixture

PARENT = "p0000000-aaaa-4bbb-8ccc-000000000001"
PEER = "019a0000-0000-7000-8000-00000000peer"
HINTS = (
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
    "CAIRN_HARNESS",
)


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _write(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


class AgentsFixture(StoreFixture):
    def setUp(self):
        super().setUp()
        for key in HINTS:
            self.env.pop(key, None)
            os.environ.pop(key, None)
        self.now = time.time()
        self.project = self.claude / "-demo"
        self.subagents = self.project / PARENT / "subagents"
        old = self.now - 300
        recent = self.now - 5
        self.child("f1", {"finished": True, "description": "finished one"}, old)
        self.child("s1", {"stoppedByUser": True, "description": "stopped one"}, old)
        self.child("n1", {"finished": False, "description": "notified one"}, old)
        self.child("r1", {"finished": False, "description": "resumed one"}, recent)
        self.child("q1", {"finished": False, "description": "silent one"}, self.now - 1000)
        notify = self.now - 100  # newer than n1's last record, older than r1's
        self.parent(
            [
                self.notification("n1", notify, "completed"),
                self.notification("r1", notify, "completed"),
            ]
        )

    def child(self, agent_id: str, meta: dict, last: float) -> None:
        path = _write(
            self.subagents / f"agent-{agent_id}.jsonl",
            [
                {
                    "type": "user",
                    "agentId": agent_id,
                    "sessionId": PARENT,
                    "timestamp": _iso(last - 60),
                    "message": {"role": "user", "content": "go"},
                },
                {
                    "type": "assistant",
                    "agentId": agent_id,
                    "sessionId": PARENT,
                    "timestamp": _iso(last),
                    "message": {"role": "assistant", "content": [], "stop_reason": "tool_use"},
                },
            ],
        )
        (self.subagents / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta))
        os.utime(path, (last, last))

    @staticmethod
    def notification(agent_id: str, when: float, status: str) -> dict:
        return {
            "type": "user",
            "sessionId": PARENT,
            "timestamp": _iso(when),
            "origin": {"kind": "task-notification"},
            "message": {
                "role": "user",
                "content": f"<task-notification><task-id>{agent_id}</task-id>"
                f"<status>{status}</status><result>ok</result></task-notification>",
            },
        }

    def parent(self, extra: list[dict]) -> None:
        records = [
            {
                "type": "user",
                "sessionId": PARENT,
                "cwd": self.cwd,
                "timestamp": _iso(self.now - 2000),
                "message": {"role": "user", "content": "fan out"},
            },
            *extra,
        ]
        _write(self.project / f"{PARENT}.jsonl", records)

    def peer_run(self, outcome: str = "succeeded") -> None:
        run_dir = self.root / "data/logs/invoke/codex" / PEER
        run_dir.mkdir(parents=True)
        state = {
            "peer": "codex",
            "thread_id": PEER,
            "cwd": self.cwd,
            "profile": "review",
            "native_policy": ["-s", "read-only"],
            "caller_session": f"claude:{PARENT}",
            "rounds": [
                {
                    "round": 1,
                    "prompt": "Review the orchard",
                    "caller_session": f"claude:{PARENT}",
                    "started_at": _iso(self.now - 60),
                    "finished_at": _iso(self.now - 30),
                    "outcome": outcome,
                    "events_path": str(run_dir / "events-01.jsonl"),
                }
            ],
        }
        (run_dir / "thread.json").write_text(json.dumps(state))

    def agents(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "cairn", "agents", *args],
            env=self.env,
            text=True,
            capture_output=True,
        )

    def rows(self):
        result = self.agents("list", "--session", f"claude:{PARENT}", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        return {row["id"]: row for row in json.loads(result.stdout)}


class TestAgentsList(AgentsFixture):
    def test_classifies_subagents_and_peer_runs(self):
        self.peer_run()
        rows = self.rows()
        expect = {
            "agent-f1": ("exited", "succeeded"),
            "agent-s1": ("exited", "cancelled"),
            "agent-n1": ("exited", "succeeded"),
            "agent-r1": ("running", None),
            "agent-q1": ("running", None),
            PEER: ("exited", "succeeded"),
        }
        self.assertEqual({k: (v["execution"], v["outcome"]) for k, v in rows.items()}, expect)
        self.assertEqual(rows["agent-r1"]["activity"], "active")
        self.assertEqual(rows["agent-q1"]["activity"], "silent")
        self.assertEqual(rows[PEER]["kind"], "peer-codex")
        self.assertEqual(rows[PEER]["title"], "Review the orchard")
        self.assertEqual(rows["agent-f1"]["parent"], f"claude:{PARENT}")
        text = self.agents("list", "--session", f"claude:{PARENT}")
        self.assertIn("claude-subagent", text.stdout)
        self.assertIn("finished one", text.stdout)

    def test_never_creates_directories_and_needs_a_session(self):
        result = self.agents("list", "--session", f"claude:{PARENT}", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "data").exists())
        result = self.agents("list")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--session", result.stderr)
        self.assertFalse((self.root / "data").exists())

    def test_notifications_parse_newest_status(self):
        found = task_notifications(self.project / f"{PARENT}.jsonl")
        self.assertEqual(set(found), {"n1", "r1"})
        self.assertEqual(found["n1"][1], "completed")


class TestAgentsWait(AgentsFixture):
    def test_exit_codes(self):
        session = ("--session", f"claude:{PARENT}", "--poll-s", "0.1")
        ok = self.agents("wait", "agent-f1", "agent-n1", *session)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        failed = self.agents("wait", "agent-s1", *session)
        self.assertEqual(failed.returncode, 1)
        self.assertIn("not succeeded: claude-subagent agent-s1: cancelled", failed.stderr)
        timed_out = self.agents("wait", "agent-q1", *session, "--timeout-s", "0.3")
        self.assertEqual(timed_out.returncode, 124)  # silent alone never ends a wait
        stalled = self.agents("wait", "agent-q1", *session, "--stall-after-s", "100")
        self.assertEqual(stalled.returncode, 3)
        self.assertIn("keeps running until its own idle watchdog", stalled.stderr)
        running = self.agents("wait", "agent-r1", *session, "--timeout-s", "0.3")
        self.assertEqual(running.returncode, 124)
        self.assertFalse((self.root / "data").exists())

    def test_peer_run_failure_exits_one(self):
        self.peer_run(outcome="failed")
        result = self.agents("wait", PEER[:12], "--poll-s", "0.1")
        self.assertEqual(result.returncode, 1, result.stderr)


CODEX_PARENT = "019a0000-0000-7000-8000-0000000parent"


class CodexSubagentsFixture(StoreFixture):
    def setUp(self):
        super().setUp()
        for key in HINTS:
            self.env.pop(key, None)
            os.environ.pop(key, None)
        old = time.time() - 900
        _write(
            self.codex / f"rollout-{CODEX_PARENT}.jsonl",
            [{"type": "session_meta", "payload": {"id": CODEX_PARENT, "cwd": self.cwd}}],
        )
        self.child("019a0000-0000-7000-8000-00000000done", ["task_started", "task_complete"], old)
        self.child("019a0000-0000-7000-8000-0000000abort", ["task_started", "turn_aborted"], old)
        self.child("019a0000-0000-7000-8000-0000000lost", ["task_started", "turn_aborted:x"], old)
        self.child(
            "019a0000-0000-7000-8000-00000000busy",
            ["task_started", "task_complete", "task_started"],
            time.time() - 2,
        )

    def child(self, thread_id: str, markers: list[str], mtime: float) -> None:
        meta = {
            "id": thread_id,
            "cwd": self.cwd,
            "source": {
                "subagent": {
                    "thread_spawn": {
                        "parent_thread_id": CODEX_PARENT,
                        "depth": 1,
                        "agent_role": "worker",
                    }
                }
            },
        }
        records: list[dict] = [{"type": "session_meta", "payload": meta}]
        records.append(
            {"type": "event_msg", "payload": {"type": "user_message", "message": "do it"}}
        )
        for marker in markers:
            kind, _, reason = marker.partition(":")
            payload: dict = {"type": kind}
            if kind == "turn_aborted":
                payload["reason"] = reason or "interrupted"
            records.append({"type": "event_msg", "payload": payload})
        path = _write(self.codex / f"rollout-{thread_id}.jsonl", records)
        os.utime(path, (mtime, mtime))


class TestCodexSubagents(CodexSubagentsFixture):
    def test_native_turn_markers_decide_completion(self):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "cairn",
                "agents",
                "list",
                "--session",
                f"codex:{CODEX_PARENT}",
                "--json",
            ],
            env=self.env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = {row["id"][-5:]: row for row in json.loads(result.stdout)}
        self.assertEqual(
            (rows["0done"]["execution"], rows["0done"]["outcome"]), ("exited", "succeeded")
        )
        self.assertEqual(
            (rows["abort"]["execution"], rows["abort"]["outcome"]), ("exited", "cancelled")
        )
        self.assertEqual(
            (rows["0lost"]["execution"], rows["0lost"]["outcome"]), ("exited", "failed")
        )
        self.assertEqual(
            (rows["0busy"]["execution"], rows["0busy"]["activity"]), ("running", "active")
        )
        self.assertEqual(rows["0done"]["kind"], "codex-subagent")


def _meta(thread_id: str, cwd: str, parent: str | None = CODEX_PARENT) -> dict:
    payload: dict = {"id": thread_id, "cwd": cwd}
    if parent:
        payload["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}}
    return {"type": "session_meta", "payload": payload}


def _event(kind: str, **extra) -> dict:
    return {"type": "event_msg", "payload": {"type": kind, **extra}}


class TestCodexReplayAndFragments(CodexSubagentsFixture):
    def rollout(self, name: str, records: list[dict], mtime: float) -> Path:
        path = _write(self.codex / name, records)
        os.utime(path, (mtime, mtime))
        return path

    def replayed_parent(self) -> list[dict]:
        return [
            _meta(CODEX_PARENT, self.cwd, parent=None),
            _event("task_started", turn_id="t-parent"),
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "PARENT FINAL"}],
                },
            },
            _event("task_complete"),
        ]

    def list_rows(self) -> dict:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "cairn",
                "agents",
                "list",
                "--session",
                f"codex:{CODEX_PARENT}",
                "--json",
            ],
            env=self.env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        ids = [row["id"] for row in rows]
        self.assertEqual(len(ids), len(set(ids)), "one row per child thread")
        return {row["id"]: row for row in rows}

    def wait(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "cairn",
                "agents",
                "wait",
                *args,
                "--session",
                f"codex:{CODEX_PARENT}",
                "--poll-s",
                "0.1",
            ],
            env=self.env,
            text=True,
            capture_output=True,
        )

    def test_unresolved_replay_is_never_success(self):
        child = "019a0000-0000-7000-8000-000000unres"
        old = time.time() - 900  # past the live-child window: exported whole, unresolved
        self.rollout(
            f"rollout-{child}.jsonl", [_meta(child, self.cwd), *self.replayed_parent()], old
        )
        row = self.list_rows()[child]
        self.assertEqual((row["execution"], row["outcome"]), ("unknown", "unknown"))
        self.assertEqual(self.wait(child, "--timeout-s", "5").returncode, 1)

    def test_resolved_replay_ignores_the_parents_final_answer(self):
        open_child = "019a0000-0000-7000-8000-0000000open"
        done_child = "019a0000-0000-7000-8000-00000000fin"
        for child, own in (
            (open_child, ["task_started"]),
            (done_child, ["task_started", "task_complete"]),
        ):
            records = [
                _meta(child, self.cwd),
                *self.replayed_parent(),
                _event("thread_settings_applied", thread_id=child),  # the boundary
                *[_event(kind, turn_id="t-child") for kind in own],
            ]
            self.rollout(f"rollout-{child}.jsonl", records, time.time() - 900)
        rows = self.list_rows()
        self.assertEqual(rows[open_child]["execution"], "running")
        self.assertIsNone(rows[open_child]["outcome"])
        self.assertEqual(
            (rows[done_child]["execution"], rows[done_child]["outcome"]), ("exited", "succeeded")
        )

    def test_resumed_fragments_form_one_thread(self):
        child = "019a0000-0000-7000-8000-00000frags"
        self.rollout(
            f"rollout-a-{child}.jsonl",
            [_meta(child, self.cwd), _event("task_started"), _event("task_complete")],
            time.time() - 900,
        )
        second = self.rollout(
            f"rollout-b-{child}.jsonl",
            [_meta(child, self.cwd), _event("task_started")],
            time.time() - 2,
        )
        row = self.list_rows()[child]
        self.assertEqual(row["execution"], "running")
        self.assertEqual(row["path"], str(second))
        self.assertEqual(self.wait(child, "--timeout-s", "0.3").returncode, 124)  # not ambiguous
        with second.open("a") as handle:
            handle.write(json.dumps(_event("task_complete")) + "\n")
        row = self.list_rows()[child]
        self.assertEqual((row["execution"], row["outcome"]), ("exited", "succeeded"))
        self.assertEqual(self.wait(child).returncode, 0)


class TestAllPrecedence(AgentsFixture):
    def test_all_ignores_the_detected_session_and_hides_exited_rows(self):
        from cairn.health import process_identity

        self.peer_run()  # exited, caller claude:PARENT
        other = "019a0000-0000-7000-8000-0000000other"
        run_dir = self.root / "data/logs/invoke/codex" / other
        run_dir.mkdir(parents=True)
        identity = process_identity(os.getpid())
        assert identity is not None
        state = {
            "peer": "codex",
            "thread_id": other,
            "cwd": self.cwd,
            "profile": "review",
            "native_policy": [],
            "caller_session": "claude:someone-else",
            "rounds": [
                {
                    "round": 1,
                    "prompt": "still going",
                    "caller_session": "claude:someone-else",
                    "started_at": _iso(self.now - 10),
                    "runner_pid": identity[0],
                    "runner_started": identity[1],
                }
            ],
        }
        (run_dir / "thread.json").write_text(json.dumps(state))
        self.env["CLAUDE_SESSION_ID"] = PARENT  # an implicitly detected host session
        everything = self.agents("list", "--all", "--json")
        self.assertEqual(everything.returncode, 0, everything.stderr)
        ids = {row["id"]: row for row in json.loads(everything.stdout)}
        self.assertIn(other, ids)
        self.assertNotIn(PEER, ids)  # exited
        self.assertNotIn("agent-r1", ids)  # the implicit session no longer scopes --all
        scoped = {row["id"] for row in json.loads(self.agents("list", "--json").stdout)}
        self.assertIn(PEER, scoped)
        self.assertNotIn(other, scoped)
        explicit = self.agents("list", "--all", "--session", f"claude:{PARENT}", "--json")
        rows = {row["id"]: row for row in json.loads(explicit.stdout)}
        self.assertIn("agent-r1", rows)  # an explicit session still applies
        self.assertNotIn("agent-f1", rows)  # exited
        self.assertNotIn(PEER, rows)
        self.assertNotIn(other, rows)


class TestClassifyClaudeChild(unittest.TestCase):
    def test_order_of_rules(self):
        now = 10_000.0
        end_turn = {
            "type": "assistant",
            "timestamp": "1970-01-01T02:00:00Z",
            "message": {"stop_reason": "end_turn"},
        }
        tool_use = {**end_turn, "message": {"stop_reason": "tool_use"}}
        cases = [
            ({"stoppedByUser": True, "finished": True}, end_turn, None, ("exited", "cancelled")),
            ({"finished": True}, tool_use, None, ("exited", "succeeded")),
            ({"finished": False}, tool_use, (7300.0, "failed"), ("exited", "failed")),
            ({"finished": False}, tool_use, (7100.0, "completed"), ("running", None)),
            ({}, end_turn, None, ("exited", "succeeded")),  # older Claude: no finished key
            ({"finished": False}, end_turn, None, ("running", None)),
        ]
        for meta, last, note, expected in cases:
            with self.subTest(meta=meta, note=note):
                execution, _activity, _age, outcome = classify_claude_child(
                    meta, last, now - 5, note, now - 5, now, 120, 600
                )
                self.assertEqual((execution, outcome), expected)
        unknown = classify_claude_child({}, tool_use, now - 900, None, now - 900, now, 120, 600)
        self.assertEqual(unknown[0], "unknown")


if __name__ == "__main__":
    unittest.main()
