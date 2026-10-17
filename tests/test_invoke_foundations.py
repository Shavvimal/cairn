"""Foundations of ``cairn invoke``: links, invoke config, export_and_index, harness adapters."""

from __future__ import annotations

import io
import json
import os
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cairn import config, hooks, qmd
from cairn.frontmatter import parse_frontmatter, preserved_tail, rewrite_preserved_tail
from cairn.harnesses import Event, InvokeRequest, get_harness, is_client_error
from cairn.harnesses.claude import KEYCHAIN_GUIDANCE, KEYCHAIN_MARKERS, keychain_failure
from cairn.harnesses.codex_models import (
    check_codex_invocation,
    format_models,
    load_codex_model_catalog,
)
from cairn.mutations import LINK_ROLES, add_link
from tests.test_shared_store import StoreFixture

DOC = """---
type: claude-code
date: 2026-10-17
session_id: abc
title: "T"
status: active
tags: []
rating: null
comments: ""
projects: []
---

# T

body links: none
"""

# Vendored from a real codex-cli 0.160.0 `codex exec --json` run (2026-10-17).
CODEX_STREAM = [
    '{"type":"thread.started","thread_id":"01a14a82-ed01-7442-aeff-ffbfd8296c88"}',
    '{"type":"item.completed","item":{"id":"item_0","type":"error","message":"`[features].'
    'codex_hooks` is deprecated. Use `[features].hooks` instead."}}',
    '{"type":"item.completed","item":{"id":"item_1","type":"error","message":"loading hooks '
    "from both /Users/x/.codex/hooks.json and /Users/x/.codex/config.toml; prefer a single "
    'representation for this layer"}}',
    '{"type":"turn.started"}',
    '{"type":"item.started","item":{"id":"item_3","type":"command_execution","command":'
    '"/bin/zsh -lc \'cat a.txt\'","aggregated_output":"","exit_code":null,"status":'
    '"in_progress"}}',
    '{"type":"item.completed","item":{"id":"item_3","type":"command_execution","command":'
    '"/bin/zsh -lc \'cat a.txt\'","aggregated_output":"hi\\n","exit_code":0,"status":'
    '"completed"}}',
    '{"type":"item.completed","item":{"id":"item_4","type":"agent_message","text":"hi"}}',
    '{"type":"turn.completed","usage":{"input_tokens":39817,"cached_input_tokens":19712,'
    '"output_tokens":67}}',
]

CLAUDE_STREAM = [
    '{"type":"system","subtype":"init","session_id":"5f0c","model":"claude-opus-5-5","tools":[]}',
    '{"type":"system","subtype":"hook_response"}',
    '{"type":"rate_limit_event"}',
    '{"type":"assistant","message":{"content":[{"type":"text","text":"Looking."},'
    '{"type":"tool_use","name":"Read","input":{"file_path":"/x/a.py"}}]}}',
    '{"type":"user","message":{"content":[{"type":"tool_result","content":"..."}]}}',
    '{"type":"assistant","message":{"content":[{"type":"text","text":"PONG"}]}}',
    '{"type":"result","subtype":"success","is_error":false,"result":"PONG",'
    '"duration_ms":1234,"total_cost_usd":0.01,"num_turns":2}',
]

MODELS_CACHE = {
    "fetched_at": "2026-10-17T00:00:00Z",
    "models": [
        {
            "slug": "gpt-6-astra",
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": [
                {"effort": "high"},
                {"effort": "low"},
                {"effort": "medium"},
            ],
            "visibility": "list",
            "additional_speed_tiers": ["fast"],
        },
        {
            "slug": "gpt-old",
            "default_reasoning_level": "low",
            "supported_reasoning_levels": [{"effort": "low"}],
            "visibility": "hide",
        },
    ],
}


def _request(peer: str, **overrides) -> InvokeRequest:
    base = InvokeRequest(
        peer=peer,
        prompt="Review this",
        cwd=Path("/work/repo"),
        profile="review",
        native_policy=(),
        model=None,
        effort=None,
        fast=False,
        timeout_s=1800,
        resume=None,
        session_id=None,
        output_schema=None,
        last_message_path=None,
        prompt_via_stdin=False,
        extra=(),
    )
    return replace(base, **overrides)


