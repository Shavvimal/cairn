"""``cairn invoke``: the runner, the post-run pipeline, status/cancel/publish, health.

Stub ``codex``, ``claude``, ``agy`` and ``qmd`` executables (tests/fixtures/invoke/) run
from a temp ``bin/`` prepended to PATH; nothing touches the network or the real stores.
"""

from __future__ import annotations

import hashlib
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
from datetime import datetime
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from cairn import config, invoke, qmd
from cairn.catalog import load_catalog
from cairn.config import PeerPolicy
from cairn.exchange import ThreadState, render_exchange, validate_head, write_exchange
from cairn.frontmatter import parse_frontmatter, rewrite_preserved_tail
from cairn.harnesses.claude import KEYCHAIN_GUIDANCE
from cairn.health import (
    Heartbeat,
    classify,
    heartbeat,
    identity_alive,
    parse_linux_stat,
    parse_ps_line,
    process_identity,
)
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
    # The developer's shell may route agy to GCP; the billing tests set these themselves.
    "AGY_ADC_AUTH",
    "GOOGLE_CLOUD_QUOTA_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "CLOUDSDK_CONFIG",
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
    "files_written",
    "denied",
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
        self.agy_store = self.root / "agy"
        self.agy_store.mkdir()
        # agy allows one shell command, so runs carry no permissions warning by default.
        (self.agy_store / "settings.json").write_text(
            json.dumps({"permissions": {"allow": ["command(rg)"]}})
        )
        shutil.copy(FIXTURES / "models_cache.json", self.codex_home / "models_cache.json")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, stub in (
            ("codex", "codex"),
            ("claude", "claude"),
            ("agy", "antigravity"),
            ("gcloud", "gcloud"),
            ("qmd", "qmd"),
        ):
            target = self.bin / name
            target.write_text(f"#!{sys.executable}\n" + (FIXTURES / f"{stub}_stub.py").read_text())
            target.chmod(0o755)
        self.stub_log = self.root / "stub.log"
        self.write_config()
        extra = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "CODEX_HOME": str(self.codex_home),
            "STUB_CLAUDE_STORE": str(self.claude),
            "STUB_AGY_STORE": str(self.agy_store),
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
        data["collections"]["antigravity-sessions"] = {"store": str(self.agy_store)}
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
        if not folder.is_dir():
            return []
        return sorted(d for d in folder.iterdir() if not d.name.startswith("."))

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

    def started_round(self, peer="codex"):
        """The renamed run dir's round once the peer pid is recorded."""

        def ready():
            for run_dir in self.run_dirs(peer):
                if run_dir.is_symlink() or run_dir.name.startswith("pending-"):
                    continue
                path = run_dir / "thread.json"
                if path.exists():
                    data = json.loads(path.read_text())
                    # The id and the peer pid together: a renamed directory is coherent
                    # from the moment it appears, so both are set in one read.
                    if (
                        data.get("thread_id")
                        and data["rounds"]
                        and data["rounds"][-1].get("peer_pid")
                    ):
                        return data
            return None

        return self.wait_for(ready)

    def hold_env(self, name="hold"):
        """Stub env that blocks after thread.started until :meth:`release` (a handshake,
        not a sleep): ``ready`` exists once the stub is waiting."""
        ready, release = self.root / f"{name}.ready", self.root / f"{name}.release"
        return {"STUB_MODE": "hold", "STUB_READY": str(ready), "STUB_RELEASE": str(release)}

    def wait_ready(self, name="hold"):
        self.wait_for(lambda: (self.root / f"{name}.ready").exists())

    def release(self, name="hold"):
        (self.root / f"{name}.release").touch()

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
        answer_target = Path(argv[argv.index("-o") + 1])
        self.assertEqual(answer_target.parent.name, ".answers")  # outside the renamed run dir
        self.assertFalse(answer_target.exists())  # moved to answer-01.md
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
        self.assertEqual(fm["anchor_session"], f"codex:{tid}")
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
        first = self.spawn("codex", "--json", "--resume", tid, "--prompt", "a", env=self.hold_env())
        self.wait_ready()  # the first resume holds the thread lock and its peer is waiting
        second = self.spawn("codex", "--json", "--resume", tid, "--prompt", "b")
        out2, _ = second.communicate(timeout=60)
        self.release()
        first.communicate(timeout=60)
        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.returncode, invoke.EXIT_BUSY)
        busy = json.loads(out2.strip().splitlines()[-1])
        self.assertEqual(busy["status"], "busy")
        self.assertEqual(busy["holder"]["round"], 2)
        self.assertEqual(len(self.thread(tid)["rounds"]), 2)
        self.assertEqual(len(self.stub_calls()), 2)  # the busy call spawned nothing

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
        # The peer's shutdown output, written while it was being stopped, is kept.
        events = Path(data["events_path"]).read_text()
        self.assertIn('"SHUTDOWN"', events)
        self.assertIn("message: SHUTDOWN", self.thread(data["thread_id"])["rounds"][-1]["progress"])

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
            "codex", "--json", "--cd", self.cwd, "--prompt", "x", env=self.hold_env()
        )
        self.wait_ready()
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
        # The peer writes its stdout straight to the events file, so it finishes
        # normally after the runner died and status adopts a successful round.
        self.release()
        self.wait_for(lambda: self.gone(peer_pid), timeout=30)
        result, data = self.invoke("status", tid, "--json")
        assert data is not None
        self.assertEqual(data["execution"], "exited")
        self.assertEqual(data["outcome"], "succeeded")
        self.assertEqual(data["last_message"], "ANSWER")
        round_ = self.thread(tid)["rounds"][-1]
        self.assertIn("adopted_at", round_)
        self.assertEqual(round_["usage"], {"input_tokens": 10, "output_tokens": 2})
        for step in ("answer_saved", "exported", "exchange_written", "indexed"):
            self.assertIn(step, round_["publication"])
        self.assertFalse(Path(round_["answer_target"]).exists())
        self.assertEqual((self.run_dirs()[0] / "answer-01.md").read_text().strip(), "ANSWER")

    def test_announces_thread_id_before_the_peer_finishes(self):
        runner = self.spawn(
            "codex", "--json", "--cd", self.cwd, "--prompt", "x", env=self.hold_env()
        )
        assert runner.stderr is not None
        line = runner.stderr.readline()
        while line and not line.startswith("cairn invoke:"):
            line = runner.stderr.readline()
        self.assertIsNone(runner.poll())  # still running: the peer waits for release
        state = self.started_round()
        tid = state["thread_id"]
        self.assertTrue(line.startswith(f"cairn invoke: codex thread {tid} round 1 (run dir "))
        self.assertIn(str(self.run_dirs()[0]), line)
        # The events path rides on the same line, so a watcher needs no guess at the round.
        self.assertIn(f"events {self.run_dirs()[0] / 'events-01.jsonl'})", line)
        self.release()
        out, err = runner.communicate(timeout=60)
        self.assertEqual(runner.returncode, 0, err)
        self.assertNotIn("cairn invoke:", err)  # announced exactly once
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["thread_id"], tid)

    def test_rename_holds_the_thread_lock_before_exposing_the_directory(self):
        runner = self.spawn(
            "codex", "--json", "--cd", self.cwd, "--prompt", "x", env=self.hold_env()
        )
        self.wait_ready()
        state = self.started_round()
        tid = state["thread_id"]
        self.assertEqual(state["thread_id"], self.run_dirs()[0].name)  # coherent identity
        result, _ = self.invoke("publish", tid)
        self.assertEqual(result.returncode, invoke.EXIT_BUSY, result.stdout + result.stderr)
        self.release()
        out, err = runner.communicate(timeout=60)
        self.assertEqual(runner.returncode, 0, err)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["status"], "succeeded")

    def test_transcript_only_activity_is_not_idle(self):
        self.write_config(codex={"idle_timeout_s": 1})
        result, data = self.run_codex(env={"STUB_MODE": "rollout", "STUB_SLEEP": "3"})
        self.assertEqual(result.returncode, 0, result.stderr)
        assert data is not None
        self.assertEqual(data["status"], "succeeded")

    def test_closed_stdout_keeps_the_watchdogs(self):
        self.write_config(codex={"idle_timeout_s": 1})
        started = time.monotonic()
        result, data = self.run_codex(env={"STUB_MODE": "closeout", "STUB_SLEEP": "30"})
        self.assertEqual(result.returncode, invoke.EXIT_IDLE, result.stderr)
        assert data is not None
        self.assertEqual(data["status"], "idle_timeout")
        self.assertLess(time.monotonic() - started, 20)

    def test_resume_defaults_to_the_recorded_caller(self):
        sid = "c0ffee00-1111-4222-8333-444455558888"
        self.native("claude", sid)
        _, data = self.run_codex("--parent", f"claude:{sid}")
        assert data is not None
        result, data2 = self.invoke(
            "codex", "--json", "--resume", data["thread_id"], "--prompt", "y"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        assert data2 is not None
        self.assertEqual(data2["caller"], f"claude:{sid}")
        self.assertFalse([w for w in data2["warnings"] if "caller unknown" in w])

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
            "ACME_TOOL_PLUGIN_ROOT": "/z",
            "CAIRN_INVOKE_DEPTH": "1",
        }
        result, _data = self.run_codex("--allow-nested", env=hints)
        self.assertEqual(result.returncode, 0, result.stderr)
        env = self.stub_calls()[-1]["env"]
        scrubbed = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT")
        for key in (*scrubbed, "ACME_TOOL_PLUGIN_ROOT"):
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


