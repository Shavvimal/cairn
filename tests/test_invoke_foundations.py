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
from typing import ClassVar
from unittest.mock import patch

from cairn import config, hooks, qmd
from cairn.frontmatter import parse_frontmatter, preserved_tail, rewrite_preserved_tail
from cairn.harnesses import Event, InvokeRequest, get_harness, is_client_error
from cairn.harnesses.antigravity import (
    MODE_BY_PROFILE,
    billing_env,
    billing_problem,
    billing_problems,
    observe_billing,
    resolved_project,
    transcript_conversation_id,
)
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
        # add_link takes a document lock under $HOME; keep it in the temp dir.
        home = patch.dict(os.environ, {"HOME": str(Path(tmp.name) / "home")})
        home.start()
        self.addCleanup(home.stop)

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
                "--",
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
        self.assertEqual(full[-4:], ["--permission-mode", "bypassPermissions", "--", "Review this"])

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
                "--",
                "Review this",
            ],
        )
        self.assertNotIn("--session-id", argv)

    def test_prompt_is_never_swallowed_by_variadic_flags(self):
        # --disallowedTools is variadic in claude 2.1.301; without "--" the review
        # profile's default argv consumed the prompt (verified live).
        argv = self.claude.exec_argv(_request("claude", session_id="s"))
        self.assertEqual(argv[-2:], ["--", "Review this"])
        stdin = self.claude.exec_argv(_request("claude", session_id="s", prompt_via_stdin=True))
        self.assertNotIn("--", stdin)

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


# ---------------------------------------------------------------------------
# Antigravity adapter
# ---------------------------------------------------------------------------

# Vendored from real agy 1.4.6 `agy --output-format stream-json -p=...` runs
# (2026-10-23), with the tool list and paths shortened.
AGY_STREAM = [
    '{"event":"init","conversation_id":"236f2e5b-0d6f-4fe5-9faa-738226a9c8b8","init":{"cwd":'
    '"/w","tools":["read_file","write_file"],"permission_mode":"request-review"}}',
    '{"event":"step_update","step_update":{"conversation_id":"236f2e5b","step_index":0,'
    '"state":"DONE","step_type":"user_input"}}',
    '{"event":"step_update","step_update":{"conversation_id":"236f2e5b","step_index":1,'
    '"state":"ACTIVE","step_type":"agent_response","text_delta":"I will start by reading "}}',
    '{"event":"step_update","step_update":{"conversation_id":"236f2e5b","step_index":1,'
    '"state":"ACTIVE","step_type":"agent_response","text_delta":"a.txt."}}',
    '{"event":"step_update","step_update":{"conversation_id":"236f2e5b","step_index":1,'
    '"state":"DONE","step_type":"agent_response","text_delta":"\\n","duration_seconds":22.1,'
    '"usage":{"input_tokens":7760,"output_tokens":1661,"thinking_tokens":1503,'
    '"cache_read_tokens":0,"total_tokens":9421}}}',
    '{"event":"step_update","step_update":{"conversation_id":"236f2e5b","step_index":2,'
    '"state":"ACTIVE","step_type":"tool","tool_name":"read_file","tool_info":{"name":'
    '"read_file","parameters":{"Path":"/w/a.txt"}}}}',
    '{"event":"step_update","step_update":{"conversation_id":"236f2e5b","step_index":2,'
    '"state":"DONE","step_type":"tool","tool_name":"read_file","duration_seconds":0.08,'
    '"tool_info":{"name":"read_file","parameters":{"Path":"/w/a.txt"},"output":"1 lines"}}}',
    '{"event":"step_update","step_update":{"conversation_id":"236f2e5b","step_index":3,'
    '"state":"DONE","step_type":"system_message","duration_seconds":0.0001}}',
    '{"event":"step_update","step_update":{"conversation_id":"236f2e5b","step_index":4,'
    '"state":"DONE","step_type":"agent_response","duration_seconds":22.3}}',
    '{"event":"step_update","step_update":{"conversation_id":"236f2e5b","step_index":5,'
    '"state":"ACTIVE","step_type":"agent_response","text_delta":"pong"}}',
    '{"event":"step_update","step_update":{"conversation_id":"236f2e5b","step_index":5,'
    '"state":"DONE","step_type":"agent_response","text_delta":"\\n","duration_seconds":3.2}}',
    '{"event":"result","result":{"conversation_id":"236f2e5b-0d6f-4fe5-9faa-738226a9c8b8",'
    '"status":"SUCCESS","response":"I will start by reading a.txt.\\npong\\n",'
    '"duration_seconds":3.24,"num_turns":1,"usage":{"input_tokens":6930,"output_tokens":27,'
    '"thinking_tokens":22,"cache_read_tokens":0,"total_tokens":6957}}}',
]
# agy 1.4.6 with --model bogus-model: no init, an ERROR result, exit 1 (verified live).
AGY_BAD_MODEL = (
    '{"event":"result","result":{"conversation_id":"","status":"ERROR","response":"",'
    '"error":"invalid model selection (--model \\"bogus-model\\" --effort \\"\\"): model '
    'bogus-model is not recognized as a known model or custom model in settings",'
    '"duration_seconds":0,"num_turns":0,"usage":{"input_tokens":0,"output_tokens":0,'
    '"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":0}}}'
)