# ---------------------------------------------------------------------------
# links: preserved tail key
# ---------------------------------------------------------------------------


class TestLinksTail(unittest.TestCase):
    def test_omitted_when_empty(self):
        self.assertNotIn("links:", "\n".join(preserved_tail({"links": []})))
        self.assertNotIn("links:", "\n".join(preserved_tail(None)))

    def test_round_trips_through_rewrite(self):
        out = rewrite_preserved_tail(DOC, lambda fm: fm.__setitem__("links", ["parent codex:t1"]))
        assert out is not None
        self.assertIn("projects: []\nlinks:\n  - parent codex:t1\n---\n", out)
        self.assertEqual(parse_frontmatter(out)["links"], ["parent codex:t1"])
        self.assertIn("body links: none", out)
        again = rewrite_preserved_tail(out, lambda fm: None)
        self.assertEqual(again, out)


class TestAddLink(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "doc.md"
        self.path.write_text(DOC)
        self.find = lambda _sid: self.path

    def add(self, role, ref):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            ok = add_link(self.find, "abc", role, ref)
        return ok, out.getvalue(), err.getvalue()

    def test_adds_and_is_idempotent(self):
        ok, out, _ = self.add("child", "codex:019a-thread")
        self.assertTrue(ok)
        self.assertIn("Added link: child codex:019a-thread", out)
        ok, out, _ = self.add("child", "codex:019a-thread")
        self.assertTrue(ok)
        self.assertEqual(out, "")
        fm = parse_frontmatter(self.path.read_text())
        self.assertEqual(fm["links"], ["child codex:019a-thread"])

    def test_rejects_invalid_role_and_ref(self):
        self.assertEqual(LINK_ROLES, frozenset({"parent", "child", "peer"}))
        for role, ref in (
            ("sibling", "codex:x"),
            ("parent", "slack:x"),
            ("parent", "codex:"),
            ("parent", "codex:a b"),
        ):
            ok, _, err = self.add(role, ref)
            self.assertFalse(ok, (role, ref))
            self.assertIn("Error", err)
        self.assertEqual(self.path.read_text(), DOC)

    def test_strips_newlines(self):
        ok, _, _ = self.add("peer", "claude:abc\n")
        self.assertTrue(ok)
        self.assertEqual(parse_frontmatter(self.path.read_text())["links"], ["peer claude:abc"])


class TestLinksSurviveReexport(StoreFixture):
    def test_link_survives_reexport(self):
        cfg = config.get_config()
        sid = "019a0000-0000-7000-8000-00000000link"
        native = self.native("codex", sid)
        doc = hooks.export_and_index(cfg, "codex", sid, str(native), update_index=False)
        self.assertTrue(doc.is_file())
        with redirect_stdout(io.StringIO()):
            self.assertTrue(add_link(lambda _sid: doc, sid, "parent", "claude:caller-1"))
        with native.open("a") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "event_msg",
                        "timestamp": "2026-03-30T01:00:00Z",
                        "payload": {"type": "user_message", "message": "follow-up"},
                    }
                )
                + "\n"
            )
        later = time.time() + 5
        os.utime(native, (later, later))
        doc2 = hooks.export_and_index(cfg, "codex", sid, str(native), update_index=False)
        content = doc2.read_text()
        self.assertIn("follow-up", content)
        self.assertEqual(parse_frontmatter(content)["links"], ["parent claude:caller-1"])


# ---------------------------------------------------------------------------
# hooks.export_and_index
# ---------------------------------------------------------------------------


