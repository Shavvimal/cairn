"""Foundations of ``cairn invoke``: links, invoke config, export_and_index, harness adapters."""

from __future__ import annotations

import io
import json
import os
import subprocess
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
    DENY_COMMANDS,
    DENY_FILES,
    MODE_BY_PROFILE,
    READ_ONLY_COMMANDS,
    TEST_COMMANDS,
    Antigravity,
    adc_token_check,
    add_dirs_from_extra,
    auth_failure,
    billing_env,
    billing_problem,
    billing_problems,
    command_segments,
    command_verdict,
    default_deny_rules,
    grant_commands,
    grant_rules,
    is_denial,
    needs_shell_rules,
    observe_billing,
    permission_rules,
    read_verdict,
    resolved_project,
    shell_rules_present,
    suggested_prefix,
    tool_name,
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
    HEAD: ClassVar[list[str]] = ["agy", "--output-format", "stream-json", "--print-timeout", "0"]

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
        self.assertEqual(full[5:], ["--dangerously-skip-permissions", "-p=Review this"])
        native = self.agy.exec_argv(_request("antigravity", native_policy=("--mode", "auto")))
        self.assertEqual(native[5:7], ["--mode", "auto"])
        # --disable-slash-commands would cancel --mode (agy 1.4.6), so it is never passed.
        self.assertNotIn("--disable-slash-commands", full)
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
        # The shell call is a structured ``command`` event; a plain tool error (not a
        # permission refusal) stays a failure note.
        self.assertEqual([e.kind for e in failed], ["command", "progress"])
        self.assertEqual([e.text for e in failed], ["rm -rf /", "tool failed: run_command: denied"])
        sub = agy.parse_event(
            '{"event":"step_update","step_update":{"step_index":8,"state":"DONE","step_type":'
            '"tool","tool_name":"invoke_subagent","subagent_info":{"subagents":[{"type_name":'
            '"researcher","role":"worker","conversation_id":"c2"}]}}}'
        )
        self.assertEqual(sub[-1].text, "subagents: researcher (worker)")

    def test_refusals_and_writes_are_structured(self):
        """The shapes agy 1.4.6 emitted live: a refused ``run_command`` ends in state
        ERROR with "permission check failed"; a write tool ends DONE with no output."""
        agy = get_harness("antigravity")
        refused_line = (
            '{"event":"step_update","step_update":{"step_index":26,"state":"%s","step_type":'
            '"tool","tool_name":"run_command","tool_info":{"name":"run_command","parameters":'
            '{"CommandLine":"rg foo src"}%s}}}'
        )
        error = (
            ',"error":{"type":"TOOL_ERROR","message":"permission check failed for command '
            '\\"rg foo src\\": user denied permission to run command:\\nrg foo src\\nDo not '
            'attempt to circumvent this denial."}'
        )
        started = agy.parse_event(refused_line % ("ACTIVE", ""))
        self.assertEqual([(e.kind, e.text) for e in started], [("command", "rg foo src")])
        ended = agy.parse_event(refused_line % ("ERROR", error))
        self.assertEqual(len(ended), 1)
        self.assertEqual(
            (ended[0].kind, ended[0].text, ended[0].error), ("denied", "rg foo src", "run_command")
        )
        write_line = (
            '{"event":"step_update","step_update":{"step_index":108,"state":"%s","step_type":'
            '"tool","tool_name":"write_to_file","tool_info":{"name":"write_to_file","parameters":'
            '{"TargetFile":"/repo/a.sql"}}}}'
        )
        started = agy.parse_event(write_line % "ACTIVE")
        self.assertEqual(started[0].kind, "progress")
        self.assertTrue(str(started[0].text).startswith("tool: write_to_file"))
        ended = agy.parse_event(write_line % "DONE")
        self.assertEqual([(e.kind, e.text) for e in ended], [("file_written", "/repo/a.sql")])
        # A replace that was refused is a refusal, not a write.
        refused_write = (
            '{"event":"step_update","step_update":{"step_index":9,"state":"DONE","step_type":'
            '"tool","tool_name":"replace_file_content","tool_info":{"name":"replace_file_content",'
            '"parameters":{"TargetFile":"/repo/b.py"},"error":{"type":"TOOL_ERROR","message":'
            '"user denied permission to write"}}}}'
        )
        events = agy.parse_event(refused_write)
        self.assertEqual(
            [(e.kind, e.text, e.error) for e in events][-1],
            ("denied", "/repo/b.py", "replace_file_content"),
        )
        self.assertTrue(is_denial("Permission check failed for command"))
        self.assertFalse(is_denial("context canceled"))
        self.assertTrue(getattr(agy, "reports_files", False))

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
        # A soft-denied tool still ends in SUCCESS (verified live); the denial is kept.
        denied = agy.parse_event(
            '{"event":"result","result":{"conversation_id":"b85d","status":"SUCCESS","response":"",'
            '"num_turns":1,"denied_actions":[{"action":"command","display_name":"RunCommand"}]}}'
        )[0]
        self.assertEqual(denied.status, "succeeded")
        assert denied.usage is not None
        self.assertEqual(denied.usage["denied_actions"], ["command"])
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
        "email=s@x.com, authMethod=adc, quotaProject=example-quota-project\n"
        "I1023 10:53:43.468255      63 http_helpers.go:349] URL: https://aiplatform."
        "googleapis.com/v1/projects/example-quota-project/locations/global/publishers/"
        "google/models/gemini-3.1-flash-lite:streamGenerateContent?alt=sse ResponseID: a\n"
        "I1023 10:53:45.166799     433 http_helpers.go:349] URL: https://aiplatform."
        "googleapis.com/v1/projects/example-quota-project/locations/global/publishers/"
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
                {
                    "auth_method": None,
                    "quota_project": None,
                    "vertex_projects": [],
                    "auth_error": None,
                },
            )
            log.write_text(self.LOG)
            observed = observe_billing(log)
        project = "example-quota-project"
        self.assertEqual(
            observed,
            {
                "auth_method": "adc",
                "quota_project": project,
                "vertex_projects": [project],
                "auth_error": None,
            },
        )
        self.assertIsNone(billing_problem("gcp", project, observed))
        self.assertIsNone(billing_problem("gcp", None, observed))
        self.assertIsNone(billing_problem("inherit", None, observed))
        self.assertIn("plan route requested", billing_problem("plan", None, observed) or "")
        self.assertIn(
            "quotaProject=example-quota-project",
            billing_problem("gcp", "other-project", observed) or "",
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


class TestAntigravityPermissions(unittest.TestCase):
    """agy auto-denies shell commands without ``permissions.allow`` rules; the grant adds
    cairn's allow and deny sets and keeps everything else, and the matcher mirrors agy's
    word-by-word prefix rules so a refusal can name the rule it needs."""

    def test_grant_merges_and_preserves(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "store" / "settings.json"
            self.assertEqual(permission_rules(path), ([], []))
            self.assertFalse(shell_rules_present([]))
            added = grant_commands(path, READ_ONLY_COMMANDS)
            self.assertEqual(len(added), len(READ_ONLY_COMMANDS))
            allow, deny = permission_rules(path)
            self.assertIn("command(rg)", allow)
            self.assertIn("command(regex:sed -n .*)", allow)
            self.assertIn("command(git diff)", allow)
            self.assertNotIn("command(git push)", allow)
            self.assertEqual(deny, [])
            self.assertTrue(shell_rules_present(allow))
            # Existing settings and deny rules survive; the grant is idempotent.
            data = json.loads(path.read_text())
            data["gcp"] = {"project": "p"}
            data["permissions"]["deny"] = ["command(git push)", "read_file(.env)"]
            data["permissions"]["allow"].insert(0, "command(poetry run pytest)")
            path.write_text(json.dumps(data))
            self.assertEqual(grant_commands(path, READ_ONLY_COMMANDS), [])
            added = grant_commands(path, READ_ONLY_COMMANDS + TEST_COMMANDS)
            self.assertEqual(added, [f"command({c})" for c in TEST_COMMANDS])
            data = json.loads(path.read_text())
            self.assertEqual(data["gcp"], {"project": "p"})
            self.assertEqual(data["permissions"]["deny"], ["command(git push)", "read_file(.env)"])
            self.assertEqual(data["permissions"]["allow"][0], "command(poetry run pytest)")
            self.assertFalse(shell_rules_present(["read_file(src/)", "write_file(src/)"]))
            path.write_text("[]")
            with self.assertRaises(ValueError):
                permission_rules(path)

    def test_deny_set_is_written_once_and_never_overrides_an_explicit_allow(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(
                json.dumps(
                    {"permissions": {"allow": ["command(python)"], "deny": ["command(aws)"]}}
                )
            )
            grant = grant_rules(path, ["command(rg)"], default_deny_rules())
            self.assertEqual(grant["added_allow"], ["command(rg)"])
            # ``aws`` was there; ``python`` is allowed by the user, so the deny is skipped.
            self.assertEqual(grant["skipped_deny"], ["command(python)"])
            self.assertNotIn("command(aws)", grant["added_deny"])
            self.assertIn("command(git push)", grant["added_deny"])
            self.assertIn("read_file(.env.prod)", grant["added_deny"])
            allow, deny = permission_rules(path)
            self.assertEqual(allow, ["command(python)", "command(rg)"])
            self.assertEqual(deny[0], "command(aws)")
            self.assertNotIn("command(python)", deny)
            self.assertEqual(len(deny), 1 + len(DENY_COMMANDS) - 2 + len(DENY_FILES))
            again = grant_rules(path, ["command(rg)"], default_deny_rules())
            self.assertEqual((again["added_allow"], again["added_deny"]), ([], []))
            self.assertEqual(again["skipped_deny"], ["command(python)"])
            self.assertEqual(default_deny_rules()[0], "command(supabase)")
            self.assertIn("read_file(.env)", default_deny_rules())

    def test_verdicts_mirror_agy_matching(self):
        allow = ["command(rg)", "command(git log)", "command(regex:sed -n .*)", "read_file(src/)"]
        deny = ["command(git push)", "command(python)"]
        self.assertEqual(command_verdict("rg foo src", allow, deny)["verdict"], "allowed")
        self.assertEqual(command_verdict("git log --oneline -3", allow, deny)["verdict"], "allowed")
        self.assertEqual(command_verdict("sed -n 1,5p a", allow, deny)["verdict"], "allowed")
        self.assertEqual(command_verdict("sed -i x a", allow, deny)["verdict"], "unmatched")
        # Word-by-word: ``git`` alone does not cover ``git diff``; ``python`` does not cover ``python3``.
        unmatched = command_verdict("git diff", allow, deny)
        self.assertEqual(
            (unmatched["verdict"], unmatched["suggest"]), ("unmatched", ["command(git diff)"])
        )
        self.assertEqual(
            command_verdict("python3 -m pytest", allow, deny)["suggest"], ["command(python3)"]
        )
        denied = command_verdict("git push origin main", allow, deny)
        self.assertEqual(
            (denied["verdict"], denied["denied_by"], denied["suggest"]),
            ("denied", ["command(git push)"], []),
        )
        # A compound command needs every segment; the suggestion names the missing ones only.
        compound = command_verdict('git log --oneline -3 && echo "---" | rg -n x', allow, deny)
        self.assertEqual(compound["verdict"], "unmatched")
        self.assertEqual(compound["unmatched"], ['echo "---"'])
        self.assertEqual(compound["suggest"], ["command(echo)"])
        self.assertEqual(command_verdict("rg a; python x.py", allow, deny)["verdict"], "denied")
        self.assertEqual(command_segments(" a && b || c | d ; e "), ["a", "b", "c", "d", "e"])
        self.assertEqual(suggested_prefix("uv run pytest -q"), "uv run")
        self.assertEqual(suggested_prefix("git -C x status"), "git")
        self.assertEqual(suggested_prefix("make"), "make")
        self.assertEqual(command_verdict("anything", ["command(*)"], [])["verdict"], "allowed")
        self.assertEqual(command_verdict("rg x", [], ["command(regex:[)"])["verdict"], "unmatched")
        self.assertTrue(needs_shell_rules(("--mode", "accept-edits")))
        self.assertFalse(needs_shell_rules(("--mode", "plan")))
        self.assertFalse(needs_shell_rules(("--mode", "auto")))
        self.assertFalse(needs_shell_rules(("--dangerously-skip-permissions",)))


class TestAntigravityReadDenials(unittest.TestCase):
    """A soft-denied ``read_file`` leaves no error in the stream: the log names it by
    step, the harness joins the step to its path, and the verdict models agy's rules."""

    LOG = (
        "I1027 11:34:10.587050    1091 runner.go:563] Monitor agy_tool_safety FINISHED\n"
        "I1027 11:34:10.590358     584 tool_confirmation_manager.go:216] Print mode: "
        'soft-denying tool confirmation "ReadFile" at step 2\n'
        "I1027 11:34:10.590400     584 tool_confirmation_manager.go:216] Print mode: "
        'soft-denying tool confirmation "RunCommand" at step 7\n'
        "I1027 11:34:10.610246       1 manager.go:857] CLI store manager shutting down\n"
    )

    def test_soft_denials_join_the_log_to_the_stream(self):
        agy = Antigravity()
        step = {
            "step_index": 2,
            "state": "DONE",
            "step_type": "tool",
            "tool_name": "read_file",
            "tool_info": {"name": "read_file", "parameters": {"Path": "/x/plan.md"}},
        }
        agy.parse_event(json.dumps({"event": "step_update", "step_update": step}))
        with TemporaryDirectory() as tmp:
            log = Path(tmp) / "peer-01.log"
            self.assertEqual(agy.soft_denials(log), [])  # no log yet
            log.write_text(self.LOG)
            self.assertEqual(
                agy.soft_denials(log),
                [
                    {"tool": "read_file", "target": "/x/plan.md"},
                    {"tool": "run_command", "target": ""},  # step 7 was not in the stream
                ],
            )
        self.assertEqual(agy.soft_denials(None), [])
        self.assertEqual(tool_name("ReadFile"), "read_file")
        self.assertEqual(tool_name("WriteToFile"), "write_to_file")
        self.assertEqual(tool_name("RunCommand"), "run_command")

    def test_add_dirs_from_extra(self):
        self.assertEqual(add_dirs_from_extra(()), [])
        self.assertEqual(
            add_dirs_from_extra(("--add-dir", "/a", "--verbose", "--add-dir=/b", "--add-dir")),
            ["/a", "/b"],
        )

    def test_read_verdict(self):
        allow = ["command(rg)", "read_file(/srv/docs)", "write_file(/srv/out)"]
        deny = ["read_file(.env*)", "read_file(/srv/secret)"]
        roots = ["/work/repo", "/extra"]
        self.assertEqual(
            read_verdict("/work/repo/src/a.py", allow, deny, roots)["allowed_by"],
            "inside /work/repo",
        )
        self.assertEqual(read_verdict("/extra/x.md", allow, deny, roots)["verdict"], "allowed")
        self.assertEqual(
            read_verdict("/srv/docs/deep/file.md", allow, deny, roots)["allowed_by"],
            "read_file(/srv/docs)",
        )
        self.assertEqual(
            read_verdict("/srv/out/x", allow, deny, roots)["allowed_by"], "write_file(/srv/out)"
        )
        # Deny beats the workspace and every allow rule; a bare target matches the name.
        self.assertEqual(
            read_verdict("/work/repo/.env.prod", allow, deny, roots)["denied_by"],
            ["read_file(.env*)"],
        )
        self.assertEqual(
            read_verdict("/srv/secret/key", allow, deny, roots)["denied_by"],
            ["read_file(/srv/secret)"],
        )
        outside = read_verdict("/srv/notes/plan.md", allow, deny, roots)
        self.assertEqual(outside["verdict"], "unmatched")
        self.assertEqual(outside["suggest"], ["read_file(/srv/notes)"])
        # A prefix is a directory boundary, not a string prefix.
        self.assertEqual(read_verdict("/srv/docs2/x", allow, deny, roots)["verdict"], "unmatched")
        self.assertEqual(read_verdict("/anything", ["read_file(*)"], [], [])["verdict"], "allowed")
        self.assertEqual(
            read_verdict("/anything", [], ["read_file(*)"], roots)["verdict"], "denied"
        )
        # A relative rule is workspace-relative; deny beats the workspace default.
        self.assertEqual(
            read_verdict("/work/repo/secret/key", [], ["read_file(secret/)"], roots)["denied_by"],
            ["read_file(secret/)"],
        )
        self.assertEqual(
            read_verdict("/srv/elsewhere/a.md", ["read_file(../elsewhere)"], [], roots)["verdict"],
            "unmatched",
        )
        self.assertEqual(
            read_verdict("/work/elsewhere/a.md", ["read_file(../elsewhere)"], [], roots)[
                "allowed_by"
            ],
            "read_file(../elsewhere)",
        )
        # Dot segments are normalised before the workspace check; a relative --add-dir
        # root is taken from the workspace; a trailing slash is not a different rule.
        self.assertEqual(
            read_verdict("/work/repo/../secret/key", [], [], roots)["verdict"], "unmatched"
        )
        self.assertEqual(
            read_verdict("/work/docs/x.md", [], [], ["/work/repo", "../docs"])["allowed_by"],
            "inside /work/docs",
        )
        self.assertEqual(
            read_verdict("/srv/docs/x", ["read_file(/srv/docs/)"], [], [])["verdict"], "allowed"
        )
        # A slash-containing glob matches the whole path; ``~`` expands.
        self.assertEqual(
            read_verdict("/work/repo/conf/x.env", [], ["read_file(conf/*.env)"], roots)["verdict"],
            "denied",
        )
        home = str(Path("~/notes").expanduser())
        self.assertEqual(
            read_verdict(f"{home}/a.md", ["read_file(~/notes)"], [], [])["allowed_by"],
            "read_file(~/notes)",
        )
        # An ask rule denies: headless agy cannot ask, and ask beats allow and the workspace.
        asked = read_verdict("/work/repo/src/a.py", allow, [], roots, ["read_file(*)"])
        self.assertEqual(asked["verdict"], "denied")
        self.assertEqual(
            asked["denied_by"], ["read_file(*) (an ask rule; headless agy cannot ask)"]
        )


class TestAntigravityAdcToken(unittest.TestCase):
    """The ADC file can exist with a refresh token Google no longer honours; only
    minting a token proves the gcp route is usable."""

    RAPT = (
        "ERROR: (gcloud.auth.application-default.print-access-token) There was a problem "
        "refreshing your current auth tokens: ('invalid_grant: reauth related error "
        "(invalid_rapt)', {'error': 'invalid_grant'})"
    )

    @staticmethod
    def _run(returncode, stderr=""):
        def run(argv, **kwargs):
            assert argv == ["gcloud", "auth", "application-default", "print-access-token"]
            assert kwargs["env"] == {"HOME": "/h"} and kwargs["timeout"] == 20.0
            return subprocess.CompletedProcess(argv, returncode, stdout="", stderr=stderr)

        return run

    def test_probe_verdicts(self):
        env = {"HOME": "/h"}
        self.assertEqual(adc_token_check(env, run=self._run(0)), ("ok", None))
        verdict, reason = adc_token_check(env, run=self._run(1, self.RAPT))
        self.assertEqual(verdict, "refused")
        assert reason is not None
        self.assertIn("Google rejected the ADC refresh token", reason)
        self.assertIn("invalid_rapt", reason)
        self.assertIn("gcloud auth application-default login", reason)
        verdict, reason = adc_token_check(env, run=self._run(1, "could not resolve host"))
        self.assertEqual(verdict, "unverified")
        assert reason is not None
        self.assertIn("could not resolve host", reason)

        def missing(argv, **kwargs):
            raise FileNotFoundError(argv[0])

        def slow(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        self.assertEqual(
            adc_token_check(env, run=missing),
            ("unverified", "gcloud is not installed, so the ADC token was not probed"),
        )
        verdict, reason = adc_token_check(env, run=slow)
        self.assertEqual(verdict, "unverified")
        self.assertIn("did not finish in 20s", reason or "")

    def test_log_names_a_rejected_refresh(self):
        failed = (
            "E1027 11:18:14.811000       1 adc_auth.go:80] adcAuth: failed to get token from "
            'token source: oauth2: "invalid_grant" "reauth related error (invalid_rapt)"\n'
        )
        recovered = failed + (
            "I1027 11:33:48.742004       1 adc_auth.go:73] adcAuth: authenticated successfully "
            "with quota project example-quota-project, token is non-empty: true\n"
            "I1027 11:33:48.871227       1 server_oauth.go:224] applyAuthResult: email=x@y.z, "
            "authMethod=adc, quotaProject=example-quota-project\n"
        )
        with TemporaryDirectory() as tmp:
            log = Path(tmp) / "peer-01.log"
            log.write_text(failed)
            observed = observe_billing(log)
            self.assertIsNone(observed["auth_method"])
            self.assertIn("invalid_rapt", observed["auth_error"])
            problem = billing_problem("gcp", "example-quota-project", observed)
            self.assertIn("ADC token refresh failed", problem or "")
            error, retryable = auth_failure(observed) or ("", True)
            self.assertIn("invalid_rapt", error)
            self.assertIn("gcloud auth application-default login", error)
            self.assertFalse(retryable)
            # A transport failure is retryable; a success after a failure clears it.
            log.write_text(
                failed.replace(
                    '"invalid_grant" "reauth related error (invalid_rapt)"', "dial tcp: i/o timeout"
                )
            )
            error, retryable = auth_failure(observe_billing(log)) or ("", False)
            self.assertIn("i/o timeout", error)
            self.assertTrue(retryable)
            log.write_text(recovered)
            observed = observe_billing(log)
            self.assertEqual(observed["auth_method"], "adc")
            self.assertIsNone(observed["auth_error"])
            self.assertIsNone(auth_failure(observed))
            self.assertIsNone(auth_failure({"auth_error": None}))
            # Any later authentication is a recovery, whatever its method.
            log.write_text(
                failed + "I1027 11:33:48.871227       1 server_oauth.go:224] applyAuthResult: "
                "email=x@y.z, authMethod=gcp, quotaProject=\n"
            )
            self.assertIsNone(observe_billing(log)["auth_error"])

    def test_only_a_rejected_credential_refuses(self):
        env = {"HOME": "/h"}
        proxy = "dial tcp: lookup reauth-proxy.example.test: no such host"
        verdict, reason = adc_token_check(env, run=self._run(1, proxy))
        self.assertEqual(verdict, "unverified")
        self.assertIn("reauth-proxy", reason or "")
        rapt = "reauth related error (invalid_rapt)"
        self.assertEqual(adc_token_check(env, run=self._run(1, rapt))[0], "refused")


if __name__ == "__main__":
    unittest.main()
