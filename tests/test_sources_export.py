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
import sqlite3
import unittest
import urllib.error
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from cairn import config


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
        self._prev = os.environ.get("CAIRN_CONFIG")

    def _activate(self, stores: dict[str, str]):
        cfg_path = _write_config(self.root, stores)
        os.environ["CAIRN_CONFIG"] = str(cfg_path)
        config.get_config.cache_clear()
        module = importlib.import_module(self.module_name)
        return importlib.reload(module)

    def tearDown(self):
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


if __name__ == "__main__":
    unittest.main()