class TestExportAndIndex(StoreFixture):
    def test_update_index_false_exports_without_qmd(self):
        sid = "019a0000-0000-7000-8000-0000000noidx"
        native = self.native("codex", sid)
        with patch("cairn.hooks.ensure_collection") as ensure:
            doc = hooks.export_and_index(
                config.get_config(), "codex", sid, str(native), update_index=False
            )
        ensure.assert_not_called()
        self.assertTrue(doc.is_file())
        self.assertEqual(parse_frontmatter(doc.read_text())["session_id"], sid)

    def test_busy_index_lock_times_out(self):
        sid = "019a0000-0000-7000-8000-00000000busy"
        native = self.native("codex", sid)
        started = time.monotonic()
        with qmd.locked_index(), self.assertRaisesRegex(RuntimeError, "index busy"):
            hooks.export_and_index(
                config.get_config(), "codex", sid, str(native), lock_timeout_s=0.3
            )
        self.assertLess(time.monotonic() - started, 60)
        # The document is on disk even though indexing was skipped.
        self.assertTrue(any((self.root / "data/codex-sessions").rglob("*.md")))


# ---------------------------------------------------------------------------
# Codex adapter
# ---------------------------------------------------------------------------


class TestCodexArgv(unittest.TestCase):
    codex = get_harness("codex")

    def test_fresh_full(self):
        argv = self.codex.exec_argv(
            _request(
                "codex",
                model="gpt-6-astra",
                effort="high",
                fast=True,
                output_schema=Path("/s.json"),
                last_message_path=Path("/run/answer-01.md"),
                extra=("--foo",),
            )
        )
        self.assertEqual(
            argv,
            [
                "codex",
                "exec",
                "--json",
                "--skip-git-repo-check",
                "-s",
                "read-only",
                "-c",
                'approval_policy="never"',
                "-m",
                "gpt-6-astra",
                "-c",
                'model_reasoning_effort="high"',
                "--enable",
                "fast_mode",
                "-c",
                'service_tier="priority"',
                "-C",
                "/work/repo",
                "--output-schema",
                "/s.json",
                "-o",
                "/run/answer-01.md",
                "--foo",
                "Review this",
            ],
        )

    def test_fresh_minimal_profiles_and_stdin(self):
        for profile, mode in (
            ("review", "read-only"),
            ("edit", "workspace-write"),
            ("full", "danger-full-access"),
        ):
            argv = self.codex.exec_argv(_request("codex", profile=profile, prompt_via_stdin=True))
            self.assertEqual(
                argv,
                [
                    "codex",
                    "exec",
                    "--json",
                    "--skip-git-repo-check",
                    "-s",
                    mode,
                    "-c",
                    'approval_policy="never"',
                    "-C",
                    "/work/repo",
                    "-",
                ],
            )
        self.assertNotIn("--enable", argv)

    def test_resume(self):
        argv = self.codex.exec_argv(
            _request(
                "codex",
                resume="019a-thread",
                profile="edit",
                model="gpt-6-astra",
                effort="low",
                last_message_path=Path("/run/answer-02.md"),
            )
        )
        self.assertEqual(
            argv,
            [
                "codex",
                "exec",
                "resume",
                "--json",
                "--skip-git-repo-check",
                "-c",
                'sandbox_mode="workspace-write"',
                "-c",
                'approval_policy="never"',
                "-m",
                "gpt-6-astra",
                "-c",
                'model_reasoning_effort="low"',
                "-o",
                "/run/answer-02.md",
                "019a-thread",
                "Review this",
            ],
        )
        self.assertNotIn("-C", argv)
        self.assertNotIn("-s", argv)

    def test_resume_native_sandbox_and_stdin(self):
        argv = self.codex.exec_argv(
            _request(
                "codex",
                resume="t",
                profile="full",
                native_policy=("-s", "read-only"),
                prompt_via_stdin=True,
            )
        )
        self.assertIn('sandbox_mode="read-only"', argv)
        self.assertEqual(argv[-2:], ["t", "-"])

    def test_policy_argv(self):
        self.assertEqual(self.codex.policy_argv("edit"), ("-s", "workspace-write"))
        self.assertEqual(
            self.codex.policy_argv("review", ("-s", "danger-full-access")),
            ("-s", "danger-full-access"),
        )
        with self.assertRaises(ValueError):
            self.codex.policy_argv("bogus")


