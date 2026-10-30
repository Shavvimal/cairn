"""Exporter guards: the parser stamp, the parser-drift guard and the findings store.

Every source store and output directory lives under a temp root (see
:class:`tests.test_shared_store.StoreFixture`); every transcript is invented.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import sqlite3
import time
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from cairn import config
from cairn.catalog import load_catalog
from cairn.frontmatter import parse_frontmatter
from cairn.jsonl import JsonlStats, iter_jsonl
from cairn.sources import guards
from cairn.sources.guards import FindingsStore, check_parse
from cairn.sources.spec_targets import SPEC_TARGETS
from tests.test_shared_store import StoreFixture

CODEX_ID = "019e0000-aaaa-7bbb-8ccc-000000000001"


def _jsonl(path: Path, records: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _age(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def _check(source: str, stats: JsonlStats, data, top_level: bool = True):
    return check_parse(
        source=source,
        native_id="demo-0001",
        path=Path("/tmp/demo.jsonl"),
        stats=stats,
        data=data,
        target=SPEC_TARGETS[source],
        adapter_version="2.0.0",
        top_level=top_level,
    )


def _stats(**types: int) -> JsonlStats:
    stats = JsonlStats(bytes=100, lines=sum(types.values()), records=sum(types.values()))
    stats.types.update({k.replace("_", "-"): v for k, v in types.items()})
    return stats


class GuardFixture(StoreFixture):
    def source(self, name: str = "codex"):
        """An in-process source bound to this fixture's config (module reload)."""
        module = importlib.reload(importlib.import_module(f"cairn.sources.{name}"))
        return getattr(module, f"{name.title()}Source")()

    def output_dir(self, collection: str = "codex-sessions") -> Path:
        return config.get_config().output_dir(collection)

    def findings(self) -> dict:
        path = self.root / "data/exporter/findings.json"
        return json.loads(path.read_text())["findings"] if path.exists() else {}

    def codex_rollout(self, records: list, sid: str = CODEX_ID) -> Path:
        return _jsonl(self.codex / f"rollout-2026-10-01T09-00-00-{sid}.jsonl", records)

    def drifted_codex(self, sid: str = CODEX_ID) -> Path:
        """A rollout whose messages moved to a record type the parser does not know."""
        return self.codex_rollout(
            [
                {"type": "session_meta", "payload": {"id": sid, "cwd": "/tmp/demo"}},
                {"type": "turn_item", "payload": {"role": "user", "text": "Investigate it"}},
                {"type": "turn_item", "payload": {"role": "assistant", "text": "Found it"}},
            ],
            sid,
        )


class TestParserStamp(GuardFixture):
    def test_rendered_frontmatter_has_exporter_keys_and_catalog_accepts_it(self):
        self.native("codex", "stamp-0001")
        self.run_cli("codex", "export", "--all")
        [path] = sorted(self.output_dir().rglob("*.md"))
        fm = parse_frontmatter(path.read_text(encoding="utf-8"))
        self.assertEqual(fm["exporter_version"], SPEC_TARGETS["codex"].adapter_version)
        self.assertEqual(fm["exporter_spec_target"], SPEC_TARGETS["codex"].checked)
        catalog = load_catalog(self.output_dir())
        self.assertEqual(catalog.errors, {})
        [(_, cached)] = list(catalog.items())
        self.assertEqual(cached["exporter_spec_target"], SPEC_TARGETS["codex"].checked)
        # A re-render regenerates the keys: exactly one stamp, never a second copy.
        source = self.source("codex")
        record = source.find_record("stamp-0001")
        assert record is not None
        data = {
            "date": "2026-03-28",
            "session_id": "stamp-0001",
            "messages": [],
            "message_count": 0,
        }
        source.export_one("stamp-0001", data, record.path)
        self.assertEqual(path.read_text(encoding="utf-8").count("exporter_version:"), 1)


class TestJsonlStats(GuardFixture):
    def test_iter_jsonl_stats_counts_lines_records_malformed_types(self):
        path = self.root / "demo.jsonl"
        path.write_text(
            '{"type":"user"}\n\n{"type":"user"}\n{"type":"api-request","x":1}\n{torn\n[1]\n'
        )
        stats = JsonlStats()
        records = [r for _, r in iter_jsonl(path, ('"type":"api-request',), stats)]
        self.assertEqual(records, [{"type": "user"}, {"type": "user"}, [1]])
        self.assertEqual((stats.lines, stats.records, stats.malformed), (5, 3, 1))
        self.assertEqual(stats.types, {"user": 2})
        self.assertEqual(stats.bytes, path.stat().st_size)

    def test_default_iter_jsonl_unchanged_without_stats(self):
        path = self.root / "demo.jsonl"
        path.write_text('{"type":"user"}\n{torn\n\n{"type":"assistant"}\n')
        self.assertEqual(
            list(iter_jsonl(path)), [(0, {"type": "user"}), (3, {"type": "assistant"})]
        )
        self.assertEqual(list(iter_jsonl(self.root / "missing.jsonl")), [])


