"""Behavioural export tests for the Codex, Cursor, and Granola adapters.

The shared engine is exercised by the config/recall/frontmatter suites; these tests
cover the *adapter-specific* parse-and-render paths that were previously only
smoke-tested (import + verb set). Codex and Cursor render real fixtures through a
temp ``CAIRN_CONFIG`` (the modules read their store at import time, so we reload them
under the temp config); Granola's export path needs the network + Keychain, so its
deterministic parse/format helpers are tested directly instead.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sqlite3
import unittest
import urllib.error
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from cairn import config
from cairn.frontmatter import parse_frontmatter
from cairn.schema import SessionData


def _write_config(root: Path, stores: dict[str, str]) -> Path:
    """Write a minimal cairn config pointing each collection at a temp store."""
    collections = {name: {"store": store} for name, store in stores.items()}
    cfg = {
        "data_root": str(root / ".context"),
        "qmd_binary": "qmd",
        "collections": collections,
        "project_groups": {},
        "repo_catalog": {},
        "project_descriptions": {},
    }
    path = root / "cairn.config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


class _AdapterFixture(unittest.TestCase):
    """Point CAIRN_CONFIG at a temp config and reload the adapter under test.

    The source modules bind their store/output dirs at import time, so we reload the
    module after the temp config is active and reload it back on teardown so the rest
    of the suite keeps the real config.
    """

    module_name: str = ""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._home_patch = patch.dict(os.environ, {"HOME": str(self.root / "home")})
        self._home_patch.start()
        self._prev = os.environ.get("CAIRN_CONFIG")

    def _activate(self, stores: dict[str, str]):
        cfg_path = _write_config(self.root, stores)
        os.environ["CAIRN_CONFIG"] = str(cfg_path)
        config.get_config.cache_clear()
        module = importlib.import_module(self.module_name)
        return importlib.reload(module)

    def tearDown(self):
        self._home_patch.stop()
        if self._prev is None:
            os.environ.pop("CAIRN_CONFIG", None)
        else:
            os.environ["CAIRN_CONFIG"] = self._prev
        config.get_config.cache_clear()
        # Rebind the reloaded module to the restored config for later tests.
        if self.module_name:
            importlib.reload(importlib.import_module(self.module_name))
        self._tmp.cleanup()


class TestCodexExport(_AdapterFixture):
    module_name = "cairn.sources.codex"

    def test_current_response_users_exclude_context_and_deduplicate_legacy_events(self):
        store = self.root / "codex-store"
        store.mkdir()
        mod = self._activate({"codex-sessions": str(store)})
        records = [{"type": "session_meta", "payload": {"id": "current", "cwd": "/tmp"}}]
        for turn in ("first", "second"):
            records.append(
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}}
            )
            for text, kind in (
                ("Injected environment", "environments.environment_context"),
                ("Investigate the widget bug", "user.text"),
            ):
                records.append(
                    {
                        "type": "response_item",
                        "timestamp": "2026-10-16T12:00:00Z",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": text}],
                            "internal_chat_message_metadata_passthrough": {
                                "turn_id": turn,
                                "content_item_kinds": [kind],
                            },
                        },
                    }
                )
            if turn == "first":
                records.append(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "Investigate the widget bug",
                        },
                    }
                )
        path = store / "rollout-current.jsonl"
        path.write_text("\n".join(map(json.dumps, records)))
        data = mod.parse_rollout(path)
        self.assertEqual(data["user_messages"], ["Investigate the widget bug"] * 2)
        self.assertEqual(data["message_count"], 2)
        self.assertEqual(len(data["messages"]), 2)

    def test_rollout_parses_and_renders(self):
        store = self.root / "codex-store"
        store.mkdir()
        rollout = [
            {
                "type": "session_meta",
                "payload": {
                    "id": "abc12345-feed-7000-8000-000000000000",
                    "timestamp": "2026-01-15T10:00:00Z",
                    "cwd": "/tmp/nowhere/demo",
                    "cli_version": "1.0.0",
                },
            },
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "Investigate the widget bug"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Found the off-by-one"}],
                },
            },
        ]
        (store / "rollout-2026.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rollout) + "\n", encoding="utf-8"
        )

        mod = self._activate({"codex-sessions": str(store)})
        exported = mod.CodexSource().export_rollouts(date_filter=None, quiet=True)
        self.assertEqual(exported, 1)

        outputs = list((self.root / ".context" / "codex-sessions").rglob("*.md"))
        self.assertEqual(len(outputs), 1)
        text = outputs[0].read_text(encoding="utf-8")
        self.assertIn("type: codex-session", text)
        self.assertIn("date: 2026-01-15", text)
        self.assertIn("Investigate the widget bug", text)  # title + user turn
        self.assertIn("Found the off-by-one", text)  # assistant turn


class TestClaudeProvenance(_AdapterFixture):
    """Every Claude export carries ``provenance`` as the last head line before status."""

    module_name = "cairn.sources.claude"
    session_id = "abc12345-feed-0000-0000-000000000000"

    def _write(self, store: Path, *texts: str) -> Path:
        folder = store / "-tmp-demo"
        folder.mkdir(parents=True, exist_ok=True)
        records = [
            {
                "sessionId": self.session_id,
                "timestamp": "2026-03-28T05:20:11Z",
                "type": "user",
                "message": {"role": "user", "content": text},
            }
            for text in texts
        ]
        path = folder / f"{self.session_id}.jsonl"
        path.write_text("\n".join(map(json.dumps, records)) + "\n", encoding="utf-8")
        return path

    def _export(self, mod, transcript: Path) -> Path:
        out = mod.ClaudeSource().sync_session(
            self.session_id, str(transcript), quiet=True, project="demo"
        )
        assert out is not None
        return out

    def test_export_writes_provenance_before_status(self):
        store = self.root / "claude-store"
        mod = self._activate({"claude-code-sessions": str(store)})
        out = self._export(mod, self._write(store, "Investigate the widget bug"))
        lines = out.read_text(encoding="utf-8").split("\n")
        index = lines.index("status: active")
        self.assertEqual(lines[index - 1], "provenance: tool")
        self.assertEqual(parse_frontmatter("\n".join(lines))["provenance"], "tool")

    def test_reexport_keeps_provenance_and_annotations(self):
        from cairn import mutations

        store = self.root / "claude-store"
        mod = self._activate({"claude-code-sessions": str(store)})
        source = mod.ClaudeSource()
        out = self._export(mod, self._write(store, "Investigate the widget bug"))
        self.assertTrue(mutations.add_comment(source.find_file, self.session_id, "keep me"))
        self.assertTrue(
            mutations._mutate_file(
                source.find_file, self.session_id, lambda fm: fm.__setitem__("tags", ["widget"])
            )
        )
        text = out.read_text(encoding="utf-8")
        placeholder = re.search(r"<!-- Add your notes here\.[^>]*-->", text)
        assert placeholder is not None
        text = text.replace(placeholder[0], "Hand-written follow-up")
        # A hand edit of the key does not survive: the re-render recomputes it.
        text = text.replace("\nprovenance: tool\n", "\nprovenance: user\n", 1)
        out.write_text(text, encoding="utf-8")
        self.assertEqual(parse_frontmatter(text)["provenance"], "user")
        transcript = self._write(store, "Investigate the widget bug", "Now add a test")
        self.assertEqual(self._export(mod, transcript), out)
        text = out.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        self.assertIn("Now add a test", text)  # the re-render happened
        self.assertEqual(fm["provenance"], "tool")
        self.assertEqual(fm["tags"], ["widget"])
        self.assertIn("keep me", fm["comments"])
        self.assertEqual(text.count("Hand-written follow-up"), 1)
        self.assertEqual(text.count("\nprovenance:"), 1)


class TestCodexProvenance(_AdapterFixture):
    module_name = "cairn.sources.codex"

    def test_peer_export_is_agent(self):
        from cairn.invoke import preamble

        store = self.root / "codex-store"
        store.mkdir()
        prompt = preamble("codex", None, 1, 600, owner="routine:example-daily") + "\n\nReview"
        rollout = [
            {
                "type": "session_meta",
                "payload": {
                    "id": "abc12345-feed-7000-8000-000000000000",
                    "timestamp": "2026-01-15T10:00:00Z",
                    "cwd": "/tmp/demo",
                },
            },
            {"type": "event_msg", "payload": {"type": "user_message", "message": prompt}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "VERDICT: accept - fine"}],
                },
            },
        ]
        (store / "rollout-2026.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rollout) + "\n", encoding="utf-8"
        )
        mod = self._activate({"codex-sessions": str(store)})
        self.assertEqual(mod.CodexSource().export_rollouts(date_filter=None, quiet=True), 1)
        (output,) = (self.root / ".context" / "codex-sessions").rglob("*.md")
        lines = output.read_text(encoding="utf-8").split("\n")
        self.assertEqual(lines[lines.index("status: active") - 1], "provenance: agent")


class TestCursorExport(_AdapterFixture):
    module_name = "cairn.sources.cursor"

    def test_composer_parses_and_renders(self):
        store = self.root / "cursor-store"
        (store / "globalStorage").mkdir(parents=True)

        composer_id = "11111111-2222-3333-4444-555555555555"
        ms = int(datetime(2026, 1, 15, 9, 0, tzinfo=UTC).timestamp() * 1000)
        composer = {
            "composerId": composer_id,
            "name": "Fix the widget pipeline",
            "status": "completed",
            "createdAt": ms,
            "lastUpdatedAt": ms,
            "fullConversationHeadersOnly": [
                {"type": 1, "bubbleId": "b1"},
                {"type": 2, "bubbleId": "b2"},
            ],
        }
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (f"composerData:{composer_id}", json.dumps(composer)),
        )
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (f"bubbleId:{composer_id}:b1", json.dumps({"text": "Hello there"})),
        )
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (f"bubbleId:{composer_id}:b2", json.dumps({"text": "Hi back"})),
        )
        conn.commit()

        mod = self._activate({"cursor-sessions": str(store)})
        self.addCleanup(conn.close)
        exported = mod.CursorSource().export_composers(conn, date_filter_ms=None, quiet=True)
        self.assertEqual(exported, 1)

        outputs = list((self.root / ".context" / "cursor-sessions").rglob("*.md"))
        self.assertEqual(len(outputs), 1)
        text = outputs[0].read_text(encoding="utf-8")
        self.assertIn("type: cursor-session", text)
        self.assertIn("Fix the widget pipeline", text)  # title
        self.assertIn("Hello there", text)  # user turn
        self.assertIn("Hi back", text)  # assistant turn

    def test_lone_surrogate_in_text_does_not_crash_export(self):
        # A lone surrogate (half an emoji pair) is a valid str code point but cannot
        # be UTF-8 encoded; before the fix it aborted the entire cursor export.
        store = self.root / "cursor-store"
        (store / "globalStorage").mkdir(parents=True)
        composer_id = "99999999-8888-7777-6666-555555555555"
        ms = int(datetime(2026, 2, 1, 9, 0, tzinfo=UTC).timestamp() * 1000)
        composer = {
            "composerId": composer_id,
            "name": "Emoji session",
            "createdAt": ms,
            "lastUpdatedAt": ms,
            "fullConversationHeadersOnly": [{"type": 1, "bubbleId": "b1"}],
        }
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (f"composerData:{composer_id}", json.dumps(composer)),
        )
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (f"bubbleId:{composer_id}:b1", json.dumps({"text": "Trophy \ud83c here"})),
        )
        conn.commit()

        mod = self._activate({"cursor-sessions": str(store)})
        exported = mod.CursorSource().export_composers(conn, date_filter_ms=None, quiet=True)
        self.assertEqual(exported, 1)

        outputs = list((self.root / ".context" / "cursor-sessions").rglob("*.md"))
        self.assertEqual(len(outputs), 1)
        text = outputs[0].read_text(encoding="utf-8")  # must not raise UnicodeDecodeError
        self.assertNotIn("\ud83c", text)  # lone surrogate stripped
        self.assertIn("Trophy", text)
        self.assertIn("here", text)


class TestGranolaExport(_AdapterFixture):
    module_name = "cairn.sources.granola"

    def test_source_headings_do_not_erase_handwritten_notes_on_reexport(self):
        mod = self._activate({"granola-sessions": str(self.root / "granola")})
        for field in ("notes_markdown", "summary_markdown"):
            with self.subTest(field=field):
                source = mod.GranolaSource()
                data: SessionData = {
                    "meeting_id": field,
                    "date": "2026-07-01",
                    "title": "Meeting",
                    "transcript_markdown": "Native transcript\n## My Notes\nTranscript text",
                }
                native_notes = "## Conversation\nCustomer feedback\n## Transcript\nMore notes"
                if field == "notes_markdown":
                    data["notes_markdown"] = native_notes
                else:
                    data["summary_markdown"] = native_notes
                path = source.export_one(field, data, None)
                assert path is not None
                path.write_text(
                    path.read_text().replace(
                        "<!-- Add your notes here. This section is preserved across exports. -->",
                        "Important handwritten follow-up\n```md\n## Conversation\n```",
                    )
                )
                for _ in range(2):
                    self.assertEqual(source.export_one(field, data, path), path)
                    content = path.read_text()
                    self.assertEqual(content.count("Important handwritten follow-up"), 1)
                    self.assertEqual(content.count("Native transcript"), 1)
                    self.assertEqual(content.count("Transcript text"), 1)
                    self.assertIn("```md\n## Conversation\n```", content)


class TestGranolaHelpers(unittest.TestCase):
    """Granola's export path needs network + Keychain; test its pure helpers here."""

    def setUp(self):
        # Granola binds its store at import; the repo config already provides one.
        from cairn.sources import granola

        self.granola = granola

    def test_prosemirror_to_markdown_handles_blocks(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "heading",
                    "attrs": {"level": 2},
                    "content": [{"type": "text", "text": "Agenda"}],
                },
                {"type": "paragraph", "content": [{"type": "text", "text": "Discuss the launch."}]},
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {"type": "paragraph", "content": [{"type": "text", "text": "One"}]}
                            ],
                        }
                    ],
                },
            ],
        }
        md = self.granola.prosemirror_to_markdown(doc)
        self.assertIn("## Agenda", md)
        self.assertIn("Discuss the launch.", md)
        self.assertIn("- One", md)

    def test_extract_meeting_data_skips_and_parses(self):
        self.assertIsNone(self.granola.extract_meeting_data({"deleted_at": "x"}, "d1"))
        self.assertIsNone(self.granola.extract_meeting_data({"type": "note"}, "d2"))

        doc = {
            "type": "meeting",
            "title": "Launch sync",
            "created_at": "2026-01-15T09:00:00Z",
            "notes_markdown": "some notes",
        }
        data = self.granola.extract_meeting_data(doc, "d3")
        assert data is not None
        self.assertEqual(data["date"], "2026-01-15")
        self.assertEqual(data["title"], "Launch sync")
        self.assertEqual(data["meeting_id"], "d3")

    def test_format_transcript_tags_speakers_and_times(self):
        segments = [
            {
                "source": "microphone",
                "text": "Hello team",
                "start_timestamp": "2026-01-15T09:00:05Z",
            },
            {"source": "system", "text": "Hi", "start_timestamp": ""},
            {"source": "microphone", "text": "   ", "start_timestamp": ""},  # blank -> skipped
        ]
        out = self.granola.format_transcript(segments)
        self.assertIn("**You**: Hello team", out)
        self.assertIn("09:00:05", out)
        self.assertIn("**Participant**: Hi", out)
        self.assertEqual(self.granola.format_transcript([]), "")

    def test_format_attendee_variants(self):
        self.assertEqual(
            self.granola.format_attendee({"name": "Ada", "email": "ada@x.com"}), "Ada <ada@x.com>"
        )
        self.assertEqual(self.granola.format_attendee({"email": "b@x.com"}), "b@x.com")
        self.assertEqual(self.granola.format_attendee({"name": "Solo"}), "Solo")

    def test_get_duration_and_time_from_calendar(self):
        doc = {
            "google_calendar_event": {
                "start": {"dateTime": "2026-01-15T09:00:00-08:00"},
                "end": {"dateTime": "2026-01-15T09:30:00-08:00"},
            }
        }
        self.assertEqual(self.granola.get_duration(doc), 30)
        time_str, last_activity = self.granola.get_meeting_time(doc)
        self.assertEqual(time_str, "09:00")
        self.assertEqual(last_activity, "2026-01-15T09:30:00-08:00")
        # No calendar event -> safe zeros/None, never a crash.
        self.assertEqual(self.granola.get_duration({}), 0)
        self.assertEqual(self.granola.get_meeting_time({}), (None, None))

    def test_find_access_token_unwraps_nested_json(self):
        # The token is buried inside a JSON-encoded string field.
        blob = {"workos_tokens": json.dumps({"access_token": "tok-123"})}
        self.assertEqual(self.granola._find_access_token(blob), "tok-123")
        self.assertIsNone(self.granola._find_access_token({"nope": 1}))

    def test_cmd_context_registers_single_entry_once(self):
        # Regression: the old cmd_context printed its status line twice. It now routes
        # through the shared register_descriptions with exactly one entry, no dup.
        with patch("cairn.sources.granola.qmd.register_descriptions", return_value=0) as reg:
            rc = self.granola.GranolaSource().cmd_context(argparse.Namespace())
        self.assertEqual(rc, 0)
        reg.assert_called_once()
        descriptions = reg.call_args.args[0]
        self.assertEqual(list(descriptions), ["qmd://granola-sessions"])

    # --- fail-loud-and-early behaviour ---

    def test_api_post_raises_on_401(self):
        err = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)  # type: ignore[arg-type]
        with (
            patch("urllib.request.urlopen", side_effect=err),
            self.assertRaises(self.granola.GranolaAuthError),
        ):
            self.granola.api_post("/v2/get-documents", {}, "tok")

    def test_transcript_degrades_on_auth_error(self):
        # Transcript is optional enrichment - an auth failure here must not propagate.
        with patch.object(
            self.granola,
            "_fetch_transcript_segments",
            side_effect=self.granola.GranolaAuthError("x"),
        ):
            self.assertEqual(self.granola.fetch_transcript("tok", "d1"), [])

    def test_run_export_fails_early_when_cryptography_missing(self):
        fake_enc = Mock()
        fake_enc.exists.return_value = True
        with (
            patch.object(self.granola, "ENC_ACCOUNTS_PATH", fake_enc),
            patch.object(self.granola, "_cryptography_available", return_value=False),
            patch.object(self.granola, "get_auth_token") as get_tok,
        ):
            rc = self.granola.GranolaSource().run_export(argparse.Namespace(all=True, quiet=True))
        self.assertEqual(rc, 1)  # non-zero -> cairn sync logs it FAILED
        get_tok.assert_not_called()  # bailed before even attempting auth (early)

    def test_run_export_fails_on_expired_token(self):
        with (
            patch.object(self.granola, "_cryptography_available", return_value=True),
            patch.object(self.granola, "get_auth_token", return_value="stale-token"),
            patch.object(self.granola, "load_cache", return_value={"documents": {}}),
            patch.object(
                self.granola.GranolaSource,
                "export_meetings",
                side_effect=self.granola.GranolaAuthError("401 Unauthorized"),
            ),
        ):
            rc = self.granola.GranolaSource().run_export(argparse.Namespace(all=True, quiet=True))
        self.assertEqual(rc, 1)