class TestCodexEvents(unittest.TestCase):
    codex = get_harness("codex")

    def parse_all(self, lines):
        return [event for line in lines for event in self.codex.parse_event(line)]

    def test_vendored_stream(self):
        events = self.parse_all(CODEX_STREAM)
        self.assertEqual(
            [e.kind for e in events],
            ["thread_started", "notice", "notice", "progress", "progress", "message", "done"],
        )
        self.assertEqual(events[0].thread_id, "01a14a82-ed01-7442-aeff-ffbfd8296c88")
        self.assertIn("deprecated", events[1].text or "")
        self.assertEqual(events[3].text, "run: /bin/zsh -lc 'cat a.txt'")
        self.assertEqual(events[4].text, "exit 0: /bin/zsh -lc 'cat a.txt'")
        self.assertEqual(events[5].text, "hi")
        self.assertEqual(events[6].status, "succeeded")
        self.assertEqual((events[6].usage or {})["output_tokens"], 67)
        # The deprecation error items never become a failure.
        self.assertFalse(any(e.kind == "done" and e.status == "failed" for e in events))

    def test_other_items(self):
        cases = {
            '{"type":"item.completed","item":{"type":"file_change","changes":'
            '[{"path":"a.py","kind":"update"}]}}': Event(
                "progress", text="edit: update a.py", raw_type="item.completed:file_change"
            ),
            '{"type":"item.updated","item":{"type":"todo_list","items":'
            '[{"text":"a","completed":true},{"text":"b","completed":false}]}}': Event(
                "progress", text="plan: [x] a; [ ] b", raw_type="item.updated:todo_list"
            ),
            '{"type":"item.completed","item":{"type":"reasoning","text":"' + "x" * 300 + '"}}': (
                Event(
                    "progress",
                    text="thinking: " + "x" * 197 + "...",
                    raw_type="item.completed:reasoning",
                )
            ),
            '{"type":"item.completed","item":{"type":"web_search","query":"flock"}}': Event(
                "progress", text="web_search: flock", raw_type="item.completed:web_search"
            ),
            '{"type":"item.completed","item":{"type":"mcp_tool_call","server":"qmd",'
            '"tool":"search"}}': Event(
                "progress",
                text="mcp_tool_call: qmd.search",
                raw_type="item.completed:mcp_tool_call",
            ),
        }
        for line, expected in cases.items():
            self.assertEqual(self.codex.parse_event(line), [expected], line)

    def test_failures_and_noise(self):
        failed = self.codex.parse_event('{"type":"turn.failed","error":{"message":"boom"}}')
        self.assertEqual(
            failed, [Event("done", status="failed", error="boom", raw_type="turn.failed")]
        )
        top = self.codex.parse_event('{"type":"error","message":"stream closed"}')
        self.assertEqual(
            (top[0].kind, top[0].status, top[0].error), ("done", "failed", "stream closed")
        )
        self.assertEqual(self.codex.parse_event('{"type":"turn.started"}'), [])
        self.assertEqual(self.codex.parse_event('{"type":"something.new"}'), [])
        self.assertEqual(self.codex.parse_event(""), [])
        noise = self.codex.parse_event("Reading additional input from stdin...")
        self.assertEqual(noise[0].kind, "notice")
        self.assertTrue((noise[0].text or "").startswith("non-JSON stdout: "))


class TestCodexTranscriptPath(unittest.TestCase):
    def test_day_folder_previous_day_and_scan(self):
        codex = get_harness("codex")
        with TemporaryDirectory() as tmp:
            store = Path(tmp)
            tid = "019a-abc"
            started = datetime(2026, 10, 17, 0, 30).astimezone()
            self.assertIsNone(codex.transcript_path(store, tid, started))
            prev = store / "2026/10/16"
            prev.mkdir(parents=True)
            prev_file = prev / f"rollout-2026-10-16T23-59-00-{tid}.jsonl"
            prev_file.write_text("{}\n")
            self.assertEqual(codex.transcript_path(store, tid, started), prev_file)
            today = store / "2026/10/17"
            today.mkdir(parents=True)
            today_file = today / f"rollout-2026-10-17T00-30-00-{tid}.jsonl"
            today_file.write_text("{}\n")
            self.assertEqual(codex.transcript_path(store, tid, started), today_file)
            old = store / "2025/01/01"
            old.mkdir(parents=True)
            old_file = old / "rollout-2025-01-01T00-00-00-other.jsonl"
            old_file.write_text("{}\n")
            self.assertEqual(codex.transcript_path(store, "other", started), old_file)
            self.assertIsNone(codex.transcript_path(store / "missing", tid, None))