class TestClassification(GuardFixture):
    def test_zero_messages_with_unknown_types_is_error(self):
        finding = _check("claude", _stats(turn_v2=4, attachment=1), {"messages_ordered": []})
        assert finding is not None
        self.assertEqual((finding.kind, finding.severity), ("zero-messages", "error"))
        self.assertIn("rendered 0 messages from 5 records (100 bytes)", finding.message)
        self.assertIn("spec target 2026-10-30 (Claude Code", finding.message)
        self.assertTrue(finding.line.startswith("Error: parser drift: claude:demo-0001 "))
        # Known conversational records that render nothing are drift too.
        finding = _check("claude", _stats(user=2, assistant=2), {"messages_ordered": []})
        self.assertEqual(finding.kind if finding else None, "zero-messages")

    def test_control_only_file_is_empty_info(self):
        finding = _check("claude", _stats(attachment=3, last_prompt=1), {"messages_ordered": []})
        assert finding is not None
        self.assertEqual((finding.kind, finding.severity), ("empty", "info"))
        # Codex control events share event_msg with messages: the finer kind decides.
        stats = JsonlStats(bytes=100, lines=2, records=2)
        stats.types.update({"session_meta": 1, "event_msg": 1})
        stats.kinds["event_msg/task_started"] = 1
        self.assertEqual(_check("codex", stats, {"messages": []}).kind, "empty")
        stats.kinds["event_msg/user_message"] = 1
        self.assertEqual(_check("codex", stats, {"messages": []}).kind, "zero-messages")

    def test_all_lines_malformed_is_unreadable_lines(self):
        stats = JsonlStats(lines=3, malformed=3, bytes=42)
        finding = _check("antigravity", stats, {"messages": []})
        assert finding is not None
        self.assertEqual((finding.kind, finding.severity), ("unreadable-lines", "error"))
        self.assertIn("0 of 3 lines parse as JSON (42 bytes)", finding.message)

    def test_no_user_turns_warns_for_top_level_only(self):
        data = {"messages": [{"role": "assistant", "text": "Done"}]}
        finding = _check("codex", _stats(response_item=1), data)
        assert finding is not None
        self.assertEqual((finding.kind, finding.severity), ("no-user-turns", "warn"))
        self.assertTrue(finding.line.startswith("Warning: parser drift: codex:demo-0001"))
        self.assertIn("1 assistant messages, 0 user messages", finding.message)
        self.assertIsNone(_check("codex", _stats(response_item=1), data, top_level=False))
        data["messages"].append({"role": "user", "text": "Go"})
        self.assertIsNone(_check("codex", _stats(response_item=2), data))


class TestAdapterGuards(GuardFixture):
    def test_claude_drift_refuses_write_and_keeps_last_good_document(self):
        sid = "c1a0de00-0000-4000-8000-000000000001"
        transcript = self.native("claude", sid)
        self.assertEqual(self.run_cli("claude", "export", "--all").returncode, 0)
        [document] = sorted(self.output_dir("claude-code-sessions").rglob("*.md"))
        before = document.read_text()
        _jsonl(
            transcript,
            [{"sessionId": sid, "type": "turn-v2", "body": "Investigate it"} for _ in range(3)],
        )
        result = self.run_cli("claude", "export", "--all", check=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f"Error: parser drift: claude:{sid} zero-messages: rendered 0", result.stderr)
        self.assertEqual(result.stderr.count("parser drift"), 1, result.stderr)
        self.assertEqual(document.read_text(), before)
        entry = self.findings()[f"claude:{sid}"]
        self.assertEqual(entry["record_types"], {"turn-v2": 3})
        self.assertEqual(entry["spec_target"], SPEC_TARGETS["claude"].checked)

    def test_codex_rollout_without_session_meta_is_no_session_id(self):
        path = self.codex_rollout([{"type": "event_msg", "payload": {"type": "user_message"}}])
        _age(path, 3600)
        result = self.run_cli("codex", "export", "--all", check=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f"Error: parser drift: codex:{CODEX_ID} no-session-id", result.stderr)
        self.assertEqual(self.findings()[f"codex:{CODEX_ID}"]["kind"], "no-session-id")

    def test_codex_live_child_is_not_drift(self):
        child, parent = CODEX_ID, "019e0000-aaaa-7bbb-8ccc-000000000002"
        path = self.codex_rollout(
            [
                {"type": "session_meta", "payload": {"id": child, "cwd": "/tmp/demo"}},
                {"type": "session_meta", "payload": {"id": parent, "cwd": "/tmp/demo"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Replayed parent turn"}],
                    },
                },
            ],
            child,
        )
        self.source("codex")  # rebinds the module to this fixture's store
        mod = importlib.import_module("cairn.sources.codex")
        self.assertIsNone(mod.parse_rollout(path))  # still replaying, written just now
        result = self.run_cli("codex", "export", "--all", check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("parser drift", result.stderr)
        self.assertEqual(self.findings(), {})

    def test_cursor_headers_without_bubbles_is_zero_messages(self):
        data = json.loads(self.config.read_text())
        store = self.root / "cursor"
        (store / "globalStorage").mkdir(parents=True)
        (store / "globalStorage/state.vscdb").touch()
        data["collections"]["cursor-sessions"] = {"store": str(store)}
        self.config.write_text(json.dumps(data))
        config.get_config.cache_clear()
        composer_id = "c0de0000-1111-4222-8333-000000000001"
        composer = {
            "composerId": composer_id,
            "createdAt": 1767000000000,
            "lastUpdatedAt": 1767000000000,
            "fullConversationHeadersOnly": [
                {"type": 1, "bubbleId": "b1"},
                {"type": 2, "bubbleId": "b2"},
            ],
        }
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (f"composerData:{composer_id}", json.dumps(composer)),
        )
        source = self.source("cursor")
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(source.export_composers(conn, quiet=True), 0)
        self.assertIn(f"Error: parser drift: cursor:{composer_id} zero-messages", err.getvalue())
        [finding] = source.report.errors
        self.assertEqual((finding.kind, finding.records), ("zero-messages", 2))
        self.assertEqual(list(self.output_dir("cursor-sessions").rglob("*.md")), [])