class TestAntigravityArgv(unittest.TestCase):
    agy = get_harness("antigravity")
    HEAD: ClassVar[list[str]] = [
        "agy",
        "--output-format",
        "stream-json",
        "--print-timeout",
        "0",
        "--disable-slash-commands",
    ]

    def test_fresh_with_schema_model_effort_and_extra(self):
        with TemporaryDirectory() as tmp:
            schema = Path(tmp) / "schema.json"
            schema.write_text('{"type":"object"}')
            argv = self.agy.exec_argv(
                _request(
                    "antigravity",
                    model="gemini-3.1-pro-high",
                    effort="high",
                    output_schema=schema,
                    extra=("--add-dir", "/x"),
                )
            )
        self.assertEqual(
            argv,
            [
                *self.HEAD,
                "--mode",
                "plan",
                "--model",
                "gemini-3.1-pro-high",
                "--effort",
                "high",
                "--json-schema",
                str(schema),
                "--add-dir",
                "/x",
                "-p=Review this",
            ],
        )

    def test_profiles_native_override_and_stdin(self):
        edit = self.agy.exec_argv(_request("antigravity", profile="edit", prompt_via_stdin=True))
        self.assertEqual(
            edit,
            [*self.HEAD, "--mode", "accept-edits", "--input-format", "stream-json", "-p="],
        )
        full = self.agy.exec_argv(_request("antigravity", profile="full"))
        self.assertEqual(full[6:], ["--dangerously-skip-permissions", "-p=Review this"])
        native = self.agy.exec_argv(_request("antigravity", native_policy=("--mode", "auto")))
        self.assertEqual(native[6:8], ["--mode", "auto"])
        self.assertEqual(self.agy.policy_argv("review"), MODE_BY_PROFILE["review"])
        with self.assertRaises(ValueError):
            self.agy.policy_argv("bogus")

    def test_resume_re_sends_the_policy(self):
        argv = self.agy.exec_argv(_request("antigravity", resume="236f", model="m"))
        self.assertEqual(
            argv,
            [
                *self.HEAD,
                "--mode",
                "plan",
                "--conversation",
                "236f",
                "--model",
                "m",
                "-p=Review this",
            ],
        )
        self.assertEqual(self.agy.resume("236f"), ["agy", "--conversation", "236f"])
        with self.assertRaises(ValueError):
            self.agy.resume("236f", fork=True)

    def test_stdin_payload_is_one_ndjson_user_message(self):
        payload = self.agy.stdin_payload("hello\nworld")
        self.assertTrue(payload.endswith(b"\n"))
        self.assertEqual(
            json.loads(payload), {"event": "user", "message": {"content": "hello\nworld"}}
        )
        self.assertEqual(payload.count(b"\n"), 1)
        for peer in ("claude", "codex"):
            self.assertEqual(get_harness(peer).stdin_payload("x y"), b"x y")