class TestRunnerEdges(InvokeFixture):
    def test_prompt_dash_reads_stdin(self):
        for flag in ("--prompt", "--prompt-file"):
            with self.subTest(flag=flag):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "cairn",
                        "invoke",
                        "codex",
                        "--json",
                        "--cd",
                        self.cwd,
                        flag,
                        "-",
                    ],
                    env=self.env,
                    input=f"PROMPT-FROM-STDIN {flag}",
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"PROMPT-FROM-STDIN {flag}", self.stub_calls()[-1]["prompt_head"])

    def test_split_utf8_temporary_eof_and_unterminated_last_line(self):
        result, data = self.run_codex(env={"STUB_MODE": "split"})
        self.assertEqual(result.returncode, 0, result.stderr)
        assert data is not None
        self.assertEqual(data["status"], "succeeded")  # turn.completed had no newline
        self.assertEqual(data["usage"], {"output_tokens": 3})
        self.assertEqual(data["last_message"], "PONG é")
        progress = self.thread(data["thread_id"])["rounds"][0]["progress"]
        self.assertIn("message: PONG é", progress)

    def test_rename_transition_blocks_publish_and_keeps_the_answer_path(self):
        real_rename = os.rename
        seen: dict = {}

        def barrier(src, dst):
            real_rename(src, dst)
            # Inside the transition: renamed, thread.json not yet rewritten.
            round_ = json.loads((Path(dst) / "thread.json").read_text())["rounds"][-1]
            target = Path(round_["answer_target"])
            target.write_text("MID-TRANSITION")  # the peer's -o path is valid throughout
            seen["target_ok"] = target.read_text() == "MID-TRANSITION"
            seen["outside"] = Path(dst) not in target.parents
            publish = subprocess.run(
                [sys.executable, "-m", "cairn", "invoke", "publish", Path(dst).name],
                env=self.env,
                text=True,
                capture_output=True,
            )
            seen["publish_rc"] = publish.returncode
            self.release()  # only now may the held peer finish and write its -o file

        out = io.StringIO()
        with (
            patch.object(invoke.os, "rename", barrier),
            redirect_stdout(out),
            redirect_stderr(io.StringIO()),
            patch.dict(os.environ, self.hold_env()),
        ):
            code = invoke.main(["codex", "--json", "--cd", self.cwd, "--prompt", "x"])
        self.assertEqual(code, 0)
        self.assertEqual(seen, {"target_ok": True, "outside": True, "publish_rc": invoke.EXIT_BUSY})
        data = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual(data["status"], "succeeded")
        self.assertEqual(data["last_message"], "ANSWER")  # the peer's final -o write won
        self.assertEqual(self.thread(data["thread_id"])["thread_id"], data["thread_id"])


class TestLineTail(unittest.TestCase):
    def test_split_character_and_unterminated_line(self):
        tail = invoke.LineTail()
        text = '{"text": "é"}\n{"last": 1}'.encode()
        cut = text.index("é".encode()) + 1
        self.assertEqual(tail.feed(text[:cut]), [])
        lines = tail.feed(text[cut:])
        self.assertEqual([line.decode() for line in lines], ['{"text": "é"}\n'])
        self.assertEqual(tail.feed(b""), [])  # a temporary EOF changes nothing
        self.assertEqual(tail.flush(), [b'{"last": 1}\n'])
        self.assertEqual(tail.flush(), [])


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
# Antigravity runs
# ---------------------------------------------------------------------------


