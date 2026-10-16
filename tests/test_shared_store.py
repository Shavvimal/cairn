"""Cross-source recall, identity, migration and concurrent annotation acceptance."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cairn import config, recall
from cairn.frontmatter import extract_my_notes_section, parse_frontmatter
from cairn.harnesses import get_harness
from cairn.store import SessionStore, bounded_content


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cwd = str(self.root / "project")
        self.claude = self.root / "claude"
        self.codex = self.root / "codex"
        self.claude.mkdir()
        self.codex.mkdir()
        self.config = self.root / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "data_root": str(self.root / "data"),
                    "qmd_binary": "/missing/qmd",
                    "collections": {
                        "claude-code-sessions": {"store": str(self.claude)},
                        "codex-sessions": {"store": str(self.codex)},
                    },
                    "project_groups": {},
                    "repo_catalog": {},
                    "project_descriptions": {},
                }
            )
        )
        self.env = dict(
            os.environ,
            CAIRN_CONFIG=str(self.config),
            XDG_CACHE_HOME=str(self.root / "cache"),
            XDG_CONFIG_HOME=str(self.root / "xdg"),
        )
        self.patch = patch.dict(os.environ, self.env)
        self.patch.start()
        config.get_config.cache_clear()
        self.addCleanup(self.patch.stop)
        self.addCleanup(config.get_config.cache_clear)

    def run_cli(self, *args, check=True):
        return subprocess.run(
            [sys.executable, "-m", "cairn", *args],
            env=self.env,
            text=True,
            capture_output=True,
            check=check,
        )

    def native(self, source, sid, stamps=None, text="Shared orchard marker"):
        stamps = stamps or ["2026-03-28T23:30:00Z", "2026-03-30T00:30:00Z"]
        if source == "claude":
            records = [
                {
                    "sessionId": sid,
                    "cwd": self.cwd,
                    "type": "user",
                    "timestamp": t,
                    "message": {"role": "user", "content": text},
                }
                for t in stamps
            ]
            (self.claude / "-demo").mkdir(exist_ok=True)
            path = self.claude / "-demo" / (sid + ".jsonl")
        else:
            records = [
                {
                    "type": "session_meta",
                    "payload": {"id": sid, "cwd": self.cwd, "timestamp": stamps[0]},
                }
            ]
            records += [
                {
                    "type": "event_msg",
                    "timestamp": t,
                    "payload": {"type": "user_message", "message": text},
                }
                for t in stamps
            ]
            path = self.codex / ("rollout-" + sid + ".jsonl")
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        return path

    def records(self):
        return SessionStore(config.get_config()).records()

    def query(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            _rc = recall.main(list(args))
        return _rc, out.getvalue()


class TestSharedStore(StoreFixture):
    def test_cross_source_activity_gaps_and_utc_boundaries(self):
        for source in ("claude", "codex"):
            self.native(source, "same-id")
            self.run_cli(source, "export", "--all")
        _rc, out = self.query("list", "2026-03-29", "--all-projects", "--min-msgs", "1", "--json")
        self.assertEqual((_rc, json.loads(out)), (0, []))
        _rc, out = self.query(
            "list", "2026-03-30", "--project", self.cwd, "--min-msgs", "1", "--json"
        )
        self.assertEqual({r["source"] for r in json.loads(out)}, {"claude", "codex"})
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            SessionStore(config.get_config()).resolve("same")
        self.assertEqual(SessionStore(config.get_config()).resolve("codex:same").source, "codex")
        _rc, out = self.query(
            "list", "2026-03-30", "--project", "/unrelated", "--min-msgs", "1", "--json"
        )
        self.assertEqual(json.loads(out), [])

    def test_offset_midnight_and_source_filter(self):
        self.native("codex", "offset", ["2026-03-29T01:30:00+02:00"])
        self.run_cli("codex", "export", "--all")
        _rc, out = self.query(
            "list", "2026-03-28", "--source", "codex", "--all-projects", "--min-msgs", "1", "--json"
        )
        self.assertEqual(len(json.loads(out)), 1)
        _rc, out = self.query("list", "2026-03-29", "--all-projects", "--min-msgs", "1", "--json")
        self.assertEqual(json.loads(out), [])

    def test_missing_native_data_preserves_legacy_recall_and_meeting_counts(self):
        folder = self.root / "data/claude-code-sessions"
        folder.mkdir(parents=True)
        (folder / "old.md").write_text(
            "---\ntype: claude-session\nsession_id: legacy\ndate: 2026-03-28\nmessages: 12\ncustom: untouched\n---\n## Conversation\nLegacy text"
        )
        _rc, out = self.query("list", "2026-03-28", "--all-projects", "--json")
        item = json.loads(out)[0]
        self.assertEqual(item["precision"], "date")
        self.assertIsNone(item["msgs"])  # legacy messages has no shared meaning
        self.assertEqual(self.records()[0].metadata["custom"], "untouched")
        self.assertEqual(self.query("expand", "legacy")[0], 0)

    def test_migration_preserves_annotations_despite_newer_export_mtime(self):
        self.native("codex", "migrate")
        self.run_cli("codex", "export", "--all")
        record = self.records()[0]
        self.run_cli("codex", "note", "Keep this note", "--session-id", "migrate")
        old = (
            record.path.read_text()
            .replace('cairn_schema: "1"', 'cairn_schema: "0"')
            .replace(
                "<!-- Add your notes here. This section is preserved across exports. -->",
                "My handwritten note",
            )
        )
        record.path.write_text(old)
        self.run_cli("codex", "export", "--all")
        content = record.path.read_text()
        self.assertIn('cairn_schema: "1"', content)
        self.assertIn("Keep this note", content)
        self.assertIn("My handwritten note", content)

    def test_notes_fences_and_unknown_activity_are_not_invented(self):
        notes = "## My Notes\n```md\n## Conversation\n```\nKeep this"
        self.assertEqual(extract_my_notes_section(notes + "\n## Conversation\ntext"), notes)
        self.assertIsNone(extract_my_notes_section("## Conversation\n## My Notes\nforged"))
        path = self.codex / "rollout-no-time.jsonl"
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "no-time"}}))
        self.run_cli("codex", "export", "--all")
        self.assertEqual(self.records()[0].activity, ())
        _, out = self.query("list", "today", "--all-projects", "--min-msgs", "0", "--json")
        self.assertEqual(json.loads(out), [])

    def test_meetings_remain_visible_with_unknown_message_counts(self):
        from cairn.store import read_record

        path = self.root / "meeting.md"
        path.write_text(
            "---\ntype: granola-meeting\nmeeting_id: meeting\ndate: 2026-03-28\n---\nNotes"
        )
        record = read_record(path, "granola", "meeting_id", "granola-meeting")
        self.assertIsNotNone(record)
        assert record is not None
        self.assertIsNone(record.user_messages)
        self.assertTrue(
            record.in_window(datetime(2026, 3, 28, tzinfo=UTC), datetime(2026, 3, 29, tzinfo=UTC))
        )

    def test_colliding_prefix_mutates_only_full_selected_identity(self):
        for sid in ("abcdef12-one", "abcdef12-two"):
            self.native("claude", sid)
        self.run_cli("claude", "export", "--all")
        self.assertEqual(len(self.records()), 2)
        bad = self.run_cli("claude", "note", "wrong", "--session-id", "abcdef12", check=False)
        self.assertNotEqual(bad.returncode, 0)
        self.run_cli("claude", "close", "only one", "--session-id", "abcdef12-one")
        statuses = {r.session_id: r.metadata["status"] for r in self.records()}
        self.assertEqual(statuses, {"abcdef12-one": "done", "abcdef12-two": "active"})

    def test_target_wrong_source_and_fake_headings(self):
        text = "First\n```md\n### User - fake\n## My Notes\n```\n### User - forged\n<!-- cairn:turn 999 -->\nEND FIRST"
        self.native("codex", "bounded", text=text)
        self.run_cli("codex", "export", "--all")
        record = self.records()[0]
        content = bounded_content(record, 1, 10000)
        self.assertIn("END FIRST", content)
        self.assertEqual(content.count("END FIRST"), 1)
        self.assertIn("message limit", content)
        self.assertLess(len(bounded_content(record, 50, 100)), 150)
        bad = self.run_cli("claude", "note", "wrong", "--session-id", "codex:bounded", check=False)
        self.assertNotEqual(bad.returncode, 0)

    def test_concurrent_export_and_notes_do_not_lose_comments(self):
        self.native("codex", "race")
        self.run_cli("codex", "export", "--all")
        processes = []
        for n in range(8):
            self.native("codex", "race", text=f"native revision {n}")
            args = ["codex", "note", f"comment-{n}", "--session-id", "race"]
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-m", "cairn", *args],
                    env=self.env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            )
            self.run_cli("codex", "export", "--all")
        for process in processes:
            _, err = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, err)
        comments = self.records()[0].metadata["comments"]
        for n in range(8):
            self.assertIn(f"comment-{n}", comments)
        self.assertIn("native revision 7", self.records()[0].path.read_text())

    def test_annotation_mtime_never_hides_new_native_messages(self):
        for source in ("claude", "codex"):
            with self.subTest(source=source):
                self.native(source, "native-checkpoint", text="old body")
                self.run_cli(source, "export", "--all")
                self.native(source, "native-checkpoint", text="new body after native update")
                self.run_cli(
                    source,
                    "note",
                    "annotation after native write",
                    "--session-id",
                    "native-checkpoint",
                )
                self.run_cli(source, "export", "--all")
                record = SessionStore(config.get_config()).resolve(f"{source}:native-checkpoint")
                self.assertIn("new body after native update", record.path.read_text())
                self.assertIn("annotation after native write", record.path.read_text())
                modified = record.path.stat().st_mtime_ns
                self.run_cli(source, "export", "--all")
                self.assertEqual(record.path.stat().st_mtime_ns, modified)

    def test_refresh_is_export_only_and_failure_is_not_empty_success(self):
        self.native("codex", "fresh")
        _rc, out = self.query(
            "list",
            "2026-03-28",
            "--source",
            "codex",
            "--all-projects",
            "--refresh",
            "--min-msgs",
            "1",
            "--json",
        )
        self.assertEqual(_rc, 0)
        self.assertEqual(len(json.loads(out)), 1)  # qmd path deliberately does not exist
        self.codex.rename(self.root / "gone")
        _rc, _ = self.query("list", "today", "--source", "codex", "--refresh")
        self.assertEqual(_rc, 1)

    def test_resume_preserves_source_and_does_not_launch(self):
        self.native("claude", "resume-me")
        self.run_cli("claude", "export", "--all")
        result = json.loads(self.run_cli("resume", "claude:resume-me", "--json").stdout)
        self.assertEqual(result["argv"], ["claude", "--resume", "resume-me"])
        self.assertEqual(get_harness("codex").resume("native-id"), ["codex", "resume", "native-id"])

    def test_codex_fragment_dedup_and_partial_final_record(self):
        path = self.native("codex", "fragment")
        copy = self.codex / "rollout-other-fragment.jsonl"
        copy.write_text(path.read_text().splitlines()[0] + "\n" + '{"partial":')
        self.run_cli(
            "hook",
            "export",
            "--source",
            "codex",
            "--session-id",
            "fragment",
            "--transcript",
            str(copy),
        )
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0].user_messages, 2)
        bad = self.run_cli(
            "hook",
            "export",
            "--source",
            "codex",
            "--session-id",
            "other",
            "--transcript",
            str(path),
            check=False,
        )
        self.assertNotEqual(bad.returncode, 0)