class TestCodexSelfCheck(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        env = patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.codex = get_harness("codex")

    def write_cache(self):
        (self.home / "models_cache.json").write_text(json.dumps(MODELS_CACHE))

    def test_missing_cache_is_a_problem(self):
        problems = self.codex.self_check("gpt-6-astra", "high", False)
        self.assertEqual(len(problems), 1)
        self.assertIn("no Codex model cache at", problems[0])
        self.assertIn("--skip-self-check", problems[0])

    def test_cases(self):
        self.write_cache()
        self.assertEqual(self.codex.self_check("gpt-6-astra", "high", True), [])
        self.assertEqual(self.codex.self_check(None, None, False), [])
        unknown = self.codex.self_check("gpt-9", None, False)
        self.assertIn("available: gpt-6-astra", unknown[0])
        self.assertNotIn("gpt-old", unknown[0])
        effort = self.codex.self_check("gpt-6-astra", "ultra", False)
        self.assertIn("supported: low, medium, high", effort[0])
        fast = self.codex.self_check("gpt-old", None, True)
        self.assertIn("fast-capable: gpt-6-astra", fast[0])

    def test_catalog_parsing(self):
        self.write_cache()
        catalog = load_codex_model_catalog()
        self.assertTrue(catalog["gpt-6-astra"].fast)
        self.assertFalse(catalog["gpt-old"].visible)
        self.assertFalse(catalog["gpt-old"].fast)
        self.assertEqual(load_codex_model_catalog(self.home / "nope.json"), {})
        self.assertEqual(
            check_codex_invocation(None, None, True, {}),
            ["fast mode requested but no model in the Codex cache supports it"],
        )

    def test_format_models(self):
        self.write_cache()
        catalog = load_codex_model_catalog()
        text = format_models(catalog, "gpt-6-astra", as_json=False)
        self.assertEqual(len(text.splitlines()), 1)
        self.assertIn("gpt-6-astra", text)
        self.assertIn("efforts=low,medium,high", text)
        self.assertIn("default_effort=medium", text)
        self.assertTrue(text.endswith("[DEFAULT fast]"))
        rows = json.loads(format_models(catalog, "gpt-6-astra", as_json=True))
        self.assertEqual(
            rows,
            [
                {
                    "slug": "gpt-6-astra",
                    "efforts": ["low", "medium", "high"],
                    "default_effort": "medium",
                    "visible": True,
                    "fast": True,
                    "default": True,
                },
                {
                    "slug": "gpt-old",
                    "efforts": ["low"],
                    "default_effort": "low",
                    "visible": False,
                    "fast": False,
                    "default": False,
                },
            ],
        )


# ---------------------------------------------------------------------------
# Claude adapter
# ---------------------------------------------------------------------------


class TestClaudeArgv(unittest.TestCase):
    claude = get_harness("claude")

    def test_fresh_with_schema(self):
        with TemporaryDirectory() as tmp:
            schema = Path(tmp) / "schema.json"
            schema.write_text('{"type":"object"}')
            argv = self.claude.exec_argv(
                _request(
                    "claude",
                    session_id="5f0c",
                    model="opus",
                    effort="high",
                    output_schema=schema,
                    extra=("--add-dir", "/x"),
                )
            )
        self.assertEqual(
            argv,
            [
                "claude",
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--session-id",
                "5f0c",
                "--permission-mode",
                "plan",
                "--disallowedTools",
                "Edit,Write,NotebookEdit",
                "--model",
                "opus",
                "--effort",
                "high",
                "--json-schema",
                '{"type":"object"}',
                "--add-dir",
                "/x",
                "Review this",
            ],
        )

    def test_profiles_and_stdin(self):
        edit = self.claude.exec_argv(
            _request("claude", session_id="s", profile="edit", prompt_via_stdin=True)
        )
        self.assertEqual(
            edit,
            [
                "claude",
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--session-id",
                "s",
                "--permission-mode",
                "acceptEdits",
            ],
        )
        full = self.claude.exec_argv(_request("claude", session_id="s", profile="full"))
        self.assertEqual(full[-3:], ["--permission-mode", "bypassPermissions", "Review this"])

    def test_resume(self):
        argv = self.claude.exec_argv(_request("claude", resume="5f0c", model="sonnet"))
        self.assertEqual(
            argv,
            [
                "claude",
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--resume",
                "5f0c",
                "--permission-mode",
                "plan",
                "--disallowedTools",
                "Edit,Write,NotebookEdit",
                "--model",
                "sonnet",
                "Review this",
            ],
        )
        self.assertNotIn("--session-id", argv)

    def test_fresh_needs_session_id(self):
        with self.assertRaises(ValueError):
            self.claude.exec_argv(_request("claude"))

    def test_policy_argv_native_override(self):
        self.assertEqual(
            self.claude.policy_argv("review", ("--permission-mode", "dontAsk")),
            ("--permission-mode", "dontAsk"),
        )
        self.assertEqual(self.claude.policy_argv("edit"), ("--permission-mode", "acceptEdits"))
        with self.assertRaises(ValueError):
            self.claude.policy_argv("bogus")


class TestClaudeEvents(unittest.TestCase):
    claude = get_harness("claude")

    def test_vendored_stream(self):
        events = [e for line in CLAUDE_STREAM for e in self.claude.parse_event(line)]
        self.assertEqual(
            [e.kind for e in events], ["thread_started", "message", "progress", "message", "done"]
        )
        self.assertEqual((events[0].thread_id, events[0].text), ("5f0c", "claude-opus-5-5"))
        self.assertEqual(events[1].text, "Looking.")
        self.assertEqual(events[2].text, 'tool: Read {"file_path": "/x/a.py"}')
        done = events[-1]
        self.assertEqual((done.status, done.text, done.error), ("succeeded", "PONG", None))
        self.assertEqual(done.usage, {"duration_ms": 1234, "total_cost_usd": 0.01, "num_turns": 2})

    def test_failed_result_and_noise(self):
        failed = self.claude.parse_event(
            '{"type":"result","subtype":"error_max_turns","is_error":true,"result":"stopped"}'
        )[0]
        self.assertEqual((failed.status, failed.error), ("failed", "error_max_turns: stopped"))
        is_error = self.claude.parse_event(
            '{"type":"result","subtype":"success","is_error":true,"result":"Not logged in"}'
        )[0]
        self.assertEqual(is_error.status, "failed")
        self.assertTrue(keychain_failure(is_error.text or ""))
        self.assertEqual(self.claude.parse_event("garbage")[0].kind, "notice")

    def test_keychain_and_client_error_markers(self):
        self.assertIn("Not logged in", KEYCHAIN_MARKERS)
        self.assertIn("keychain", KEYCHAIN_GUIDANCE)
        self.assertTrue(keychain_failure("Error: not authenticated"))
        self.assertFalse(keychain_failure("all good"))
        self.assertTrue(is_client_error('{"status":400,"error":"bad"}'))
        self.assertTrue(is_client_error("Invalid_Request_Error: model"))
        self.assertFalse(is_client_error("connection reset"))


class TestClaudeTranscriptAndSelfCheck(unittest.TestCase):
    claude = get_harness("claude")

    def test_transcript_path(self):
        with TemporaryDirectory() as tmp:
            store = Path(tmp)
            self.assertIsNone(self.claude.transcript_path(store, "5f0c", None))
            project = store / "-Users-x-repo"
            project.mkdir()
            path = project / "5f0c.jsonl"
            path.write_text("{}\n")
            self.assertEqual(self.claude.transcript_path(store, "5f0c", None), path)

    def test_self_check(self):
        self.assertEqual(self.claude.self_check("any-model", "max", False), [])
        self.assertEqual(self.claude.self_check(None, None, False), [])
        self.assertIn("not supported by claude", self.claude.self_check(None, "ultra", False)[0])
        self.assertEqual(self.claude.self_check(None, None, True), ["fast mode is Codex-only"])


if __name__ == "__main__":
    unittest.main()
