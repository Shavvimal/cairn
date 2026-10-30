"""Per-source parser fixtures: each adapter parses an invented transcript and the
message recall is scored exactly, so a parser change shows up as a number.

The fixtures under ``tests/fixtures/<source>/`` are invented (ids, paths, text). The
score helpers live in :mod:`tests.parser_fixtures`.
"""

from __future__ import annotations

import importlib
import json
import shutil
import sqlite3
from pathlib import Path

from cairn import config
from cairn.jsonl import JsonlStats
from cairn.registry import SOURCES
from cairn.sources.guards import check_parse
from cairn.sources.spec_targets import SPEC_TARGETS
from tests.parser_fixtures import expected_files, input_of, load_expected, score
from tests.test_shared_store import StoreFixture

# Sources that parse a native file; Granola (remote API) and service docs (byte
# copies) never parse a transcript, so they have no spec target.
FILE_SOURCES = ("claude", "codex", "antigravity", "cursor")


class FixtureParse(StoreFixture):
    def setUp(self):
        super().setUp()
        store = self.root / "cursor"
        (store / "globalStorage").mkdir(parents=True)
        data = json.loads(self.config.read_text())
        data["collections"]["cursor-sessions"] = {"store": str(store)}
        self.config.write_text(json.dumps(data))
        config.get_config.cache_clear()

    def adapter(self, name: str):
        """The source module bound to this fixture's temp config."""
        return importlib.reload(importlib.import_module(f"cairn.sources.{name}"))

    def parse(self, source: str, path: Path) -> tuple[dict | None, JsonlStats, str]:
        """``(data, stats, native_id)`` from the source's own parse functions."""
        mod = self.adapter(source)
        if source == "claude":
            stats = JsonlStats()
            data = mod.extract_session_data(mod._records(path, stats=stats))
            return data, stats, str(data.get("session_id") or path.stem)
        if source == "codex":
            stats = JsonlStats(kind_of=mod.record_kind)
            data = mod.parse_rollout(path, stats)
            return data, stats, str(data["session_id"]) if data else path.stem
        if source == "antigravity":
            conversation = "a9000000-0000-4000-8000-000000000001"
            logs = self.antigravity / "brain" / conversation / ".system_generated" / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            transcript = logs / "transcript_full.jsonl"
            shutil.copyfile(path, transcript)
            stats = JsonlStats()
            data = mod.finish(mod.parse_transcript(transcript, conversation, stats), None)
            return data, stats, conversation
        rows = json.loads(path.read_text(encoding="utf-8"))
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT INTO cursorDiskKV VALUES (?, ?)", [(k, json.dumps(v)) for k, v in rows.items()]
        )
        [composer] = [v for k, v in rows.items() if k.startswith("composerData:")]
        data = mod.extract_composer_data(composer, {})
        data["messages_ordered"] = mod.fetch_bubbles(
            conn, composer["composerId"], data["bubble_order"], data.get("legacy_turns")
        )
        return data, mod.composer_stats(composer, data), composer["composerId"]

    def finding(self, source: str, path: Path):
        data, stats, native_id = self.parse(source, path)
        top_level = source != "cursor" and not (data or {}).get("parent_session_id")
        return check_parse(
            source=source,
            native_id=native_id,
            path=path,
            stats=stats,
            data=data,
            target=SPEC_TARGETS[source],
            adapter_version="2.0.0",
            top_level=top_level,
        ), data


class TestParserFixtures(FixtureParse):
    def test_every_fixture_scores_full_recall(self):
        cases = [p for p in expected_files() if load_expected(p)["expected_finding"] is None]
        self.assertGreaterEqual({load_expected(p)["source"] for p in cases}, set(FILE_SOURCES))
        for expected_path in cases:
            expected = load_expected(expected_path)
            with self.subTest(fixture=expected["fixture_id"]):
                source = expected["source"]
                finding, data = self.finding(source, input_of(expected_path))
                assert data is not None
                messages = data.get("messages_ordered", data.get("messages", []))
                result = score(expected["fixture_id"], expected["expected_messages"], messages)
                target = SPEC_TARGETS[source]
                report = result.describe(target.describe())
                self.assertEqual(result.recall, 1.0, report)
                self.assertEqual(len(result.parsed_roles), result.expected, report)
                self.assertEqual(result.parsed_roles, expected["expected_roles"], report)
                self.assertIsNone(finding, finding.message if finding else "")

    def test_drifted_fixtures_raise_zero_messages(self):
        drifted = [p for p in expected_files() if load_expected(p)["expected_finding"]]
        self.assertTrue(drifted)
        for expected_path in drifted:
            expected = load_expected(expected_path)
            with self.subTest(fixture=expected["fixture_id"]):
                finding, _data = self.finding(expected["source"], input_of(expected_path))
                assert finding is not None, expected["fixture_id"]
                self.assertEqual(finding.kind, expected["expected_finding"])
                self.assertEqual(finding.severity, "error")

    def test_fixture_spec_target_matches_adapter(self):
        for expected_path in expected_files():
            expected = load_expected(expected_path)
            source = expected["source"]
            with self.subTest(fixture=expected["fixture_id"]):
                self.assertEqual(expected["spec_target"], SPEC_TARGETS[source].checked)
                adapter = self.adapter(source)
                cls = getattr(adapter, f"{source.title()}Source")
                self.assertIs(cls.spec_target, SPEC_TARGETS[source])
                self.assertEqual(cls.version, SPEC_TARGETS[source].adapter_version)

    def test_every_file_source_has_a_spec_target(self):
        registered = {s.source for s in SOURCES if s.source in FILE_SOURCES}
        self.assertEqual(registered, set(FILE_SOURCES))
        self.assertEqual(set(SPEC_TARGETS), set(FILE_SOURCES))
        for source, target in SPEC_TARGETS.items():
            with self.subTest(source=source):
                self.assertRegex(target.checked, r"^\d{4}-\d{2}-\d{2}$")
                self.assertTrue(target.harness_version)
                self.assertTrue(target.conversational)
                self.assertFalse(target.conversational & target.ignored)
                self.assertTrue((Path(__file__).parent / "fixtures" / source).is_dir())

    def test_recall_scorer_reports_partial_recall(self):
        expected = [
            {"role": "user", "text_contains": "Investigate the widget"},
            {"role": "assistant", "text_contains": "off-by-one"},
            {"role": "assistant", "text_contains": "off-by-one"},
        ]
        messages = [
            {"role": "assistant", "text": "Found the off-by-one"},
            {"role": "tool", "text": "Investigate the widget"},
        ]
        result = score("claude/basic", expected, messages)
        self.assertEqual((result.matched, result.expected), (1, 3))
        self.assertEqual(
            result.describe("spec target 2026-10-30 (Claude Code 0.0.0)"),
            'claude/basic: message recall 1/3 (0.33); missing: user "Investigate the", '
            'assistant "off-by-one"; spec target 2026-10-30 (Claude Code 0.0.0)',
        )