# ---------------------------------------------------------------------------
# Antigravity (agy): brain transcripts + conversation_summaries.db
# ---------------------------------------------------------------------------

# Record shapes vendored from agy 1.4.6 transcripts (2026-10-23), paths anonymised.
AGY_CONVERSATION = "0ccaac42-fa89-4d4f-8d55-236fe26b984a"
AGY_STEPS = [
    {
        "step_index": 0,
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "status": "DONE",
        "created_at": "2026-10-23T09:30:11Z",
        "content": "<USER_REQUEST>\nhelp me find out if Antigravity is using my GCP credits\n"
        "</USER_REQUEST>\n<ADDITIONAL_METADATA>\nThe current local time is: 2026-10-23T10:30:11"
        "+01:00.\n</ADDITIONAL_METADATA>\n<USER_SETTINGS_CHANGE>\nThe user changed setting "
        "`Model Selection` from None to Gemini 4 Argon (High).\n</USER_SETTINGS_CHANGE>",
    },
    {
        "step_index": 1,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": "2026-10-23T09:30:11Z",
        "input_tokens": 8045,
        "cache_read_tokens": 0,
        "output_tokens": 192,
        "thinking": "**Checking Antigravity Usage**\n\nI will read the guide first.",
        "tool_calls": [
            {"name": "read_file", "args": {"Path": "/w/SKILL.md"}},
            {"name": "run_command", "args": {"CommandLine": "gcloud billing projects describe p"}},
        ],
    },
    {
        "step_index": 2,
        "source": "MODEL",
        "type": "GENERIC",
        "status": "DONE",
        "created_at": "2026-10-23T09:30:18Z",
        "content": "Created At: 2026-10-23T10:30:18+01:00\nCompleted At: 2026-10-23T10:30:18+01:00\n"
        "Showing lines 1-2 of 2\n1: ---\n2: name: antigravity-guide",
    },
    {
        "step_index": 3,
        "source": "MODEL",
        "type": "GENERIC",
        "status": "ERROR",
        "error": "permission check failed for run_command",
        "created_at": "2026-10-23T09:30:30Z",
        "content": "Created At: 2026-10-23T10:30:30+01:00\nCompleted At: 2026-10-23T10:31:06+01:00\n"
        "Encountered error in step execution: permission check failed for run_command",
    },
    {
        "step_index": 4,
        "source": "SYSTEM",
        "type": "SYSTEM_MESSAGE",
        "status": "DONE",
        "created_at": "2026-10-23T09:39:38Z",
        "content": "The following is a <SYSTEM_MESSAGE> not actually sent by the user.\n\n"
        "<SYSTEM_MESSAGE>\n[Message] sender=task-19 content=Task was canceled\n</SYSTEM_MESSAGE>",
    },
    {
        "step_index": 5,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": "2026-10-23T09:40:00Z",
        "input_tokens": 100,
        "cache_read_tokens": 50,
        "output_tokens": 20,
        "content": "You are billed through Vertex AI on project p.",
        "media": [{"mime_type": "image/png", "uri": "/w/shot.png"}],
    },
]
AGY_COLUMNS = (
    "conversation_id text primary key, title text, preview text, step_count integer, "
    "last_modified_time text, last_user_input_time text, workspace_uris text, status text, "
    "source text, agent_name text, parent_conversation_id text, nesting_depth integer"
)