class TestAntigravityRun(InvokeFixture):
    def run_agy(self, *args, prompt="Say PONG", env=None):
        return self.invoke(
            "antigravity", "--json", "--cd", self.cwd, "--prompt", prompt, *args, env=env
        )

    def test_fresh_run_without_caller_keeps_only_the_answer(self):
        result, data = self.run_agy()
        self.assertEqual(result.returncode, 0, result.stderr)
        assert data is not None
        self.assertEqual(RESULT_KEYS - set(data), set())
        self.assertEqual(data["status"], "succeeded")
        self.assertEqual(data["last_message"], "PONG")
        self.assertEqual(data["native_policy"], ["--mode", "plan"])
        self.assertEqual(data["usage"]["num_turns"], 1)
        self.assertEqual(data["usage"]["total_tokens"], 12)
        tid = data["thread_id"]
        # The id came from the init event: the pending dir was renamed to it.
        self.assertEqual([d.name for d in self.run_dirs("antigravity")], [tid])
        run_dir = self.run_dirs("antigravity")[0]
        self.assertEqual((run_dir / "answer-01.md").read_text().strip(), "PONG")
        argv = self.stub_calls()[0]["argv"]
        self.assertEqual(argv[1:5], ["--output-format", "stream-json", "--print-timeout", "0"])
        self.assertNotIn("--disable-slash-commands", argv)  # it would cancel --mode plan
        self.assertIn("--mode", argv)
        self.assertTrue(argv[-1].startswith("-p=[cairn invoke] You are antigravity"))
        self.assertNotIn("--conversation", argv)
        # The conversation agy recorded is exported at once; with no caller the
        # exchange document anchors on the peer document's folder.
        peer_doc = Path(data["peer_session_doc"])
        self.assertTrue(peer_doc.is_file())
        self.assertEqual(peer_doc.parent, self.root / "data/antigravity-sessions")
        fm = parse_frontmatter(peer_doc.read_text())
        self.assertEqual(fm["type"], "antigravity-session")
        self.assertEqual(fm["session_id"], tid)
        self.assertEqual(fm["title"], "Stub Conversation Title")
        self.assertEqual(fm["run_status"], "IDLE")
        body = peer_doc.read_text()
        self.assertIn("### User", body)
        self.assertIn("Say PONG", body)
        self.assertNotIn("<USER_REQUEST>", body)
        self.assertNotIn("ADDITIONAL_METADATA", body)
        self.assertIn('<tool name="read_file" status="done">', body)
        self.assertIn("<details><summary>Reasoning</summary>", body)
        exchange = Path(data["exchange_doc"])
        self.assertEqual(exchange.parent, session_dir_of(peer_doc))
        self.assertTrue(data["indexed"])
        # The only warning is the operator run's unknown caller; no publication step failed.
        self.assertEqual(len(data["warnings"]), 1)
        self.assertIn("caller unknown", data["warnings"][0])
        state = self.thread(tid, "antigravity")
        round_ = state["rounds"][0]
        self.assertEqual(round_["argv"][-1], "-p=<prompt>")
        self.assertEqual(
            set(round_["publication"]),
            {"answer_saved", "exported", "exchange_written", "indexed", "pending"},
        )
        self.assertEqual(round_["publication"]["pending"], {})
        self.assertEqual(
            round_["transcript_path"],
            str(self.agy_store / "brain" / tid / ".system_generated/logs/transcript_full.jsonl"),
        )
        self.assertEqual(state["resume_command"], f"agy --conversation {tid}")
        self.assertIn('tool: read_file {"Path": "a.py"}', round_["progress"])
        self.assertIn("message: Looking.", round_["progress"][0])

    def test_caller_anchors_the_exchange_and_resume_continues_the_conversation(self):
        sid = "c0ffee00-1111-4222-8333-444455557777"
        self.native("claude", sid)
        result, data = self.run_agy("--parent", f"claude:{sid}")
        self.assertEqual(result.returncode, 0, result.stderr)
        assert data is not None
        tid = data["thread_id"]
        caller_doc = Path(data["caller_session_doc"])
        exchange = Path(data["exchange_doc"])
        self.assertEqual(exchange.parent, session_dir_of(caller_doc))
        self.assertTrue(exchange.name.startswith("exchange-antigravity-"))
        fm = parse_frontmatter(exchange.read_text())
        self.assertEqual(fm["thread_id"], tid)
        self.assertEqual(fm["anchor_session"], f"claude:{sid}")
        self.assertIn("PONG", exchange.read_text())
        self.assertTrue(data["indexed"])
        self.assertEqual(data["warnings"], [])
        peer_doc = Path(data["peer_session_doc"])
        self.assertEqual(parse_frontmatter(peer_doc.read_text())["links"], [f"parent claude:{sid}"])
        self.assertEqual(
            parse_frontmatter(caller_doc.read_text())["links"], [f"child antigravity:{tid}"]
        )
        publication = self.thread(tid, "antigravity")["rounds"][0]["publication"]
        self.assertEqual(
            set(publication),
            {
                "answer_saved",
                "exported",
                "caller_exported",
                "linked",
                "exchange_written",
                "indexed",
                "pending",
            },
        )
        self.assertIn(
            f"invoked by claude session claude:{sid}", self.stub_calls()[0]["prompt_head"]
        )

        result2, data2 = self.invoke(
            "antigravity", "--json", "--resume", tid, "--prompt", "Again", "--effort", "high"
        )
        self.assertEqual(result2.returncode, 0, result2.stderr)
        assert data2 is not None
        self.assertEqual(data2["round"], 2)
        self.assertEqual(data2["thread_id"], tid)
        argv2 = self.stub_calls()[-1]["argv"]
        self.assertEqual(argv2[argv2.index("--conversation") + 1], tid)
        self.assertEqual(argv2[argv2.index("--effort") + 1], "high")
        self.assertIn("plan", argv2)  # the profile's flags are re-sent on a resume
        self.assertEqual(parse_frontmatter(exchange.read_text())["rounds"], "2")
        # One conversation, one document: the second round re-exports it in place.
        self.assertEqual(data2["peer_session_doc"], str(peer_doc))
        self.assertEqual(parse_frontmatter(peer_doc.read_text())["messages"], "2")
        # One session document in the peer collection; the exchange lives with the caller.
        self.assertEqual(len(list((self.root / "data/antigravity-sessions").rglob("*.md"))), 1)
        resumed, _ = self.invoke("--help", check_json=False)  # parser smoke
        self.assertEqual(resumed.returncode, 0)
        result = subprocess.run(
            [sys.executable, "-m", "cairn", "resume", f"antigravity:{tid}"],
            env=self.env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"agy --conversation {tid}")

    def test_unconfigured_collection_skips_export_with_one_warning(self):
        data = json.loads(self.config.read_text())
        del data["collections"]["antigravity-sessions"]
        self.config.write_text(json.dumps(data))
        config.get_config.cache_clear()
        result, data = self.run_agy()
        self.assertEqual(result.returncode, 0, result.stderr)
        assert data is not None
        self.assertEqual(data["status"], "succeeded")
        self.assertIsNone(data["peer_session_doc"])
        self.assertIsNone(data["exchange_doc"])  # no caller and no peer document: no anchor
        self.assertTrue(any("not configured" in w for w in data["warnings"]))
        publication = self.thread(data["thread_id"], "antigravity")["rounds"][0]["publication"]
        self.assertEqual(set(publication), {"answer_saved", "pending"})
        self.assertEqual(publication["pending"], {})

    def test_native_mode_and_large_prompt_via_ndjson_stdin(self):
        prompt_file = self.root / "big.txt"
        prompt_file.write_text("orchard " * 9000)
        result, data = self.invoke(
            "antigravity",
            "--json",
            "--cd",
            self.cwd,
            "--prompt-file",
            str(prompt_file),
            "--mode",
            "auto",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        assert data is not None
        self.assertEqual(data["native_policy"], ["--mode", "auto"])
        call = self.stub_calls()[0]
        self.assertTrue(call["stdin"])
        self.assertGreater(call["prompt_len"], 72000)
        argv = call["argv"]
        self.assertEqual(argv[-3:], ["--input-format", "stream-json", "-p="])
        self.assertEqual(argv[argv.index("--mode") + 1], "auto")

    def test_bad_model_fails_and_is_not_retryable(self):
        result, data = self.invoke(
            "antigravity",
            "--json",
            "--cd",
            self.cwd,
            "--prompt",
            "x",
            "--model",
            "bogus",
            env={"STUB_MODE": "badmodel"},
        )
        self.assertEqual(result.returncode, 1)
        assert data is not None
        self.assertEqual(data["status"], "failed")
        self.assertIsNone(data["thread_id"])
        self.assertIn("invalid model selection", data["error"])
        self.assertFalse(data["retryable"])

    def write_adc(self, quota_project="adc-project"):
        adc = self.root / "home" / ".config" / "gcloud" / "application_default_credentials.json"
        adc.parent.mkdir(parents=True, exist_ok=True)
        body = {"type": "authorized_user", "refresh_token": "x"}
        if quota_project:
            body["quota_project_id"] = quota_project
        adc.write_text(json.dumps(body))
        return adc

    def test_default_route_inherits_the_host_environment(self):
        result, data = self.run_agy()
        self.assertEqual(result.returncode, 0, result.stderr)
        assert data is not None
        self.assertEqual(data["billing"]["route"], "inherit")
        self.assertIsNone(data["billing"]["project"])
        self.assertIsNone(data["billing"]["problem"])
        self.assertEqual(data["billing"]["observed"]["auth_method"], "gcp")  # the stub's login
        env = self.stub_calls()[0]["env"]
        self.assertNotIn("AGY_ADC_AUTH", env)
        run_dir = self.run_dirs("antigravity")[0]
        self.assertTrue((run_dir / "peer-01.log").is_file())  # moved in from .logs/
        self.assertEqual(list((self.root / "data/logs/invoke/antigravity/.logs").iterdir()), [])
        result, data = self.run_agy(env={"AGY_ADC_AUTH": "true"})
        assert data is not None
        self.assertEqual(self.stub_calls()[-1]["env"]["AGY_ADC_AUTH"], "true")  # passed through
        self.assertEqual(data["billing"]["observed"]["auth_method"], "adc")
        self.assertIsNone(data["billing"]["problem"])

    def test_gcp_route_sets_adc_env_and_verifies_from_the_log(self):
        self.write_adc("adc-project")
        self.write_config(
            antigravity={"billing": {"route": "gcp", "project": "cfg-project", "location": "eu"}}
        )
        result, data = self.run_agy()
        self.assertEqual(result.returncode, 0, result.stderr)
        assert data is not None
        env = self.stub_calls()[0]["env"]
        self.assertEqual(env["AGY_ADC_AUTH"], "true")
        self.assertEqual(env["GOOGLE_CLOUD_QUOTA_PROJECT"], "cfg-project")
        self.assertEqual(env["GOOGLE_CLOUD_LOCATION"], "eu")
        self.assertIn("--log-file", self.stub_calls()[0]["argv"])
        billing = data["billing"]
        self.assertEqual(
            {k: billing[k] for k in ("route", "project", "location", "problem")},
            {"route": "gcp", "project": "cfg-project", "location": "eu", "problem": None},
        )
        self.assertEqual(billing["observed"]["auth_method"], "adc")
        self.assertEqual(billing["observed"]["quota_project"], "cfg-project")
        self.assertEqual(billing["observed"]["vertex_projects"], ["cfg-project"])
        self.assertEqual([w for w in data["warnings"] if "billing" in w], [])
        text, _ = self.invoke("antigravity", "--cd", self.cwd, "--prompt", "x")
        self.assertIn("billing: route=gcp project=cfg-project auth=adc", text.stdout)
        self.assertIn("verified", text.stdout)
        # Without a configured project the ADC file's quota project is the one billed.
        self.write_config(antigravity={"billing": {"route": "gcp"}})
        _, data2 = self.run_agy()
        assert data2 is not None
        self.assertEqual(data2["billing"]["project"], "adc-project")
        self.assertNotIn("GOOGLE_CLOUD_QUOTA_PROJECT", self.stub_calls()[-1]["env"])
        self.assertEqual(data2["billing"]["observed"]["quota_project"], "stub-project")
        self.assertIn("quotaProject=stub-project", data2["billing"]["problem"])
        self.assertTrue(any(w.startswith("billing: ") for w in data2["warnings"]))

    def test_gcp_route_refuses_to_spawn_without_adc(self):
        self.write_config(antigravity={"billing": {"route": "gcp"}})
        result, _ = self.run_agy()
        self.assertEqual(result.returncode, 2)
        self.assertIn("billing check failed", result.stderr)
        self.assertIn("application_default_credentials.json", result.stderr)
        self.assertEqual(self.stub_calls(), [])
        self.assertEqual(self.run_dirs("antigravity"), [])
        self.write_adc(quota_project=None)
        result, _ = self.run_agy()
        self.assertEqual(result.returncode, 2)
        self.assertIn("no quota project", result.stderr)
        self.assertEqual(self.stub_calls(), [])
        # --skip-self-check never skips the billing check.
        result, _ = self.run_agy("--skip-self-check")
        self.assertEqual(result.returncode, 2)
        # A project from the environment satisfies it.
        result, data = self.run_agy(env={"GOOGLE_CLOUD_QUOTA_PROJECT": "env-project"})
        self.assertEqual(result.returncode, 0, result.stderr)
        assert data is not None
        self.assertEqual(data["billing"]["project"], "env-project")
        self.assertIsNone(data["billing"]["problem"])

    def test_gcp_route_refuses_to_spawn_when_the_adc_token_is_stale(self):
        """The ADC file exists but Google rejects its refresh token (invalid_rapt): the
        probe refuses the spawn with the login command. A probe that cannot conclude
        (no network) warns and spawns; a stale token agy itself reports is named."""
        self.write_adc("adc-project")
        self.write_config(antigravity={"billing": {"route": "gcp"}})
        result, _ = self.run_agy(env={"STUB_GCLOUD": "rapt"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("billing check failed: Google rejected the ADC refresh token", result.stderr)
        self.assertIn("invalid_rapt", result.stderr)
        self.assertIn("gcloud auth application-default login", result.stderr)
        self.assertEqual(self.stub_calls(), [])
        result, data = self.run_agy(env={"STUB_GCLOUD": "fail"})
        self.assertEqual(result.returncode, 0, result.stderr)
        assert data is not None
        warning = next(w for w in data["warnings"] if w.startswith("billing: "))
        self.assertIn("ADC token not verified", warning)
        self.assertIn("could not resolve host", warning)
        self.assertEqual(len(self.stub_calls()), 1)
        # agy's log is read when the probe passed but agy's own refresh failed.
        result, data = self.run_agy(env={"STUB_MODE": "noauth"})
        self.assertEqual(result.returncode, 1)
        assert data is not None
        self.assertEqual(data["status"], "failed")
        self.assertIn("ADC token refresh failed", data["error"])
        self.assertIn("invalid_rapt", data["error"])
        self.assertIn("gcloud auth application-default login", data["error"])
        self.assertFalse(data["retryable"])
        self.assertIn("ADC token refresh failed", data["billing"]["problem"])
        text, _ = self.invoke(
            "antigravity", "--cd", self.cwd, "--prompt", "x", env={"STUB_MODE": "noauth"}
        )
        self.assertIn("ADC token refresh failed", text.stdout + text.stderr)
        # The plan route never probes.
        self.write_config(antigravity={"billing": {"route": "plan"}})
        result, data = self.run_agy(env={"STUB_GCLOUD": "rapt"})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_billing_flag_overrides_the_route_per_round(self):
        self.write_adc()
        self.write_config(antigravity={"billing": {"route": "gcp"}})
        result, data = self.run_agy("--billing", "plan", env={"AGY_ADC_AUTH": "true"})
        self.assertEqual(result.returncode, 0, result.stderr)
        assert data is not None
        self.assertEqual(data["billing"]["route"], "plan")
        self.assertNotIn("AGY_ADC_AUTH", self.stub_calls()[0]["env"])  # stripped
        self.assertIsNone(data["billing"]["problem"])
        # A resume takes today's route, not the recorded one.
        result, data2 = self.invoke(
            "antigravity", "--json", "--resume", data["thread_id"], "--prompt", "again"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        assert data2 is not None
        self.assertEqual(data2["billing"]["route"], "gcp")
        self.assertEqual(self.stub_calls()[-1]["env"]["AGY_ADC_AUTH"], "true")
        exchange = Path(self.run_dirs("antigravity")[0] / "thread.json")
        rounds = json.loads(exchange.read_text())["rounds"]
        self.assertEqual([r["billing"]["route"] for r in rounds], ["plan", "gcp"])
        result, _ = self.invoke("codex", "--cd", self.cwd, "--prompt", "x", "--billing", "gcp")
        self.assertEqual(result.returncode, 2)  # argparse: codex has no --billing

    def test_edit_profile_refuses_without_command_rules(self):
        """An accept-edits run with no ``command`` rule cannot test what it writes, so
        it is refused before anything is spawned (exit 2, like the billing check), with
        the one command that fixes it. Review still runs and carries the warning."""
        settings = self.agy_store / "settings.json"
        settings.unlink()
        result, _ = self.run_agy("--profile", "edit")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(
            "Error: permissions check failed: agy has no command allow rules", result.stderr
        )
        self.assertIn("cairn hook install --harness antigravity --grant-commands", result.stderr)
        self.assertIn("--skip-permission-check", result.stderr)
        self.assertEqual(self.stub_calls(), [])  # nothing spawned
        self.assertEqual(self.run_dirs("antigravity"), [])
        result, _ = self.run_agy("--mode", "accept-edits")
        self.assertEqual(result.returncode, 2)
        # Plan (review) and full still run; only plan warns.
        result, data = self.run_agy()
        self.assertEqual(result.returncode, 0, result.stderr)
        assert data is not None
        self.assertEqual(data["status"], "succeeded")
        self.assertTrue(any(w.startswith("permissions: ") for w in data["warnings"]))
        self.assertIn("--grant-commands", " ".join(data["warnings"]))
        result, data = self.run_agy("--profile", "full")
        assert data is not None
        self.assertFalse(any(w.startswith("permissions: ") for w in data["warnings"]))
        result, data = self.run_agy("--profile", "edit", "--skip-permission-check")
        self.assertEqual(result.returncode, 0, result.stderr)
        assert data is not None
        self.assertTrue(any(w.startswith("permissions: ") for w in data["warnings"]))
        # The rules in force are recorded with the round.
        state = self.thread(data["thread_id"], "antigravity")
        self.assertEqual(state["rounds"][0]["permissions"]["allow"], [])
        settings.write_text(json.dumps({"permissions": {"allow": ["command(grep)"]}}))
        result, data = self.run_agy("--profile", "edit")
        self.assertEqual(result.returncode, 0, result.stderr)
        assert data is not None
        self.assertFalse(any(w.startswith("permissions: ") for w in data["warnings"]))
        state = self.thread(data["thread_id"], "antigravity")
        self.assertEqual(state["rounds"][0]["permissions"]["allow"], ["command(grep)"])
        self.assertEqual(state["rounds"][0]["permissions"]["path"], str(settings))

    def test_refused_command_with_empty_answer_is_a_denied_round(self):
        """The dead round from the post-mortem: agy refuses one ``rg``, the model gives
        up with an empty response, agy says SUCCESS. cairn ends it as ``denied`` with
        exit 2, names the command and the rule it needs, and the next resume tells the
        peer what changed."""
        command = 'git log --oneline -3 && echo "---"'
        result, data = self.run_agy("--profile", "edit", env={"STUB_DENY": command})
        self.assertEqual(result.returncode, 2, result.stderr)
        assert data is not None
        self.assertEqual(
            (data["status"], data["outcome"], data["exit_code"]), ("denied", "denied", 2)
        )
        self.assertTrue(data["retryable"])
        self.assertIsNone(data["last_message"])
        self.assertEqual(data["files_written"], [])
        self.assertEqual(data["denied"], [{"tool": "run_command", "target": command}])
        self.assertEqual(data["usage"]["denied_actions"], ["command"])
        error = data["error"]
        self.assertIn("agy auto-denied 1 tool call(s)", error)
        self.assertIn(
            f"`{command}` needs command(git log), command(echo) in permissions.allow", error
        )
        self.assertIn(str(self.agy_store / "settings.json"), error)
        self.assertIn("then resume", error)
        self.assertFalse(any(w.startswith("permissions: ") for w in data["warnings"]))
        tid = data["thread_id"]
        run_dir = self.run_dirs("antigravity")[0]
        self.assertEqual(json.loads((run_dir / "result-01.json").read_text())["status"], "denied")
        round_ = self.thread(tid, "antigravity")["rounds"][0]
        self.assertIn(f"run: {command}", round_["progress"])
        self.assertIn(f"denied: run_command {command}", round_["progress"])
        self.assertEqual(round_["outcome"], "denied")
        # The round is still exported, linked and indexed: the refusal is data, not a crash.
        self.assertTrue(data["indexed"])
        self.assertIn("- denied: run_command git log", Path(data["exchange_doc"]).read_text())
        # Allow the commands, resume: the peer learns the diff and the verdict on its refusal.
        (self.agy_store / "settings.json").write_text(
            json.dumps(
                {"permissions": {"allow": ["command(rg)", "command(git log)", "command(echo)"]}}
            )
        )
        result, data = self.run_agy("--resume", tid, prompt="Carry on")
        self.assertEqual(result.returncode, 0, result.stderr)
        assert data is not None
        self.assertEqual(data["status"], "succeeded")
        head = self.stub_calls()[-1]["prompt_head"]
        self.assertTrue(head.startswith("[cairn invoke] You are antigravity"))
        self.assertIn("[cairn invoke] Shell permissions since your previous round:", head)
        self.assertIn("- newly allowed: command(echo), command(git log)", head)
        self.assertIn(f"- previously refused `{command}`: now allowed", head)
        self.assertIn("\n\n---\n\nCarry on", head)
        rounds = self.thread(tid, "antigravity")["rounds"]
        self.assertIn("newly allowed", rounds[1]["permissions"]["note"])
        self.assertEqual(rounds[1]["prompt"], "Carry on")  # the note is not the prompt
        # A third round with nothing changed and nothing refused gets no note.
        result, data = self.run_agy("--resume", tid, prompt="Again")
        assert data is not None
        self.assertIsNone(self.thread(tid, "antigravity")["rounds"][2]["permissions"]["note"])
        self.assertNotIn("Shell permissions", self.stub_calls()[-1]["prompt_head"])

    def test_soft_denied_read_with_empty_answer_is_a_denied_round(self):
        """The second dead round: plan mode soft-denies one ``read_file`` outside the
        workspace, the step ends DONE with no error, the turn ends with an empty answer
        and agy says SUCCESS with ``denied_actions``. cairn ends it as ``denied``."""
        outside = str(self.root / "elsewhere" / "plan.md")
        result, data = self.run_agy(env={"STUB_SOFT_DENY": outside})
        self.assertEqual(result.returncode, 2, result.stderr)
        assert data is not None
        self.assertEqual(
            (data["status"], data["outcome"], data["exit_code"]), ("denied", "denied", 2)
        )
        self.assertTrue(data["retryable"])
        self.assertIsNone(data["last_message"])
        self.assertEqual(data["usage"]["denied_actions"], ["read_file"])
        # The path comes from agy's log (the step it names) joined to the stream.
        self.assertEqual(data["denied"], [{"tool": "read_file", "target": outside}])
        error = data["error"]
        self.assertIn("agy auto-denied 1 tool call(s)", error)
        self.assertIn(f"`{outside}` is outside the workspace", error)
        self.assertIn(f"needs read_file({self.root / 'elsewhere'}) in permissions.allow", error)
        self.assertIn(f"--extra --add-dir {self.root / 'elsewhere'}", error)
        permissions = self.thread(data["thread_id"], "antigravity")["rounds"][0]["permissions"]
        self.assertEqual(permissions["roots"], [str(Path(self.cwd).resolve())])
        self.assertEqual(permissions["ask"], [])
        self.assertFalse(any(w.startswith("permissions: ") for w in data["warnings"]))
        self.assertFalse(any(w.startswith("empty answer") for w in data["warnings"]))
        run_dir = self.run_dirs("antigravity")[0]
        self.assertEqual(json.loads((run_dir / "result-01.json").read_text())["status"], "denied")
        self.assertEqual(
            self.thread(data["thread_id"], "antigravity")["rounds"][0]["outcome"], "denied"
        )
        # ``tail`` on a stream the runner has not finalised yet decides the same way:
        # the record is stripped of its finish to stand in for that window.
        path = run_dir / "thread.json"
        saved = json.loads(path.read_text())
        state = json.loads(json.dumps(saved))
        for key in ("finished_at", "outcome", "exit_code"):
            state["rounds"][0].pop(key, None)
        path.write_text(json.dumps(state))
        result, _ = self.invoke("tail", data["thread_id"], "--no-follow", check_json=False)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], "RESULT denied")
        # Even with the count stripped from the stream, the log's soft denial decides.
        events = run_dir / "events-01.jsonl"
        kept = events.read_text()
        events.write_text(
            kept.replace(
                '"denied_actions": [{"action": "read_file", "display_name": "ReadFile"}]',
                '"denied_actions": []',
            )
        )
        self.assertNotIn("ReadFile", events.read_text())
        result, _ = self.invoke("tail", data["thread_id"], "--no-follow", check_json=False)
        self.assertEqual(result.stdout.splitlines()[-1], "RESULT denied")
        _, shown = self.invoke("status", data["thread_id"], "--json")
        assert shown is not None
        self.assertEqual(shown["outcome"], "denied")
        self.assertIsNone(shown["last_message"])  # "Looking." never poses as the answer
        # Following a stream whose runner never finalises: after the grace wait, the
        # fallback reads the log and still says denied (the record stays unfinished).
        result, _ = self.invoke("tail", data["thread_id"], check_json=False)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], "RESULT denied")
        events.write_text(kept)
        # ``wait`` while another reader holds the thread lock (adopting): it holds for
        # that reader's record instead of building a result from an unfinished round.
        (run_dir / "result-01.json").unlink()
        lock = invoke.ThreadLock()
        lock.acquire(run_dir)
        waiter = self.spawn("wait", data["thread_id"], "--json")
        time.sleep(1.5)
        lock.release()
        out, err = waiter.communicate(timeout=60)
        self.assertEqual(waiter.returncode, 2, err)
        shown = json.loads(out.strip().splitlines()[-1])
        self.assertEqual((shown["status"], shown["exit_code"]), ("denied", 2))
        self.assertIn("result file missing", " ".join(shown["warnings"]))
        _, shown = self.invoke("status", data["thread_id"], "--json")
        assert shown is not None
        self.assertEqual((shown["execution"], shown["outcome"]), ("exited", "denied"))
        self.assertEqual(shown["denied"], [{"tool": "read_file", "target": outside}])
        # Restore the record and resume with --extra --add-dir: the added directory is
        # a root for the verdict, so the peer learns the path is readable now.
        path.write_text(json.dumps(saved))
        result, data = self.run_agy(
            "--resume",
            data["thread_id"],
            "--extra",
            "--add-dir",
            str(self.root / "elsewhere"),
            prompt="Carry on",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        assert data is not None
        head = self.stub_calls()[-1]["prompt_head"]
        self.assertIn(
            f"- previously refused read_file `{outside}`: now readable "
            f"(inside {self.root / 'elsewhere'})",
            head,
        )
        rounds = self.thread(data["thread_id"], "antigravity")["rounds"]
        self.assertEqual(
            rounds[1]["permissions"]["roots"],
            [str(Path(self.cwd).resolve()), str(self.root / "elsewhere")],
        )
        # A rule change alone is a diff line; the previous round refused nothing.
        (self.agy_store / "settings.json").write_text(
            json.dumps({"permissions": {"allow": ["command(rg)", f"read_file({self.root})"]}})
        )
        result, data = self.run_agy("--resume", data["thread_id"], prompt="Again")
        self.assertEqual(result.returncode, 0, result.stderr)
        head = self.stub_calls()[-1]["prompt_head"]
        self.assertIn(f"- newly allowed: read_file({self.root})", head)
        self.assertNotIn("previously refused", head)

    def test_abandoned_soft_denied_round_is_adopted_as_denied(self):
        """A dead runner: ``wait`` adopts the round from the stream and reaches the same
        verdict, exit code and empty answer as the live runner would have."""
        outside = str(self.root / "elsewhere" / "plan.md")
        runner = self.spawn(
            "antigravity",
            "--json",
            "--cd",
            self.cwd,
            "--prompt",
            "x",
            env={"STUB_SOFT_DENY": outside, "STUB_SLEEP": "3"},
        )
        state = self.started_round("antigravity")
        tid = state["thread_id"]
        peer_pid = state["rounds"][0]["peer_pid"]
        os.kill(runner.pid, signal.SIGKILL)
        runner.wait()
        self.wait_for(lambda: self.gone(runner.pid))
        self.wait_for(lambda: self.gone(peer_pid))
        result, data = self.invoke("wait", tid, "--json")
        self.assertEqual(result.returncode, 2, result.stderr)
        assert data is not None
        self.assertEqual(
            (data["status"], data["exit_code"], data["retryable"]), ("denied", 2, True)
        )
        self.assertIsNone(data["last_message"])
        self.assertEqual(data["denied"][0]["tool"], "read_file")
        self.assertIn("read_file", data["error"])
        self.assertIn("result file missing", " ".join(data["warnings"]))
        round_ = self.thread(tid, "antigravity")["rounds"][0]
        self.assertTrue(round_["adopted_at"])
        self.assertEqual(round_["exit_code"], 2)
        # ``status`` reads the record, not the stream: no progress message poses as the answer.
        _, shown = self.invoke("status", tid, "--json")
        assert shown is not None
        self.assertEqual((shown["execution"], shown["outcome"]), ("exited", "denied"))
        self.assertIsNone(shown["last_message"])
        result, _ = self.invoke("tail", tid, check_json=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout.splitlines()[-1], "RESULT denied")

    def test_partial_refusal_warns_and_the_result_lists_files_written(self):
        (self.agy_store / "settings.json").write_text(
            json.dumps({"permissions": {"allow": ["command(rg)"], "deny": ["command(curl)"]}})
        )
        target = str(Path(self.cwd) / "out.sql")
        env = {
            "STUB_CMD": "rg foo src",
            "STUB_WRITE": target,
            "STUB_DENY": "curl https://x.test",
            "STUB_DENY_ANSWER": "1",
        }
        result, data = self.run_agy("--profile", "edit", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        assert data is not None
        self.assertEqual((data["status"], data["last_message"]), ("succeeded", "PONG"))
        self.assertEqual(data["files_written"], [target])
        self.assertEqual(data["denied"], [{"tool": "run_command", "target": "curl https://x.test"}])
        warning = next(w for w in data["warnings"] if w.startswith("permissions: "))
        self.assertIn("agy auto-denied 1 tool call(s), so the answer may be incomplete", warning)
        self.assertIn("`curl https://x.test` denied by command(curl)", warning)
        round_ = self.thread(data["thread_id"], "antigravity")["rounds"][0]
        self.assertIn("run: rg foo src", round_["progress"])
        self.assertIn(f"wrote: {target}", round_["progress"])
        exchange = Path(data["exchange_doc"]).read_text()
        self.assertIn(f"- files written: {target}", exchange)
        self.assertIn("- denied: run_command curl https://x.test", exchange)
        # The human summary names the files too.
        result, _ = self.invoke(
            "antigravity", "--cd", self.cwd, "--prompt", "x", "--profile", "edit", env=env
        )
        self.assertIn(f"files_written: {target}", result.stdout)
        # A run that wrote nothing says so (an empty list), unlike peers that do not report.
        result, data = self.run_agy()
        assert data is not None
        self.assertEqual(data["files_written"], [])
        _, codex = self.run_codex()
        assert codex is not None
        self.assertIsNone(codex["files_written"])
        self.assertEqual(codex["denied"], [])

    def test_usage_refusals(self):
        result, _ = self.run_agy("--fast")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Codex-only", result.stderr)
        result, data = self.run_agy()
        assert data is not None
        result, _ = self.invoke(
            "antigravity", "--resume", data["thread_id"], "--fork", "--prompt", "f"
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot fork", result.stderr)
        result, _ = self.invoke("codex", "--cd", self.cwd, "--prompt", "x", "--mode", "plan")
        self.assertEqual(result.returncode, 2)  # argparse: --mode is not a codex flag
        result, _ = self.invoke(
            "antigravity", "--cd", self.cwd, "--prompt", "x", "--permission-mode", "plan"
        )
        self.assertEqual(result.returncode, 2)


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

    def test_tail_replays_a_finished_round_one_line_per_call(self):
        target = str(Path(self.cwd) / "a.sql")
        _, data = self.invoke(
            "antigravity",
            "--json",
            "--cd",
            self.cwd,
            "--prompt",
            "x",
            "--profile",
            "edit",
            env={"STUB_CMD": "rg foo", "STUB_WRITE": target},
        )
        assert data is not None
        tid = data["thread_id"]
        result, _ = self.invoke("tail", tid)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertTrue(lines[0].startswith(f"START {tid}"), lines[0])
        self.assertIn('TOOL read_file {"Path": "a.py"}', lines)
        self.assertIn("CMD rg foo", lines)
        self.assertIn(f"WRITE {target}", lines)
        self.assertIn("MSG Looking.", lines)
        self.assertIn("MSG PONG", lines)
        self.assertEqual(lines[-1], "RESULT succeeded")
        self.assertLess(lines.index("CMD rg foo"), lines.index(f"WRITE {target}"))
        result, _ = self.invoke("tail", tid, "--json", check_json=False)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(rows[0]["kind"], "thread_started")
        self.assertIn({"kind": "command", "text": "rg foo"}, rows)
        self.assertEqual(rows[-1], {"kind": "result", "outcome": "succeeded"})
        missing, _ = self.invoke("tail", tid, "--round", "9")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("no round 9", missing.stderr)
        # A denied round ends the feed with its outcome and exit code.
        _, data = self.invoke(
            "antigravity",
            "--json",
            "--cd",
            self.cwd,
            "--prompt",
            "x",
            "--profile",
            "edit",
            env={"STUB_DENY": "curl x"},
        )
        assert data is not None
        result, _ = self.invoke("tail", data["thread_id"])
        self.assertEqual(result.returncode, 2)
        lines = result.stdout.splitlines()
        self.assertIn("DENIED run_command curl x", lines)
        self.assertEqual(lines[-1], "RESULT denied")
        # Codex threads replay through the same feed with their generic steps.
        _, codex = self.run_codex()
        assert codex is not None
        result, _ = self.invoke("tail", codex["thread_id"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("STEP run: cat a.txt", result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith("RESULT succeeded"))

    def test_tail_and_wait_follow_a_live_round(self):
        runner = self.spawn(
            "antigravity",
            "--json",
            "--cd",
            self.cwd,
            "--prompt",
            "x",
            env={"STUB_SLEEP": "3", "STUB_CMD": "rg live"},
        )
        state = self.started_round("antigravity")
        tid = state["thread_id"]
        tail = self.spawn("tail", tid)
        waiter = self.spawn("wait", tid, "--json")
        runner_out, runner_err = runner.communicate(timeout=60)
        self.assertEqual(runner.returncode, 0, runner_err)
        tail_out, tail_err = tail.communicate(timeout=60)
        self.assertEqual(tail.returncode, 0, tail_err)
        lines = tail_out.splitlines()
        self.assertIn("CMD rg live", lines)
        self.assertEqual(lines[-1], "RESULT succeeded")
        wait_out, wait_err = waiter.communicate(timeout=60)
        self.assertEqual(waiter.returncode, 0, wait_err)
        # ``wait`` prints the very line the runner printed (the saved result file).
        self.assertEqual(
            json.loads(wait_out.strip()), json.loads(runner_out.strip().splitlines()[-1])
        )
        # Already finished: both return at once, with the same verdict.
        result, _ = self.invoke("wait", tid)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("round 1: succeeded", result.stdout)
        self.assertIn("PONG", result.stdout)
        result, data = self.invoke("wait", tid, "--json")
        assert data is not None
        self.assertEqual(data["status"], "succeeded")
        # A denied round: wait exits with the round's code.
        _, denied = self.invoke(
            "antigravity", "--json", "--cd", self.cwd, "--prompt", "x", env={"STUB_DENY": "curl x"}
        )
        assert denied is not None
        result, data = self.invoke("wait", denied["thread_id"], "--json")
        self.assertEqual(result.returncode, 2)
        assert data is not None
        self.assertEqual(data["status"], "denied")

    def test_wait_adopts_an_abandoned_round(self):
        runner = self.spawn(
            "codex", "--json", "--cd", self.cwd, "--prompt", "x", env=self.hold_env()
        )
        state = self.started_round()
        tid = state["thread_id"]
        peer_pid = state["rounds"][0]["peer_pid"]
        os.kill(runner.pid, signal.SIGKILL)
        runner.wait()
        self.wait_for(lambda: self.gone(runner.pid))
        self.release()
        self.wait_for(lambda: self.gone(peer_pid))
        result, data = self.invoke("wait", tid, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        assert data is not None
        self.assertEqual(data["status"], "succeeded")
        self.assertIn("result file missing", " ".join(data["warnings"]))
        self.assertTrue(self.thread(tid)["rounds"][0]["adopted_at"])

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

    def test_interrupted_publication_is_finished_by_publish(self):
        def killed(*_args):
            raise SystemExit("runner killed between steps")

        with (
            patch.dict(invoke._STEP_FUNCTIONS, {"exchange_written": killed}),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            invoke.main(["codex", "--json", "--cd", self.cwd, "--prompt", "x"])
        tid = self.run_dirs()[0].name
        round_ = self.thread(tid)["rounds"][0]
        self.assertTrue(round_["finished_at"])
        self.assertIn("exported", round_["publication"])
        self.assertEqual(set(round_["publication"]["pending"]), {"exchange_written", "indexed"})
        result, _ = self.invoke("publish", tid)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        round_ = self.thread(tid)["rounds"][0]
        self.assertEqual(round_["publication"]["pending"], {})
        for step in ("exchange_written", "indexed"):
            self.assertIn(step, round_["publication"])
        self.assertTrue(Path(self.thread(tid)["exchange_doc"]).is_file())

    def test_late_upstream_success_redoes_downstream_steps(self):
        cfg = config.get_config()
        state = ThreadState("codex", "t-1", self.cwd, "review", [])
        state.path = self.root / "t.json"
        round_: dict = {"round": 1, "finished_at": "2026-10-17T10:00:00+00:00"}
        state.rounds.append(round_)
        calls: list[str] = []

        def step(name, error=None):
            def run(*_args):
                calls.append(name)
                if error:
                    raise RuntimeError(error)

            return run

        fake_index = patch.object(invoke, "update_index", lambda *a, **k: calls.append("indexed"))
        with (
            patch.dict(
                invoke._STEP_FUNCTIONS,
                {"exported": step("exported", "export broke"), "exchange_written": step("x")},
            ),
            fake_index,
        ):
            invoke.publish_round(cfg, state, round_)
        self.assertEqual(calls, ["exported", "x", "indexed"])
        self.assertEqual(invoke.pending_steps(cfg, round_, "codex"), ("exported",))
        calls.clear()
        with (
            patch.dict(
                invoke._STEP_FUNCTIONS,
                {"exported": step("exported"), "exchange_written": step("x")},
            ),
            fake_index,
        ):
            invoke.publish_round(
                cfg, state, round_, steps=invoke.pending_steps(cfg, round_, "codex")
            )
        self.assertEqual(calls, ["exported", "x", "indexed"])  # downstream redone
        self.assertEqual(invoke.pending_steps(cfg, round_, "codex"), ())
        self.assertEqual(round_["publication"]["pending"], {})

    def test_late_caller_export_moves_the_exchange_to_the_caller_folder(self):
        sid = "c0ffee00-1111-4222-8333-44445555aaaa"
        _, data = self.run_codex("--parent", f"claude:{sid}")  # no caller transcript yet
        assert data is not None
        tid = data["thread_id"]
        pending = data["publication"]["pending"]
        self.assertIn("caller_exported", pending)
        self.assertIn("linked", pending)
        provisional = Path(data["exchange_doc"])
        self.assertEqual(provisional.parent, session_dir_of(Path(data["peer_session_doc"])))
        self.assertIsNone(self.thread(tid)["anchor"])  # not chosen for good
        provisional.write_text(
            provisional.read_text().replace(
                "<!-- Add your notes here. This section is preserved across exports. -->",
                "NOTE-SURVIVES-MOVE",
            )
        )
        self.native("claude", sid)
        result, _ = self.invoke("publish", tid)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = self.thread(tid)
        moved = Path(state["exchange_doc"])
        self.assertEqual(moved.parent, session_dir_of(Path(state["caller_session_doc"])))
        self.assertFalse(provisional.exists())
        self.assertIn("NOTE-SURVIVES-MOVE", moved.read_text())
        self.assertEqual(state["anchor"], str(moved.parent))
        self.assertEqual(state["anchor_session"], f"claude:{sid}")
        self.assertEqual(parse_frontmatter(moved.read_text())["anchor_session"], f"claude:{sid}")
        round_ = state["rounds"][0]
        self.assertEqual(round_["publication"]["pending"], {})
        self.assertIn("exchange_written", round_["publication"])
        caller_doc = Path(state["caller_session_doc"])
        self.assertEqual(parse_frontmatter(caller_doc.read_text())["links"], [f"child codex:{tid}"])

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


class TestExchangeCollisions(InvokeFixture):
    def state(self, thread_id, peer="codex"):
        doc = self.root / "data/codex-sessions/2026-10-17-peerdoc.md"
        state = ThreadState(peer, thread_id, self.cwd, "review", [])
        state.peer_session_doc = str(doc)
        state.rounds.append({"round": 1, "prompt": f"prompt of {thread_id}"})
        return state

    def test_colliding_short_ids_never_share_a_document(self):
        cfg = config.get_config()
        for peer, first_id, second_id in (
            (
                "codex",
                "019a0000-1111-7000-8000-abcd00000001",
                "019a0000-2222-7000-8000-abcd00000002",
            ),
            (
                "claude",
                "deadbeef-1111-4000-8000-000000000001",
                "deadbeef-2222-4000-8000-000000000002",
            ),
        ):
            with self.subTest(peer=peer):
                one, two = self.state(first_id, peer), self.state(second_id, peer)
                first = write_exchange(cfg, one, describe_folder=False)
                second = write_exchange(cfg, two, describe_folder=False)
                assert first is not None and second is not None
                self.assertNotEqual(first, second)
                self.assertEqual(parse_frontmatter(first.read_text())["thread_id"], first_id)
                self.assertEqual(parse_frontmatter(second.read_text())["thread_id"], second_id)
                self.assertIn(hashlib.sha256(second_id.encode()).hexdigest()[:16], second.name)
                # Re-writing both keeps each in its own file.
                self.assertEqual(write_exchange(cfg, one, describe_folder=False), first)
                self.assertEqual(write_exchange(cfg, two, describe_folder=False), second)

    def test_refuses_to_overwrite_another_threads_document(self):
        cfg = config.get_config()
        one = self.state("019a0000-1111-7000-8000-abcd00000001")
        path = write_exchange(cfg, one, describe_folder=False)
        assert path is not None
        intruder = self.state("019a0000-9999-7000-8000-ffff00000009")
        intruder.anchor, intruder.exchange_doc = str(path.parent), str(path)
        with self.assertRaisesRegex(ValueError, "belongs to thread"):
            write_exchange(cfg, intruder, describe_folder=False)
        self.assertEqual(parse_frontmatter(path.read_text())["thread_id"], one.thread_id)


class TestReplayFacts(unittest.TestCase):
    """The structured facts a replay keeps beside the progress bullets, the ``denied``
    verdict, and the note a resumed peer gets about its permissions."""

    def test_feed_collects_commands_writes_and_refusals(self):
        from cairn.harnesses import Event

        replay = invoke.Replay()
        replay.feed(
            [
                Event("command", text="rg foo"),
                Event("file_written", text="/r/a.py"),
                Event("file_written", text="/r/a.py"),
                Event("denied", text="curl x", error="run_command"),
                Event("done", status="succeeded", text=""),
            ]
        )
        self.assertEqual(replay.commands, ["rg foo"])
        self.assertEqual(replay.files_written, ["/r/a.py"])
        self.assertEqual(replay.denied, [{"tool": "run_command", "target": "curl x"}])
        self.assertEqual(
            replay.progress,
            ["run: rg foo", "wrote: /r/a.py", "wrote: /r/a.py", "denied: run_command curl x"],
        )
        self.assertTrue(invoke.denied_outcome(replay))
        replay.done = Event("done", status="succeeded", text="an answer")
        self.assertFalse(invoke.denied_outcome(replay))
        replay.done = Event("done", status="failed", text="")
        self.assertFalse(invoke.denied_outcome(replay))
        self.assertFalse(invoke.denied_outcome(invoke.Replay()))

    def test_denial_text_and_tail_lines(self):
        from cairn.harnesses import Event

        permissions = {"path": "/s.json", "allow": ["command(rg)"], "deny": ["command(curl)"]}
        denied = [
            {"tool": "run_command", "target": "git diff"},
            {"tool": "run_command", "target": "curl x"},
            {"tool": "run_command", "target": "rg x"},
            {"tool": "replace_file_content", "target": "/r/b.py"},
        ]
        text = invoke.denial_text(denied, permissions)
        self.assertIn("agy auto-denied 4 tool call(s): ", text)
        self.assertIn("`git diff` needs command(git diff) in permissions.allow", text)
        self.assertIn("`curl x` denied by command(curl)", text)
        self.assertIn("`rg x` (the recorded rules allow it; agy still refused it)", text)
        self.assertIn(
            "replace_file_content /r/b.py (needs a replace_file_content allow rule)", text
        )
        self.assertIn("/s.json", text)
        self.assertIn(
            "(+2 more)",
            invoke.denial_text(denied * 3 if False else denied + denied + denied[:2], permissions),
        )
        self.assertEqual(
            invoke.tail_line(Event("thread_started", thread_id="t1", text="m")), "START t1 m"
        )
        self.assertEqual(invoke.tail_line(Event("command", text="rg x")), "CMD rg x")
        self.assertEqual(invoke.tail_line(Event("file_written", text="/a")), "WRITE /a")
        self.assertEqual(
            invoke.tail_line(Event("denied", text="rg x", error="run_command")),
            "DENIED run_command rg x",
        )
        self.assertEqual(invoke.tail_line(Event("message", text="hi")), "MSG hi")
        self.assertEqual(
            invoke.tail_line(Event("progress", text="tool: view_file {}")), "TOOL view_file {}"
        )
        self.assertEqual(
            invoke.tail_line(Event("progress", text="tool failed: x: y")), "FAILED x: y"
        )
        self.assertEqual(invoke.tail_line(Event("progress", text="run: cat")), "STEP run: cat")
        self.assertEqual(invoke.tail_line(Event("notice", text="n")), "NOTICE n")
        self.assertIsNone(invoke.tail_line(Event("done", status="succeeded")))

    def test_refusals_merge_the_stream_the_log_and_the_count(self):
        """``denied_actions`` counts action types; the stream and the log name calls."""
        done = invoke.Event(
            "done",
            text=None,
            status="succeeded",
            usage={"denied_actions": ["command", "read_file"]},
        )
        replay = invoke.Replay()
        replay.feed([done])
        self.assertTrue(invoke.denied_outcome(replay))
        self.assertEqual(invoke.stream_outcome(replay), "denied")
        self.assertEqual(
            replay.refusals(),
            [{"tool": "run_command", "target": ""}, {"tool": "read_file", "target": ""}],
        )
        # A stream refusal names the command; the counted ``command`` adds nothing.
        replay.feed([invoke.Event("denied", text="rg x", error="run_command")])
        self.assertEqual(
            replay.refusals(),
            [{"tool": "run_command", "target": "rg x"}, {"tool": "read_file", "target": ""}],
        )
        # The log names the soft-denied reads, several per counted action type.
        observed = [
            {"tool": "read_file", "target": "/x/a.md"},
            {"tool": "read_file", "target": "/x/b.md"},
            {"tool": "read_file", "target": "/x/a.md"},
        ]
        replay.observed = observed
        self.assertEqual(
            replay.refusals(),
            [
                {"tool": "run_command", "target": "rg x"},
                {"tool": "read_file", "target": "/x/a.md"},
                {"tool": "read_file", "target": "/x/b.md"},
            ],
        )
        # A real answer beside a refusal is not a denied round.
        answered = invoke.Replay()
        answered.feed([invoke.Event("done", text="PONG", status="succeeded", usage=done.usage)])
        self.assertFalse(invoke.denied_outcome(answered))
        self.assertEqual(invoke.stream_outcome(answered), "succeeded")
        self.assertIsNone(invoke.stream_outcome(invoke.Replay()))

    def test_only_an_authentication_error_yields_to_the_adc_log(self):
        self.assertTrue(invoke._auth_related("ERROR: authentication failed or timed out"))
        self.assertTrue(invoke._auth_related("You are not logged into Antigravity."))
        self.assertFalse(invoke._auth_related("input token count exceeds maximum context length"))
        self.assertFalse(invoke._auth_related("cannot read /srv/author/x.py"))

    def test_permissions_note(self):
        before = {"allow": ["command(rg)"], "deny": ["command(curl)"]}
        same = {"allow": ["command(rg)"], "deny": ["command(curl)"]}
        self.assertIsNone(invoke.permissions_note(before, same, []))
        self.assertIsNone(invoke.permissions_note(None, same, []))
        after = {"allow": ["command(rg)", "command(git log)"], "deny": ["command(python)"]}
        refused = [
            {"tool": "run_command", "target": "git log -1"},
            {"tool": "run_command", "target": "curl x"},
            {"tool": "run_command", "target": "python x.py"},
            {"tool": "write_to_file", "target": "/r/c"},
        ]
        note = invoke.permissions_note(before, after, refused)
        assert note is not None
        self.assertTrue(
            note.startswith("[cairn invoke] Shell permissions since your previous round:")
        )
        self.assertIn("- newly allowed: command(git log)", note)
        self.assertIn("- newly denied: command(python)", note)
        self.assertIn("- no longer denied: command(curl)", note)
        self.assertIn("- previously refused `git log -1`: now allowed", note)
        self.assertIn(
            "- previously refused `curl x`: still has no allow rule; do not retry it", note
        )
        self.assertIn(
            "- previously refused `python x.py`: still denied by command(python); do not retry it",
            note,
        )
        self.assertIn("- previously refused: write_to_file /r/c; it stays refused", note)
        self.assertTrue(note.endswith("A refused call is never retried by rephrasing it."))
        # A refused read is judged against the roots and the read_file rules now in force.
        reads = [
            {"tool": "read_file", "target": "/r/plan.md"},
            {"tool": "read_file", "target": "/r/.env"},
            {"tool": "read_file", "target": "/w/src/a.py"},
            {"tool": "read_file", "target": "/docs/x.md"},
        ]
        rules = {
            "allow": ["read_file(/docs)"],
            "deny": ["read_file(.env*)"],
            "ask": ["read_file(/w/src/b.py)"],
            "roots": ["/w"],
        }
        note = invoke.permissions_note(before, rules, reads)
        assert note is not None
        self.assertIn(
            "- previously refused read_file `/r/plan.md`: still outside the workspace with no "
            "read_file rule; do not retry it, ask for its text instead",
            note,
        )
        self.assertIn(
            "- previously refused read_file `/r/.env`: still denied by read_file(.env*); do not",
            note,
        )
        self.assertIn(
            "- previously refused read_file `/w/src/a.py`: now readable (inside /w)", note
        )
        asked = invoke.permissions_note(
            before, rules, [{"tool": "read_file", "target": "/w/src/b.py"}]
        )
        assert asked is not None
        self.assertIn(
            "still denied by read_file(/w/src/b.py) (an ask rule; headless agy cannot ask)", asked
        )
        self.assertIn(
            "- previously refused read_file `/docs/x.md`: now readable (read_file(/docs))", note
        )
        # Nothing changed but something was refused: the verdicts alone are the note.
        only = invoke.permissions_note(before, same, refused[:2])
        assert only is not None
        self.assertNotIn("newly", only)
        self.assertIn("`git log -1`: still has no allow rule", only)
        self.assertIn("`curl x`: still denied by command(curl)", only)
        # A first round has no previous rules to diff against, even with refusals recorded.
        first = invoke.permissions_note(None, after, refused[:1])
        assert first is not None
        self.assertNotIn("newly allowed", first)


class TestFollowCursor(unittest.TestCase):
    def test_cursor_is_absolute_past_the_cap(self):
        self.assertEqual(invoke.unseen_progress(["a", "b"], 0, 0), (["a", "b"], 2))
        self.assertEqual(invoke.unseen_progress(["a", "b", "c"], 0, 2), (["c"], 3))
        window = [f"item {n}" for n in range(5, 205)]  # 205 seen in total, 5 dropped
        fresh, cursor = invoke.unseen_progress(window, 5, 200)
        self.assertEqual(fresh, [f"item {n}" for n in range(200, 205)])
        self.assertEqual(cursor, 205)
        self.assertEqual(invoke.unseen_progress(window, 5, 205), ([], 205))


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

    def test_zombies_are_not_alive(self):
        child = subprocess.Popen(["true"])
        try:
            # Not reaped yet: kill(pid, 0) still succeeds, but it is a zombie.
            deadline = time.monotonic() + 10
            while process_identity(child.pid) is not None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIsNone(process_identity(child.pid))
            os.kill(child.pid, 0)  # the pid still exists
        finally:
            child.wait()

    def test_start_token_parsers(self):
        stat = (
            "1234 (my (odd) cmd) S 1 1234 1234 0 -1 4194560 " + " ".join(["0"] * 12) + " 98765 rest"
        )
        self.assertEqual(parse_linux_stat(stat, "1700000000"), "1700000000:98765")
        zombie = stat.replace(") S ", ") Z ")
        self.assertIsNone(parse_linux_stat(zombie, "1700000000"))
        self.assertIsNone(parse_linux_stat(stat.replace(") S ", ") X "), "1"))
        self.assertNotEqual(
            parse_linux_stat(stat, "1"), parse_linux_stat(stat.replace("98765", "98766"), "1")
        )
        self.assertEqual(
            parse_ps_line("Ss   Sat Oct 17 16:51:32 2026"),
            str(int(datetime(2026, 10, 17, 16, 51, 32).timestamp())),
        )
        self.assertIsNone(parse_ps_line("Z+   Sat Oct 17 16:51:32 2026"))

    def test_heartbeat_takes_newest_source(self):
        beat = heartbeat(None, None, "2026-10-17T10:00:00+00:00", "x")
        self.assertEqual(beat, Heartbeat(1_792_231_200.0, "thread.json", "x"))
        self.assertEqual(heartbeat(None, None, None).source, "none")


if __name__ == "__main__":
    unittest.main()