class TestFindingsStore(GuardFixture):
    def test_finding_cleared_after_clean_parse(self):
        self.drifted_codex()
        self.assertEqual(self.run_cli("codex", "export", "--all", check=False).returncode, 1)
        self.assertIn(f"codex:{CODEX_ID}", self.findings())
        self.native("codex", CODEX_ID).rename(self.codex / f"rollout-x-{CODEX_ID}.jsonl")
        next(self.codex.glob("rollout-2026-*")).unlink()
        result = self.run_cli("codex", "export", "--all", check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.findings(), {})

    def test_findings_written_only_on_change(self):
        store = FindingsStore(self.root / "data")
        source_file = _jsonl(self.root / "demo.jsonl", [{"type": "x"}])
        finding = _check("claude", _stats(turn_v2=1), {"messages_ordered": []})
        assert finding is not None
        finding.path = str(source_file)
        with mock.patch.object(guards, "atomic_write", wraps=guards.atomic_write) as write:
            store.clear("claude:demo-0001")  # no entry: no I/O
            store.record(finding)
            store.record(guards.Finding.from_dict(finding.as_dict()))  # same kind
            self.assertEqual(write.call_count, 1)
            store.clear("claude:demo-0001", (guards.DOCUMENT_KIND,))
            self.assertEqual(write.call_count, 1)  # another kind is open
            store.clear("claude:demo-0001")
            self.assertEqual(write.call_count, 2)
        self.assertEqual(FindingsStore(self.root / "data").findings, {})
        # A corrupt file reads as empty, with one warning, and is rewritten on change.
        store.path.write_text("{not json")
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(FindingsStore(self.root / "data").findings, {})
        self.assertIn("rewritten on the next change", err.getvalue())

    def test_export_json_prints_one_object_and_exits_1_on_error(self):
        self.native("codex", "good-0001")
        self.drifted_codex()
        result = self.run_cli("codex", "export", "--all", "--json", check=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["source"], "codex")
        self.assertEqual(report["exit"], 1)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(len(report["written"]), 1)
        self.assertEqual(
            [(f["ref"], f["kind"]) for f in report["findings"]],
            [(f"codex:{CODEX_ID[:8]}", "zero-messages")],
        )
        self.assertEqual(report["spec_target"]["checked"], SPEC_TARGETS["codex"].checked)
        self.assertIn("Exported 1 sessions", result.stderr)

    def test_hook_export_reports_parser_drift_message(self):
        self.drifted_codex()
        result = self.run_cli(
            "hook", "export", "--source", "codex", "--session-id", CODEX_ID, check=False
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f"Error: parser drift: codex:{CODEX_ID} zero-messages", result.stderr)
        self.assertEqual(result.stderr.count("parser drift"), 1, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_unparseable_document_recorded(self):
        sid = "c1a0de00-0000-4000-8000-000000000002"
        transcript = self.native("claude", sid)
        self.run_cli("claude", "export", "--all")
        [document] = sorted(self.output_dir("claude-code-sessions").rglob("*.md"))
        broken = document.read_text().replace("\nstatus:", "\ntags: [a, b]\nstatus:", 1)
        document.write_text(broken)
        self.native("claude", sid, text="A later turn")
        self.assertTrue(transcript.exists())
        result = self.run_cli("claude", "export", "--all", check=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(
            f"Error: refusing to re-export claude:{sid}: existing document {document}",
            result.stderr,
        )
        self.assertIn("fix or move it", result.stderr)
        self.assertEqual(document.read_text(), broken)
        self.assertEqual(self.findings()[f"claude:{sid}"]["kind"], "unparseable-document")