class TestAntigravityEvents(unittest.TestCase):
    def test_vendored_stream(self):
        agy = get_harness("antigravity")
        events = [e for line in AGY_STREAM for e in agy.parse_event(line)]
        self.assertEqual(
            [e.kind for e in events], ["thread_started", "message", "progress", "message", "done"]
        )
        self.assertEqual(events[0].thread_id, "236f2e5b-0d6f-4fe5-9faa-738226a9c8b8")
        self.assertEqual(events[1].text, "I will start by reading a.txt.")  # deltas joined
        self.assertEqual(events[2].text, 'tool: read_file {"Path": "/w/a.txt"}')
        self.assertEqual(events[3].text, "pong")
        done = events[-1]
        self.assertEqual(done.status, "succeeded")
        self.assertEqual(done.text, "I will start by reading a.txt.\npong")
        self.assertIsNone(done.error)
        assert done.usage is not None
        self.assertEqual(done.usage["num_turns"], 1)
        self.assertEqual(done.usage["total_tokens"], 6957)

    def test_tool_failure_and_subagents(self):
        agy = get_harness("antigravity")
        failed = agy.parse_event(
            '{"event":"step_update","step_update":{"step_index":7,"state":"DONE","step_type":'
            '"tool","tool_name":"run_command","tool_info":{"name":"run_command","parameters":'
            '{"CommandLine":"rm -rf /"},"error":{"type":"PERMISSION_DENIED","message":"denied"}}}}'
        )
        self.assertEqual(
            [e.text for e in failed],
            ['tool: run_command {"CommandLine": "rm -rf /"}', "tool failed: run_command: denied"],
        )
        sub = agy.parse_event(
            '{"event":"step_update","step_update":{"step_index":8,"state":"DONE","step_type":'
            '"tool","tool_name":"invoke_subagent","subagent_info":{"subagents":[{"type_name":'
            '"researcher","role":"worker","conversation_id":"c2"}]}}}'
        )
        self.assertEqual(sub[-1].text, "subagents: researcher (worker)")

    def test_bad_model_is_a_failed_done_and_a_client_error(self):
        agy = get_harness("antigravity")
        events = agy.parse_event(AGY_BAD_MODEL)
        self.assertEqual(len(events), 1)
        done = events[0]
        self.assertEqual((done.kind, done.status, done.text), ("done", "failed", None))
        assert done.error is not None
        self.assertTrue(done.error.startswith("ERROR: invalid model selection"))
        self.assertTrue(is_client_error(done.error))
        self.assertTrue(is_client_error("You are not logged into Antigravity."))
        other = agy.parse_event('{"event":"result","result":{"status":"INTERRUPTED"}}')[0]
        self.assertEqual(other.error, "INTERRUPTED: agy ended with status INTERRUPTED")
        self.assertEqual(agy.parse_event("garbage")[0].kind, "notice")
        self.assertEqual(agy.parse_event('{"event":"unknown"}'), [])

    def test_transcript_path_and_self_check(self):
        agy = get_harness("antigravity")
        with TemporaryDirectory() as tmp:
            store = Path(tmp)
            self.assertIsNone(agy.transcript_path(store, "236f", None))
            logs = store / "brain" / "236f" / ".system_generated" / "logs"
            logs.mkdir(parents=True)
            compact = logs / "transcript.jsonl"
            compact.write_text("{}\n")
            self.assertEqual(agy.transcript_path(store, "236f", None), compact)
            full = logs / "transcript_full.jsonl"
            full.write_text("{}\n")
            self.assertEqual(agy.transcript_path(store, "236f", None), full)  # full wins
            self.assertEqual(transcript_conversation_id(compact), "236f")
            self.assertIsNone(transcript_conversation_id(store / "x" / "transcript.jsonl"))
        self.assertEqual(agy.self_check("gemini-3.1-pro-high", "xhigh", False), [])
        self.assertEqual(agy.self_check(None, None, False), [])
        self.assertIn("not supported by antigravity", agy.self_check(None, "ultra", False)[0])
        self.assertEqual(agy.self_check(None, None, True), ["fast mode is Codex-only"])
        self.assertIsNone(agy.current_id())