class TestAntigravityExport(_AdapterFixture):
    module_name = "cairn.sources.antigravity"

    def setUp(self):
        super().setUp()
        self.store = self.root / "agy"
        (self.store / "brain").mkdir(parents=True)
        self.workspace = self.root / "home" / "Code" / "demo"
        self.workspace.mkdir(parents=True)

    def transcript(self, conversation_id, steps=AGY_STEPS, compact_only=False):
        logs = self.store / "brain" / conversation_id / ".system_generated" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        body = "".join(json.dumps(step) + "\n" for step in steps)
        (logs / "transcript.jsonl").write_text(body)
        if not compact_only:
            (logs / "transcript_full.jsonl").write_text(body)
        return logs / ("transcript.jsonl" if compact_only else "transcript_full.jsonl")

    def row(
        self,
        conversation_id,
        title="Checking GCP Credits Usage",
        last="2026-10-23 09:42:57.132013+00:00",
        parent="",
        depth=0,
        status="CASCADE_RUN_STATUS_IDLE",
    ):
        db = sqlite3.connect(self.store / "conversation_summaries.db")
        db.execute(f"create table if not exists conversation_summaries ({AGY_COLUMNS})")
        db.execute(
            "insert or replace into conversation_summaries values (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                conversation_id,
                title,
                "preview",
                6,
                last,
                last,
                json.dumps([self.workspace.as_uri()]),
                status,
                "",
                "",
                parent,
                depth,
            ),
        )
        db.commit()
        db.close()

    def activate(self):
        return self._activate({"antigravity-sessions": str(self.store)})

    def documents(self):
        return sorted((self.root / ".context" / "antigravity-sessions").rglob("*.md"))

    def test_transcript_parses_and_renders(self):
        self.transcript(AGY_CONVERSATION)
        mod = self.activate()
        # First export before agy names the conversation: the prompt is the title.
        self.assertEqual(mod.AntigravitySource().export_conversations(quiet=True), 1)
        docs = self.documents()
        self.assertEqual(
            parse_frontmatter(docs[0].read_text())["title"],
            "help me find out if Antigravity is using my GCP credits",
        )
        self.row(AGY_CONVERSATION)
        self.assertEqual(mod.AntigravitySource().export_conversations(quiet=True), 1)
        self.assertEqual(len(docs), 1)
        text = docs[0].read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        self.assertEqual(fm["type"], "antigravity-session")
        self.assertEqual(fm["session_id"], AGY_CONVERSATION)
        self.assertEqual(fm["date"], "2026-10-23")
        self.assertEqual(fm["title"], "Checking GCP Credits Usage")
        self.assertEqual(fm["run_status"], "IDLE")
        self.assertEqual(fm["messages"], "1")
        self.assertEqual(fm["steps"], "6")
        self.assertEqual(fm["step_errors"], "1")
        self.assertEqual(fm["tokens"], "in=8145 out=212 cached=50")
        self.assertEqual(fm["source"], "antigravity")
        self.assertEqual(fm["native_id"], AGY_CONVERSATION)
        self.assertEqual(fm["cwd"], str(self.workspace))
        self.assertEqual(fm["activity_dates"], ["2026-10-23"])
        self.assertEqual(fm["status"], "active")  # the preserved tail, not agy's run status
        # Body: the prompt without its wrapper, thinking folded, paired tool results.
        self.assertIn("### User - 2026-10-23T09:30:11Z", text)
        self.assertIn("help me find out if Antigravity is using my GCP credits", text)
        for leaked in (
            "<USER_REQUEST>",
            "ADDITIONAL_METADATA",
            "USER_SETTINGS_CHANGE",
            "Created At:",
        ):
            self.assertNotIn(leaked, text)
        self.assertIn("<details><summary>Reasoning</summary>", text)
        self.assertIn('<tool name="read_file" status="done">', text)
        self.assertIn("2: name: antigravity-guide", text)
        self.assertIn('<tool name="run_command" status="error">', text)
        self.assertIn("permission check failed for run_command", text)
        self.assertIn("### System - 2026-10-23T09:39:38Z", text)
        self.assertIn("[Message] sender=task-19 content=Task was canceled", text)
        self.assertNotIn("not actually sent by the user", text)
        self.assertIn("You are billed through Vertex AI on project p.", text)
        self.assertIn("[media image/png](/w/shot.png)", text)
        # Re-export is a no-op until the native snapshot changes.
        self.assertEqual(mod.AntigravitySource().export_conversations(quiet=True), 0)
        # A later index change re-renders, but a title that is no longer the prompt is
        # treated as the user's and preserved.
        self.row(AGY_CONVERSATION, title="Renamed by agy", last="2026-10-23 09:50:00+00:00")
        self.assertEqual(mod.AntigravitySource().export_conversations(quiet=True), 1)
        self.assertEqual(
            parse_frontmatter(docs[0].read_text())["title"], "Checking GCP Credits Usage"
        )

    def test_without_an_index_row_or_full_transcript(self):
        self.transcript("bbbbbbbb-0000-4000-8000-000000000001", compact_only=True)
        (self.store / "brain" / "cccccccc-0000-4000-8000-000000000002").mkdir()  # no transcript
        mod = self.activate()
        source = mod.AntigravitySource()
        self.assertEqual(source.export_conversations(quiet=True), 1)
        docs = self.documents()
        self.assertEqual(len(docs), 1)
        fm = parse_frontmatter(docs[0].read_text())
        self.assertEqual(fm["title"], "help me find out if Antigravity is using my GCP credits")
        self.assertNotIn("run_status", fm)
        self.assertEqual(docs[0].parent.name, "antigravity-sessions")  # no workspace: root
        self.assertEqual(
            source.conversations(),
            {
                "bbbbbbbb-0000-4000-8000-000000000001": self.transcript(
                    "bbbbbbbb-0000-4000-8000-000000000001", compact_only=True
                )
            },
        )

    def test_window_uses_the_index_time(self):
        self.transcript(AGY_CONVERSATION)
        self.row(AGY_CONVERSATION, last="2026-01-01 00:00:00+00:00")
        mod = self.activate()
        cutoff = datetime(2026, 6, 1, tzinfo=UTC).timestamp()
        self.assertEqual(
            mod.AntigravitySource().export_conversations(date_filter=cutoff, quiet=True), 0
        )
        self.assertEqual(
            mod.AntigravitySource().export_conversations(date_filter=None, quiet=True), 1
        )

    def test_export_target_prefers_the_full_sibling_and_checks_identity(self):
        compact = self.transcript(AGY_CONVERSATION).with_name("transcript.jsonl")
        self.row(AGY_CONVERSATION)
        mod = self.activate()
        source = mod.AntigravitySource()
        document = source.export_target(AGY_CONVERSATION, str(compact))
        self.assertTrue(document.is_file())
        self.assertEqual(source.export_target(AGY_CONVERSATION, None), document)
        with self.assertRaisesRegex(ValueError, "does not belong"):
            source.export_target("other-id", str(compact))
        with self.assertRaisesRegex(ValueError, "unavailable"):
            source.export_target("dddddddd-0000-4000-8000-000000000004", None)

    def test_child_conversation_lands_in_the_parent_folder(self):
        child = "eeeeeeee-0000-4000-8000-000000000005"
        self.transcript(AGY_CONVERSATION)
        self.row(AGY_CONVERSATION)
        self.transcript(child, steps=AGY_STEPS[:2])
        self.row(child, title="Worker: billing check", parent=AGY_CONVERSATION, depth=1)
        mod = self.activate()
        self.assertEqual(mod.AntigravitySource().export_conversations(quiet=True), 2)
        docs = self.documents()
        parent_doc = next(d for d in docs if d.name.endswith(f"{AGY_CONVERSATION[:8]}.md"))
        child_doc = next(d for d in docs if d.name == f"agent-{child[:8]}.md")
        self.assertEqual(child_doc.parent, parent_doc.with_suffix(""))
        child_fm = parse_frontmatter(child_doc.read_text())
        self.assertEqual(child_fm["type"], "antigravity-subagent")
        self.assertEqual(child_fm["parent_session_id"], AGY_CONVERSATION)
        self.assertEqual(child_fm["spawn_depth"], "1")
        parent_text = parent_doc.read_text()
        self.assertIn("## Subagents", parent_text)
        self.assertIn(
            f"[Worker: billing check](./{parent_doc.stem}/agent-{child[:8]}.md)", parent_text
        )
        # A targeted export of the child refreshes the whole family.
        source = mod.AntigravitySource()
        self.assertEqual(source.export_target(child, None), child_doc)


if __name__ == "__main__":
    unittest.main()
