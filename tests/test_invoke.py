"""``cairn invoke``: the runner, the post-run pipeline, status/cancel/publish, health.

Stub ``codex``, ``claude`` and ``qmd`` executables (tests/fixtures/invoke/) run from
a temp ``bin/`` prepended to PATH; nothing touches the network or the real stores.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from cairn import config, invoke, qmd
from cairn.catalog import load_catalog
from cairn.config import PeerPolicy
from cairn.exchange import ThreadState, render_exchange, validate_head
from cairn.frontmatter import parse_frontmatter, rewrite_preserved_tail
from cairn.harnesses.claude import KEYCHAIN_GUIDANCE
from cairn.health import Heartbeat, classify, heartbeat, identity_alive, process_identity
from cairn.paths import session_dir_of
from tests.test_shared_store import StoreFixture

FIXTURES = Path(__file__).parent / "fixtures" / "invoke"
HOST_HINTS = (
    "CLAUDE_SESSION_ID",
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
    "CAIRN_HARNESS",
    "CAIRN_INVOKE_DEPTH",
    "CAIRN_INVOKE_CALLER",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_PLUGIN_ROOT",
    "PLUGIN_ROOT",
    "CLAUDE_CODE_SESSION_ID",
)
EXPECTED_CODEX_TYPES = [
    "thread.started",
    "item.completed",
    "item.completed",
    "turn.started",
    "item.completed",
    "item.started",
    "item.completed",
    "item.completed",
    "turn.completed",
]
RESULT_KEYS = {
    "status",
    "outcome",
    "peer",
    "thread_id",
    "round",
    "model",
    "effort",
    "profile",
    "native_policy",
    "cwd",
    "caller",
    "duration_s",
    "exit_code",
    "events_path",
    "stderr_path",
    "answer_path",
    "transcript_path",
    "exchange_doc",
    "peer_session_doc",
    "caller_session_doc",
    "last_message",
    "usage",
    "indexed",
    "publication",
    "error",
    "retryable",
    "warnings",
}


class InvokeFixture(StoreFixture):
    invoke_policy: ClassVar[dict] = {}

    def setUp(self):
        super().setUp()
        Path(self.cwd).mkdir()
        self.codex_home = self.root / "codexhome"
        self.sessions = self.codex_home / "sessions"
        self.sessions.mkdir(parents=True)
        shutil.copy(FIXTURES / "models_cache.json", self.codex_home / "models_cache.json")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("codex", "claude", "qmd"):
            target = self.bin / name
            target.write_text(f"#!{sys.executable}\n" + (FIXTURES / f"{name}_stub.py").read_text())
            target.chmod(0o755)
        self.stub_log = self.root / "stub.log"
        self.write_config()
        extra = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "CODEX_HOME": str(self.codex_home),
            "STUB_CLAUDE_STORE": str(self.claude),
            "STUB_LOG": str(self.stub_log),
        }
        self.env.update(extra)
        os.environ.update(extra)
        for key in HOST_HINTS:
            self.env.pop(key, None)
            os.environ.pop(key, None)

    def write_config(self, **policy):
        data = json.loads(self.config.read_text())
        data["collections"]["codex-sessions"]["store"] = str(self.sessions)
        data["qmd_binary"] = str(self.bin / "qmd")
        merged = {**self.invoke_policy, **policy}
        if merged:
            data["invoke"] = merged
        self.config.write_text(json.dumps(data))
        config.get_config.cache_clear()

    # ---- helpers ---------------------------------------------------------

    def invoke(self, *args, env=None, check_json=True):
        result = subprocess.run(
            [sys.executable, "-m", "cairn", "invoke", *args],
            env={**self.env, **(env or {})},
            text=True,
            capture_output=True,
        )
        data = None
        if check_json and "--json" in args and result.stdout.strip():
            data = json.loads(result.stdout.strip().splitlines()[-1])
        return result, data

    def run_codex(self, *args, env=None, prompt="Review the orchard plan"):
        return self.invoke("codex", "--json", "--cd", self.cwd, "--prompt", prompt, *args, env=env)

    def spawn(self, *args, env=None):
        return subprocess.Popen(
            [sys.executable, "-m", "cairn", "invoke", *args],
            env={**self.env, **(env or {})},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def stub_calls(self):
        if not self.stub_log.exists():
            return []
        return [json.loads(line) for line in self.stub_log.read_text().splitlines()]

    def run_dirs(self, peer="codex"):
        folder = self.root / "data/logs/invoke" / peer
        return sorted(folder.iterdir()) if folder.is_dir() else []

    def thread(self, thread_id, peer="codex"):
        return json.loads(
            (self.root / "data/logs/invoke" / peer / thread_id / "thread.json").read_text()
        )

    def wait_for(self, predicate, timeout=20.0, interval=0.1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(interval)
        self.fail("condition not met in time")

    def started_round(self):
        """The renamed run dir's round once the peer pid is recorded."""

        def ready():
            for run_dir in self.run_dirs():
                if run_dir.is_symlink() or run_dir.name.startswith("pending-"):
                    continue
                path = run_dir / "thread.json"
                if path.exists():
                    data = json.loads(path.read_text())
                    if data["rounds"] and data["rounds"][-1].get("peer_pid"):
                        return data
            return None

        return self.wait_for(ready)

    @staticmethod
    def gone(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False


# ---------------------------------------------------------------------------
# Codex runs
# ---------------------------------------------------------------------------


class TestCodexRun(InvokeFixture):
    def test_fresh_run_end_to_end(self):
        result, data = self.run_codex("--effort", "low")
        self.assertEqual(result.returncode, 0, result.stderr)
        assert data is not None
        self.assertEqual(RESULT_KEYS - set(data), set())
        self.assertEqual(data["status"], "succeeded")
        self.assertEqual(data["last_message"], "ANSWER")
        self.assertEqual(data["round"], 1)
        self.assertEqual(data["profile"], "review")
        self.assertEqual(data["native_policy"], ["-s", "read-only"])
        self.assertIsNone(data["caller"])
        self.assertTrue(data["indexed"])
        tid = data["thread_id"]
        # The pending dir was renamed to the thread id and its symlink removed.
        self.assertEqual([d.name for d in self.run_dirs()], [tid])
        run_dir = self.run_dirs()[0]
        # The tee holds every stdout line in order; deprecation notices never fail a run.
        lines = (run_dir / "events-01.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(line)["type"] for line in lines], EXPECTED_CODEX_TYPES)
        self.assertEqual(json.loads(lines[0])["thread_id"], tid)
        self.assertEqual((run_dir / "answer-01.md").read_text().strip(), "ANSWER")
        self.assertTrue((run_dir / "result-01.json").is_file())
        # The stub got the preamble, the sandbox, approval_policy and a closed stdin.
        call = self.stub_calls()[0]
        self.assertFalse(call["stdin"])
        self.assertIn(
            "[cairn invoke] You are codex being invoked by an operator", call["prompt_head"]
        )
        self.assertIn("(round 1)", call["prompt_head"])
        self.assertIn("Review the orchard plan", call["prompt_head"])
        argv = call["argv"]
        self.assertIn("read-only", argv)
        self.assertIn('approval_policy="never"', argv)
        self.assertIn("/pending-", argv[argv.index("-o") + 1])  # kept valid by a symlink
        # Peer export, then the exchange document in the peer's folder (no caller).
        peer_doc = Path(data["peer_session_doc"])
        self.assertTrue(peer_doc.is_file())
        self.assertEqual(parse_frontmatter(peer_doc.read_text())["session_id"], tid)
        exchange = Path(data["exchange_doc"])
        self.assertEqual(exchange.parent, session_dir_of(peer_doc))
        self.assertTrue(exchange.name.startswith("exchange-codex-"))
        fm = parse_frontmatter(exchange.read_text())
        self.assertEqual(fm["type"], "agent-exchange")
        self.assertEqual(fm["thread_id"], tid)
        self.assertEqual(fm["rounds"], "1")
        self.assertEqual(fm["run_status"], "succeeded")
        self.assertIn("ANSWER", exchange.read_text())
        self.assertIn("- run: cat a.txt", exchange.read_text())
        state = self.thread(tid)
        self.assertEqual(state["rounds"][0]["argv"][-1], "<prompt>")
        self.assertEqual(state["resume_command"], f"codex resume {tid}")
        publication = state["rounds"][0]["publication"]
        for step in ("answer_saved", "exported", "exchange_written", "indexed"):
            self.assertIn(step, publication)
        self.assertEqual(publication["pending"], {})

    def test_human_summary_without_json(self):
        result, _ = self.invoke("codex", "--cd", self.cwd, "--prompt", "hello")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("succeeded", result.stdout)
        self.assertIn("ANSWER", result.stdout)
        self.assertIn("result: ", result.stdout)

    def test_parent_links_resume_notes_and_reexport(self):
        sid = "c0ffee00-1111-4222-8333-444455556666"
        self.native("claude", sid)
        result, data = self.run_codex("--parent", f"claude:{sid}")
        self.assertEqual(result.returncode, 0, result.stderr)
        assert data is not None
        tid = data["thread_id"]
        caller_doc = Path(data["caller_session_doc"])
        peer_doc = Path(data["peer_session_doc"])
        self.assertEqual(parse_frontmatter(peer_doc.read_text())["links"], [f"parent claude:{sid}"])
        self.assertEqual(parse_frontmatter(caller_doc.read_text())["links"], [f"child codex:{tid}"])
        exchange = Path(data["exchange_doc"])
        self.assertEqual(exchange.parent, session_dir_of(caller_doc))
        call = self.stub_calls()[0]
        self.assertIn(f"invoked by claude session claude:{sid}", call["prompt_head"])

        # Hand edits: notes and a comment survive the next round.
        text = exchange.read_text().replace(
            "<!-- Add your notes here. This section is preserved across exports. -->",
            "KEEP-THIS-NOTE",
        )
        edited = rewrite_preserved_tail(text, lambda fm: fm.__setitem__("comments", "KEEP-COMMENT"))
        assert edited is not None
        exchange.write_text(edited)

        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        bad, _ = self.invoke(
            "codex", "--json", "--resume", tid, "--cd", str(elsewhere), "--prompt", "x"
        )
        self.assertEqual(bad.returncode, 2)
        self.assertIn("recorded cwd", bad.stderr)

        result2, data2 = self.invoke(
            "codex", "--json", "--resume", tid, "--parent", f"claude:{sid}", "--prompt", "Round two"
        )
        self.assertEqual(result2.returncode, 0, result2.stderr)
        assert data2 is not None
        self.assertEqual(data2["round"], 2)
        self.assertEqual(data2["thread_id"], tid)
        call2 = self.stub_calls()[-1]
        self.assertEqual(call2["argv"][1:3], ["exec", "resume"])
        self.assertIn(tid, call2["argv"])
        self.assertEqual(call2["cwd"], str(Path(self.cwd).resolve()))
        self.assertIn('sandbox_mode="read-only"', call2["argv"])
        content = exchange.read_text()
        fm = parse_frontmatter(content)
        self.assertEqual(fm["rounds"], "2")
        self.assertEqual(fm["comments"], "KEEP-COMMENT")
        self.assertIn("KEEP-THIS-NOTE", content)
        self.assertIn("### Round 2", content)
        self.assertEqual(content.count("## My Notes"), 1)
        self.assertEqual(parse_frontmatter(peer_doc.read_text())["links"], [f"parent claude:{sid}"])
        self.assertEqual(parse_frontmatter(caller_doc.read_text())["links"], [f"child codex:{tid}"])

        # links: survive a re-export of the caller with new content.
        native = self.claude / "-demo" / f"{sid}.jsonl"
        with native.open("a") as handle:
            handle.write(
                json.dumps(
                    {
                        "sessionId": sid,
                        "cwd": self.cwd,
                        "type": "user",
                        "timestamp": "2026-03-30T02:00:00Z",
                        "message": {"role": "user", "content": "later follow-up"},
                    }
                )
                + "\n"
            )
        later = time.time() + 5
        os.utime(native, (later, later))
        from cairn import hooks

        again = hooks.export_and_index(
            config.get_config(), "claude", sid, str(native), update_index=False
        )
        self.assertIn("later follow-up", again.read_text())
        self.assertEqual(parse_frontmatter(again.read_text())["links"], [f"child codex:{tid}"])

    def test_concurrent_resumes_one_busy(self):
        _, data = self.run_codex()
        assert data is not None
        tid = data["thread_id"]
        env = {"STUB_MODE": "sleep", "STUB_SLEEP": "3"}
        first = self.spawn("codex", "--json", "--resume", tid, "--prompt", "a", env=env)
        self.wait_for(lambda: len(self.thread(tid)["rounds"]) == 2)
        second = self.spawn("codex", "--json", "--resume", tid, "--prompt", "b", env=env)
        out2, _ = second.communicate(timeout=60)
        _out1, _ = first.communicate(timeout=60)
        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.returncode, invoke.EXIT_BUSY)
        busy = json.loads(out2.strip().splitlines()[-1])
        self.assertEqual(busy["status"], "busy")
        self.assertEqual(busy["holder"]["round"], 2)
        self.assertEqual(len(self.thread(tid)["rounds"]), 2)
        self.assertEqual(len(self.stub_calls()), 2 + 1)  # two runs plus the sleep child line

    def test_timeout_kills_process_group(self):
        result, data = self.run_codex(
            "--timeout-s", "2", env={"STUB_MODE": "sleep", "STUB_SLEEP": "30"}
        )
        self.assertEqual(result.returncode, invoke.EXIT_TIMEOUT, result.stderr)
        assert data is not None
        self.assertEqual(data["status"], "timeout")
        self.assertTrue(data["retryable"])
        child = next(c["sleep_child"] for c in self.stub_calls() if "sleep_child" in c)
        self.wait_for(lambda: self.gone(child), timeout=10)

    def test_idle_timeout_is_retryable(self):
        self.write_config(codex={"idle_timeout_s": 2, "quiet_after_s": 1, "stall_after_s": 2})
        result, data = self.run_codex(env={"STUB_MODE": "sleep", "STUB_SLEEP": "30"})
        self.assertEqual(result.returncode, invoke.EXIT_IDLE, result.stderr)
        assert data is not None
        self.assertEqual(data["status"], "idle_timeout")
        self.assertTrue(data["retryable"])
        self.assertIn("no activity for 2s", data["error"])

    def test_sigterm_to_runner_cancels(self):
        runner = self.spawn(
            "codex", "--json", "--cd", self.cwd, "--prompt", "x", env={"STUB_MODE": "sleep"}
        )
        self.started_round()
        child = self.wait_for(
            lambda: next((c["sleep_child"] for c in self.stub_calls() if "sleep_child" in c), None)
        )
        runner.send_signal(signal.SIGTERM)
        out, err = runner.communicate(timeout=60)
        self.assertEqual(runner.returncode, invoke.EXIT_CANCELLED, err)
        data = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(data["status"], "cancelled")
        self.assertFalse(data["retryable"])
        self.wait_for(lambda: self.gone(child), timeout=10)

    def test_cancel_command_signals_the_runner(self):
        runner = self.spawn(
            "codex", "--json", "--cd", self.cwd, "--prompt", "x", env={"STUB_MODE": "sleep"}
        )
        state = self.started_round()
        result, _data = self.invoke("cancel", state["thread_id"], "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        runner.communicate(timeout=60)
        self.assertEqual(runner.returncode, invoke.EXIT_CANCELLED)
        self.assertEqual(self.thread(state["thread_id"])["rounds"][-1]["outcome"], "cancelled")

    def test_sigkill_runner_leaves_orphan_then_status_adopts(self):
        runner = self.spawn(
            "codex",
            "--json",
            "--cd",
            self.cwd,
            "--prompt",
            "x",
            env={"STUB_MODE": "sleep", "STUB_SLEEP": "4"},
        )
        state = self.started_round()
        tid = state["thread_id"]
        peer_pid = state["rounds"][-1]["peer_pid"]
        runner.kill()
        runner.communicate(timeout=30)
        result, data = self.invoke("status", tid, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        assert data is not None
        self.assertEqual(data["execution"], "orphaned")
        self.assertFalse(data["runner_alive"])
        self.assertTrue(data["peer_alive"])
        # The stub cannot write to the dead runner's pipe; once it exits, status adopts.
        self.wait_for(lambda: self.gone(peer_pid), timeout=30)
        result, data = self.invoke("status", tid, "--json")
        assert data is not None
        self.assertEqual(data["execution"], "exited")
        self.assertEqual(data["outcome"], "aborted")
        round_ = self.thread(tid)["rounds"][-1]
        self.assertIn("adopted_at", round_)
        self.assertIn("answer_saved", round_["publication"])
        self.assertIn("exported", round_["publication"])

    def test_fail401_is_not_retryable(self):
        result, data = self.run_codex(env={"STUB_MODE": "fail401"})
        self.assertEqual(result.returncode, 1)
        assert data is not None
        self.assertEqual(data["status"], "failed")
        self.assertFalse(data["retryable"])
        self.assertIn('"status":401', data["error"])
        self.assertIn("no thread id was captured; nothing was published", data["warnings"])

    def test_turn_failed_fails_the_round(self):
        result, data = self.run_codex(env={"STUB_MODE": "turnfail"})
        self.assertEqual(result.returncode, 1)
        assert data is not None
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["error"], "model overloaded")
        self.assertTrue(data["retryable"])

    def test_codex_fork_creates_new_thread(self):
        _, data = self.run_codex()
        assert data is not None
        result, forked = self.invoke(
            "codex", "--json", "--resume", data["thread_id"], "--fork", "--prompt", "f"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        assert forked is not None
        self.assertNotEqual(forked["thread_id"], data["thread_id"])
        self.assertEqual(self.stub_calls()[-1]["argv"][1:3], ["exec", "fork"])
        self.assertEqual(self.thread(forked["thread_id"])["forked_from"], data["thread_id"])

    def test_self_check_fails_before_spawn(self):
        result, _ = self.run_codex("--model", "gpt-6-astra", "--effort", "bogus")
        self.assertEqual(result.returncode, 2)
        self.assertIn("self-check failed", result.stderr)
        self.assertEqual(self.stub_calls(), [])

    def test_depth_guard_and_env_scrub(self):
        result, _ = self.run_codex(env={"CAIRN_INVOKE_DEPTH": "1"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("nested", result.stderr)
        self.assertFalse((self.root / "data/logs/invoke").exists())
        hints = {
            "CLAUDECODE": "1",
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_PLUGIN_ROOT": "/x",
            "PLUGIN_ROOT": "/y",
            "CAIRN_INVOKE_DEPTH": "1",
        }
        result, _data = self.run_codex("--allow-nested", env=hints)
        self.assertEqual(result.returncode, 0, result.stderr)
        env = self.stub_calls()[-1]["env"]
        for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT"):
            self.assertNotIn(key, env)
        self.assertEqual(env["CAIRN_INVOKE_DEPTH"], "2")
        self.assertEqual(env["CAIRN_INVOKE_CALLER"], "unknown")

    def test_large_prompt_goes_through_stdin(self):
        prompt_file = self.root / "big.txt"
        prompt_file.write_text("orchard " * 9000)
        result, _data = self.invoke(
            "codex", "--json", "--cd", self.cwd, "--prompt-file", str(prompt_file)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        call = self.stub_calls()[0]
        self.assertTrue(call["stdin"])
        self.assertGreater(call["prompt_len"], 72000)
        self.assertEqual(call["argv"][-1], "-")


# ---------------------------------------------------------------------------
# Claude runs
# ---------------------------------------------------------------------------


class TestClaudeRun(InvokeFixture):
    def test_fresh_and_resume(self):
        result, data = self.invoke("claude", "--json", "--cd", self.cwd, "--prompt", "Say PONG")
        self.assertEqual(result.returncode, 0, result.stderr)
        assert data is not None
        self.assertEqual(data["status"], "succeeded")
        self.assertEqual(data["last_message"], "PONG")
        sid = data["thread_id"]
        argv = self.stub_calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--session-id") + 1], sid)
        self.assertIn("--disallowedTools", argv)
        self.assertEqual(argv[-2], "--")
        self.assertEqual(
            data["native_policy"],
            ["--permission-mode", "plan", "--disallowedTools", "Edit,Write,NotebookEdit"],
        )
        run_dir = self.root / "data/logs/invoke/claude" / sid
        self.assertEqual((run_dir / "answer-01.md").read_text().strip(), "PONG")
        self.assertTrue(Path(data["peer_session_doc"]).is_file())
        self.assertTrue(Path(data["exchange_doc"]).is_file())
        result2, data2 = self.invoke("claude", "--json", "--resume", sid, "--prompt", "Again")
        self.assertEqual(result2.returncode, 0, result2.stderr)
        assert data2 is not None
        self.assertEqual(data2["round"], 2)
        argv2 = self.stub_calls()[-1]["argv"]
        self.assertEqual(argv2[argv2.index("--resume") + 1], sid)
        self.assertIn("plan", argv2)
        self.assertEqual(parse_frontmatter(Path(data2["exchange_doc"]).read_text())["rounds"], "2")

    def test_fork_records_forked_from(self):
        _, data = self.invoke("claude", "--json", "--cd", self.cwd, "--prompt", "one")
        assert data is not None
        result, forked = self.invoke(
            "claude", "--json", "--resume", data["thread_id"], "--fork", "--prompt", "two"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        assert forked is not None
        self.assertNotEqual(forked["thread_id"], data["thread_id"])
        self.assertIn("--fork-session", self.stub_calls()[-1]["argv"])
        self.assertEqual(
            self.thread(forked["thread_id"], "claude")["forked_from"], data["thread_id"]
        )

    def test_not_logged_in_gives_guidance(self):
        result, data = self.invoke(
            "claude", "--json", "--cd", self.cwd, "--prompt", "x", env={"STUB_MODE": "notlogged"}
        )
        self.assertEqual(result.returncode, 1)
        assert data is not None
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["error"], KEYCHAIN_GUIDANCE)
        self.assertFalse(data["retryable"])

    def test_native_flag_mismatch_is_usage_error(self):
        result, _ = self.invoke("claude", "--cd", self.cwd, "--prompt", "x", "--fast")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Codex-only", result.stderr)


# ---------------------------------------------------------------------------
# status, publish, drain, list-models
# ---------------------------------------------------------------------------


class TestStatusAndPublish(InvokeFixture):
    def test_status_renders_and_follow_ends(self):
        self.assertFalse((self.root / "data").exists())
        missing, _ = self.invoke("status", "nope")
        self.assertEqual(missing.returncode, 1)
        self.assertFalse((self.root / "data").exists())
        _, data = self.run_codex()
        assert data is not None
        tid = data["thread_id"]
        result, _ = self.invoke("status", tid[:8])
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertTrue(lines[1].startswith("exited (succeeded), none for"), lines[1])
        self.assertIn("runner gone, peer gone", lines[1])
        self.assertIn("  - run: cat a.txt", result.stdout)
        self.assertIn("  - message: Looking", result.stdout)
        self.assertIn("ANSWER", result.stdout)
        follow, _ = self.invoke("status", tid, "--follow")
        self.assertEqual(follow.returncode, 0)
        result, status = self.invoke("status", tid, "--json")
        assert status is not None
        self.assertEqual(status["execution"], "exited")
        self.assertEqual(status["outcome"], "succeeded")

    def test_status_shows_progress_midway(self):
        runner = self.spawn(
            "codex",
            "--json",
            "--cd",
            self.cwd,
            "--prompt",
            "x",
            env={"STUB_MODE": "sleep", "STUB_SLEEP": "3"},
        )
        state = self.started_round()
        _result, data = self.invoke("status", state["thread_id"], "--json")
        assert data is not None
        self.assertEqual(data["execution"], "running")
        self.assertEqual(data["activity"], "active")
        self.assertTrue(data["runner_alive"])
        runner.communicate(timeout=60)

    def test_held_index_lock_then_publish_clears_it(self):
        out = io.StringIO()
        with (
            patch.object(invoke, "INDEX_LOCK_TIMEOUT_S", 0.3),
            qmd.locked_index(),
            redirect_stdout(out),
            redirect_stderr(io.StringIO()),
        ):
            code = invoke.main(["codex", "--json", "--cd", self.cwd, "--prompt", "x"])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertFalse(data["indexed"])
        self.assertIn("index busy", data["publication"]["pending"]["indexed"])
        self.assertTrue(any(w.startswith("indexed:") for w in data["warnings"]))
        self.assertEqual(data["status"], "succeeded")
        result, _published = self.invoke("publish", data["thread_id"], "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        round_ = self.thread(data["thread_id"])["rounds"][0]
        self.assertIn("indexed", round_["publication"])
        self.assertEqual(round_["publication"]["pending"], {})
        again, _ = self.invoke("publish", data["thread_id"])
        self.assertIn("nothing pending", again.stdout)

    def test_sync_drains_pending_publications(self):
        _, data = self.run_codex("--no-index")
        assert data is not None
        self.assertFalse(data["indexed"])
        self.assertIn("--no-index", data["publication"]["pending"]["indexed"])
        result = self.run_cli("sync", check=False)
        round_ = self.thread(data["thread_id"])["rounds"][0]
        self.assertEqual(round_["publication"]["pending"], {}, result.stdout + result.stderr)
        self.assertIn("indexed", round_["publication"])

    def test_list_models(self):
        result, _ = self.invoke("list-models")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gpt-6-astra", result.stdout)
        self.assertIn("ultra", result.stdout)
        self.assertRegex(result.stdout, r"gpt-6-swift.*\[fast\]")
        self.assertIn("claude has no model catalogue", result.stderr)
        result, _rows = self.invoke("list-models", "--json", check_json=False)
        models = json.loads(result.stdout)
        self.assertEqual({row["slug"] for row in models}, {"gpt-6-astra", "gpt-6-swift"})

    def test_exchange_docs_stay_out_of_session_views(self):
        sid = "c0ffee00-1111-4222-8333-444455557777"
        self.native("claude", sid)
        _, data = self.run_codex("--parent", f"claude:{sid}")
        assert data is not None
        exchange = Path(data["exchange_doc"])
        self.assertTrue(exchange.is_file())
        for collection in ("claude-code-sessions", "codex-sessions"):
            catalog = load_catalog(self.root / "data" / collection)
            self.assertEqual(catalog.errors, {})
        listing = self.run_cli("codex", "list", "--all", "--json")
        self.assertNotIn("exchange-", listing.stdout)
        listing = self.run_cli("claude", "list", "--all", "--json")
        self.assertNotIn("exchange-", listing.stdout)
        recall = self.run_cli(
            "recall", "list", "2026-01-01", "--all-projects", "--json", check=False
        )
        self.assertNotIn("exchange-", recall.stdout)
        probe = (
            "import sys; from cairn.sources.claude import ClaudeSource as S; "
            "r = S().find_record(sys.argv[1]); print(r.path if r else None)"
        )
        found = subprocess.run(
            [sys.executable, "-c", probe, sid], env=self.env, text=True, capture_output=True
        )
        self.assertNotIn("exchange-", found.stdout)
        self.assertTrue(found.stdout.strip().endswith(".md"), found.stderr)


# ---------------------------------------------------------------------------
# Exchange renderer (pure)
# ---------------------------------------------------------------------------


class TestExchangeRenderer(unittest.TestCase):
    def state(self):
        return ThreadState(
            peer="codex",
            thread_id="019a0000-0000-7000-8000-000000000abc",
            cwd="/work/repo",
            profile="review",
            native_policy=["-s", "read-only"],
            rounds=[
                {
                    "round": 1,
                    "prompt": "Has ``` fences ````` inside",
                    "started_at": "2026-10-17T10:00:00+00:00",
                    "finished_at": "2026-10-17T10:01:00+00:00",
                    "outcome": "succeeded",
                    "progress": ["run: ls"],
                    "last_message": "done",
                    "duration_s": 60.0,
                    "exit_code": 0,
                }
            ],
        )

    def test_renders_valid_document_with_long_fence(self):
        text = render_exchange(self.state(), None, None)
        fm = parse_frontmatter(text)
        self.assertEqual(fm["type"], "agent-exchange")
        self.assertEqual(fm["native_policy"], "-s read-only")
        self.assertEqual(fm["status"], "active")
        self.assertNotIn("source", fm)
        self.assertIn("``````text\nHas ``` fences ````` inside\n``````", text)
        self.assertIn('title: "codex round 1: Has ``` fences ````` inside"', text)

    def test_rejects_tail_and_source_keys(self):
        with self.assertRaisesRegex(ValueError, "preserved-tail"):
            validate_head(["---", "type: agent-exchange", "status_extra: x"])
        with self.assertRaisesRegex(ValueError, "preserved-tail"):
            validate_head(["links_seen: 2"])
        with self.assertRaisesRegex(ValueError, "source"):
            validate_head(["source: claude"])
        with self.assertRaisesRegex(ValueError, "native_id"):
            validate_head(["native_id: x"])
        validate_head(["run_status: running", "peer_session: codex:x"])


# ---------------------------------------------------------------------------
# Health (pure)
# ---------------------------------------------------------------------------


class TestHealth(unittest.TestCase):
    policy = PeerPolicy(quiet_after_s=120, stall_after_s=600)

    def test_classify_table(self):
        start = "2026-10-17T10:00:00+00:00"
        t0 = 1_792_231_200.0  # 2026-10-17T10:00:00Z
        open_round = {"started_at": start, "peer_pid": 42}
        cases: list[tuple[dict, float, bool, bool, float | None, str | None, tuple]] = [
            # (round, now, runner, peer, heartbeat, done) -> (execution, activity, outcome)
            ({"started_at": start}, t0 + 5, True, False, None, None, ("pending", "active", None)),
            (open_round, t0 + 30, True, True, t0 + 20, None, ("running", "active", None)),
            (open_round, t0 + 300, True, True, t0 + 10, None, ("running", "quiet", None)),
            (open_round, t0 + 900, True, True, t0 + 10, None, ("running", "silent", None)),
            (open_round, t0 + 30, False, True, t0 + 20, None, ("orphaned", "active", None)),
            (open_round, t0 + 30, False, False, None, None, ("unknown", "none", "unknown")),
            (open_round, t0 + 30, False, False, None, "succeeded", ("exited", "none", "succeeded")),
            (
                {**open_round, "finished_at": start, "outcome": "idle_timeout"},
                t0 + 30,
                False,
                False,
                None,
                None,
                ("exited", "none", "idle_timeout"),
            ),
        ]
        for round_, now, runner, peer, beat, done, expected in cases:
            with self.subTest(expected=expected):
                health = classify(
                    round_,
                    now,
                    self.policy,
                    runner_alive=runner,
                    peer_alive=peer,
                    heartbeat_at=beat,
                    done_status=done,
                )
                self.assertEqual((health.execution, health.activity, health.outcome), expected)

    def test_identity_includes_start_time(self):
        identity = process_identity(os.getpid())
        assert identity is not None
        self.assertTrue(identity_alive(os.getpid(), identity[1]))
        self.assertFalse(identity_alive(os.getpid(), "1"))  # a reused pid
        self.assertFalse(identity_alive(None, identity[1]))
        self.assertIsNone(process_identity(2**22 + 12345))

    def test_heartbeat_takes_newest_source(self):
        beat = heartbeat(None, None, "2026-10-17T10:00:00+00:00", "x")
        self.assertEqual(beat, Heartbeat(1_792_231_200.0, "thread.json", "x"))
        self.assertEqual(heartbeat(None, None, None).source, "none")


if __name__ == "__main__":
    unittest.main()