class TestAntigravityBilling(unittest.TestCase):
    """The billing route: config parsing, the child environment, the pre-spawn check and
    the post-run reading of agy's log (line shapes vendored from agy 1.4.6)."""

    LOG = (
        "I1023 10:53:39.862890       1 server_oauth.go:224] applyAuthResult: "
        "email=s@x.com, authMethod=adc, quotaProject=gen-lang-client-0002376644\n"
        "I1023 10:53:43.468255      63 http_helpers.go:349] URL: https://aiplatform."
        "googleapis.com/v1/projects/gen-lang-client-0002376644/locations/global/publishers/"
        "google/models/gemini-3.1-flash-lite:streamGenerateContent?alt=sse ResponseID: a\n"
        "I1023 10:53:45.166799     433 http_helpers.go:349] URL: https://aiplatform."
        "googleapis.com/v1/projects/gen-lang-client-0002376644/locations/global/publishers/"
        "google/models/gemini-4-argon:streamGenerateContent?alt=sse ResponseID: b\n"
    )

    def test_config_parsing(self):
        policy = config._parse_invoke(
            {"antigravity": {"billing": {"route": "gcp", "project": "p"}}}
        )
        self.assertEqual(policy.antigravity.billing, config.BillingPolicy("gcp", "p", None))
        self.assertEqual(config._parse_invoke({}).antigravity.billing, config.BillingPolicy())
        self.assertIsNone(config._parse_invoke({}).codex.billing)
        for bad in (
            {"codex": {"billing": {"route": "gcp"}}},
            {"antigravity": {"billing": {"route": "free"}}},
            {"antigravity": {"billing": {"project": ""}}},
            {"antigravity": {"billing": {"bogus": 1}}},
            {"antigravity": {"billing": "gcp"}},
        ):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                config._parse_invoke(bad)

    def test_child_environment(self):
        host = {"PATH": "/bin", "AGY_ADC_AUTH": "true", "GOOGLE_CLOUD_QUOTA_PROJECT": "host-p"}
        self.assertEqual(billing_env(config.BillingPolicy(), host), host)
        plan = billing_env(config.BillingPolicy("plan"), host)
        self.assertNotIn("AGY_ADC_AUTH", plan)
        self.assertEqual(plan["GOOGLE_CLOUD_QUOTA_PROJECT"], "host-p")
        gcp = billing_env(config.BillingPolicy("gcp", "cfg-p", "eu"), {"PATH": "/bin"})
        self.assertEqual(
            gcp,
            {
                "PATH": "/bin",
                "AGY_ADC_AUTH": "true",
                "GOOGLE_CLOUD_QUOTA_PROJECT": "cfg-p",
                "GOOGLE_CLOUD_LOCATION": "eu",
            },
        )
        bare = billing_env(config.BillingPolicy("gcp"), host)
        self.assertEqual(bare["GOOGLE_CLOUD_QUOTA_PROJECT"], "host-p")  # untouched
        self.assertIsNot(bare, host)

    def test_pre_spawn_check_and_project_resolution(self):
        gcp = config.BillingPolicy("gcp")
        with TemporaryDirectory() as tmp:
            env = {"HOME": tmp}
            self.assertEqual(billing_problems(config.BillingPolicy(), env), [])
            self.assertEqual(billing_problems(config.BillingPolicy("plan"), env), [])
            missing = billing_problems(gcp, env)
            self.assertEqual(len(missing), 1)
            self.assertIn("application_default_credentials.json does not exist", missing[0])
            self.assertIn("gcloud auth application-default login", missing[0])
            adc = Path(tmp) / ".config/gcloud/application_default_credentials.json"
            adc.parent.mkdir(parents=True)
            adc.write_text("not json")
            self.assertIn("unreadable", billing_problems(gcp, env)[0])
            adc.write_text('{"type":"authorized_user"}')
            self.assertIn("no quota project", billing_problems(gcp, env)[0])
            self.assertIsNone(resolved_project(gcp, env))
            self.assertEqual(
                billing_problems(gcp, {**env, "GOOGLE_CLOUD_QUOTA_PROJECT": "env-p"}), []
            )
            self.assertEqual(billing_problems(config.BillingPolicy("gcp", "cfg-p"), env), [])
            adc.write_text('{"type":"authorized_user","quota_project_id":"adc-p"}')
            self.assertEqual(billing_problems(gcp, env), [])
            self.assertEqual(resolved_project(gcp, env), "adc-p")
            self.assertEqual(
                resolved_project(gcp, {**env, "GOOGLE_CLOUD_QUOTA_PROJECT": "env-p"}), "env-p"
            )
            self.assertEqual(resolved_project(config.BillingPolicy("gcp", "cfg-p"), env), "cfg-p")
            self.assertIsNone(resolved_project(config.BillingPolicy("plan"), env))
            # GOOGLE_APPLICATION_CREDENTIALS and CLOUDSDK_CONFIG relocate the file.
            elsewhere = Path(tmp) / "elsewhere.json"
            self.assertIn(
                "elsewhere.json",
                billing_problems(gcp, {**env, "GOOGLE_APPLICATION_CREDENTIALS": str(elsewhere)})[0],
            )
            self.assertIn(
                str(Path(tmp) / "sdk"),
                billing_problems(gcp, {**env, "CLOUDSDK_CONFIG": str(Path(tmp) / "sdk")})[0],
            )

    def test_log_observation_and_verification(self):
        with TemporaryDirectory() as tmp:
            log = Path(tmp) / "peer-01.log"
            self.assertEqual(
                observe_billing(log),
                {"auth_method": None, "quota_project": None, "vertex_projects": []},
            )
            log.write_text(self.LOG)
            observed = observe_billing(log)
        project = "gen-lang-client-0002376644"
        self.assertEqual(
            observed,
            {"auth_method": "adc", "quota_project": project, "vertex_projects": [project]},
        )
        self.assertIsNone(billing_problem("gcp", project, observed))
        self.assertIsNone(billing_problem("gcp", None, observed))
        self.assertIsNone(billing_problem("inherit", None, observed))
        self.assertIn("plan route requested", billing_problem("plan", None, observed) or "")
        self.assertIn(
            "quotaProject=gen-lang", billing_problem("gcp", "other-project", observed) or ""
        )
        consumer = {"auth_method": "gcp", "quota_project": project, "vertex_projects": []}
        self.assertIn("authMethod=gcp", billing_problem("gcp", project, consumer) or "")
        self.assertIsNone(billing_problem("plan", None, consumer))
        self.assertIn(
            "unverified",
            billing_problem("gcp", project, {"auth_method": None, "vertex_projects": []}) or "",
        )
        elsewhere = {**observed, "vertex_projects": [project, "leak-project"]}
        self.assertIn("requests went to", billing_problem("gcp", project, elsewhere) or "")

    def test_argv_carries_the_log_file(self):
        agy = get_harness("antigravity")
        argv = agy.exec_argv(_request("antigravity", log_path=Path("/r/.logs/x.log")))
        self.assertEqual(argv[argv.index("--log-file") + 1], "/r/.logs/x.log")
        self.assertNotIn("--log-file", agy.exec_argv(_request("antigravity")))


class TestAntigravityStopHook(unittest.TestCase):
    """agy's ``Stop`` payload (camelCase protojson, agy 1.4.6 embedded docs)."""

    PAYLOAD: ClassVar[dict] = {
        "conversationId": "236f2e5b-0d6f-4fe5-9faa-738226a9c8b8",
        "workspacePaths": ["/w/repo"],
        "transcriptPath": "/h/.gemini/antigravity-cli/brain/236f2e5b-0d6f-4fe5-9faa-738226a9c8b8/"
        ".system_generated/logs/transcript.jsonl",
        "artifactDirectoryPath": "/h/.gemini/antigravity-cli/brain/236f2e5b-0d6f-4fe5-9faa-738226a9c8b8",
        "modelName": "gemini-4-argon-high",
        "executionNum": 1,
        "terminationReason": "NO_TOOL_CALL",
        "error": "",
        "fullyIdle": True,
    }

    def test_decode(self):
        event = get_harness("antigravity").decode(self.PAYLOAD)
        self.assertEqual((event.harness, event.source), ("antigravity", "antigravity"))
        self.assertEqual(event.session_id, self.PAYLOAD["conversationId"])
        self.assertEqual(event.transcript_path, self.PAYLOAD["transcriptPath"])
        self.assertEqual(event.cwd, "/w/repo")
        self.assertEqual(event.event, "Stop")
        # No workspace: the app-data directory stands in for the cwd.
        bare = get_harness("antigravity").decode({**self.PAYLOAD, "workspacePaths": []})
        self.assertEqual(bare.cwd, "/h/.gemini/antigravity-cli")
        minimal = get_harness("antigravity").decode({"conversationId": "abc"})
        self.assertIsNone(minimal.transcript_path)

    def test_rejects_malformed_payloads(self):
        agy = get_harness("antigravity")
        bad: object
        for bad in (
            [],
            {},
            {**self.PAYLOAD, "conversationId": ""},
            {**self.PAYLOAD, "conversationId": "a/b"},
            {**self.PAYLOAD, "conversationId": "a\nb"},
            {**self.PAYLOAD, "workspacePaths": "relative"},
            {**self.PAYLOAD, "workspacePaths": ["relative"]},
            {**self.PAYLOAD, "transcriptPath": 1},
            {**self.PAYLOAD, "transcriptPath": "relative.jsonl"},
            {"hook_event_name": "SessionEnd", "session_id": "x", "cwd": "/w"},  # a Claude payload
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                agy.decode(bad)


if __name__ == "__main__":
    unittest.main()
