"""``cairn recall gaps`` and the ``coverage`` object on ``recall list`` / ``recall digest``.

QMD is always a ``#!python`` stub printing an invented ``qmd collection list``
fixture (or no binary at all); no test depends on a real qmd install.
"""

from __future__ import annotations

import io
import json
import os
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from cairn import config, qmd, recall
from cairn import recall_gaps as gaps
from cairn.recall_gaps import CollectionFacts, Facts, IndexFacts, classify
from tests.test_shared_store import StoreFixture

NOW = datetime(2026, 10, 30, 13, 50, 2, tzinfo=UTC)


def collection(**overrides) -> CollectionFacts:
    """A clean, enabled, exported Claude collection; ``overrides`` add the gap."""
    values: dict = {
        "collection": "claude-code-sessions",
        "source": "claude",
        "enabled": True,
        "store": Path("/tmp/claude"),
        "store_exists": True,
        "session": True,
        "markdown_files": 10,
        "exported_docs": 10,
        "last_export_at": NOW - timedelta(minutes=5),
        "newest_activity_at": NOW - timedelta(minutes=6),
        "native_newest_at": NOW - timedelta(minutes=6),
        "native_basis": "file",
        "index": IndexFacts(True, 10, 0, "cairn"),
    }
    values.update(overrides)
    return CollectionFacts(**values)


def facts(*collections: CollectionFacts, **overrides) -> Facts:
    values: dict = {
        "qmd": "available",
        "stamp_at": NOW - timedelta(minutes=5),
        "stamp_basis": "cairn",
        "rg": True,
    }
    values.update(overrides)
    return Facts(tuple(collections) or (collection(),), **values)


def lagging(minutes: int, basis: str = "file", **overrides) -> CollectionFacts:
    """Native activity ``minutes`` after the newest export, not yet exported."""
    values: dict = {
        "newest_activity_at": NOW - timedelta(minutes=minutes + 1),
        "last_export_at": NOW - timedelta(minutes=minutes),
        "native_newest_at": NOW - timedelta(minutes=1),
        "native_basis": basis,
    }
    values.update(overrides)
    return collection(**values)


class TestClassify(StoreFixture):
    def kinds(self, found) -> list[tuple[str, str]]:
        return [(g.kind, g.severity) for g in found]

    def test_clean_facts_give_clean_status(self):
        found = classify(facts(), NOW)
        self.assertEqual(found, [])
        self.assertEqual(gaps.status_of(found), "clean")

    def test_export_lag_over_grace_is_warn_and_under_is_info(self):
        over = classify(facts(lagging(90)), NOW)
        self.assertEqual(self.kinds(over), [("export-lag", "warn")])
        self.assertEqual(over[0].facts, {"lag_s": 90 * 60, "basis": "file"})
        self.assertIn("(1h30m behind)", over[0].message)
        self.assertEqual(over[0].fix, "cairn sync")
        under = classify(facts(lagging(20)), NOW)
        self.assertEqual(self.kinds(under), [("export-lag", "info")])
        self.assertEqual(gaps.status_of(under), "clean")

    def test_export_written_after_native_change_is_not_lag(self):
        caught_up = lagging(90, last_export_at=NOW)
        self.assertEqual(classify(facts(caught_up), NOW), [])

    def test_store_basis_lag_is_never_warn(self):
        for source, name in (("cursor", "cursor-sessions"), ("granola", "granola-sessions")):
            found = classify(facts(lagging(600, "store", source=source, collection=name)), NOW)
            self.assertEqual(self.kinds(found), [("export-lag", "info")], source)
            self.assertIn("not proof of a new session", found[0].message)

    def test_disabled_source_is_warn_only_when_requested(self):
        disabled = collection(
            source="cursor", collection="cursor-sessions", enabled=False, index=None
        )
        self.assertEqual(self.kinds(classify(facts(disabled), NOW)), [("source-disabled", "info")])
        named = classify(facts(disabled), NOW, requested=["cursor"])
        self.assertEqual(self.kinds(named), [("source-disabled", "warn")])
        self.assertIn("sync.enabled=false; exports stop at", named[0].message)

    def test_never_exported_and_unreadable_are_warn(self):
        never = collection(
            exported_docs=0,
            markdown_files=0,
            last_export_at=None,
            newest_activity_at=None,
            index=IndexFacts(False),
        )
        found = classify(facts(never), NOW)
        self.assertEqual(self.kinds(found), [("never-exported", "warn")])
        self.assertEqual(found[0].fix, "cairn claude export --all")
        unreadable = classify(facts(collection(unreadable=2)), NOW)
        self.assertEqual(self.kinds(unreadable), [("unreadable-exports", "warn")])

    def test_window_filter_keeps_only_intersecting_lag(self):
        lag = lagging(90)  # unexported interval: the last 91 minutes before NOW
        today = (NOW.replace(hour=0, minute=0, second=0), NOW + timedelta(days=1))
        last_week = (NOW - timedelta(days=8), NOW - timedelta(days=1))
        self.assertEqual(len(classify(facts(lag), NOW, window=today)), 1)
        self.assertEqual(classify(facts(lag), NOW, window=last_week), [])
        # Index gaps never reach a window (coverage is store scope only).
        index_lag = collection(index=IndexFacts(True, 10, 3, "cairn"))
        self.assertEqual(len(classify(facts(index_lag), NOW)), 1)
        self.assertEqual(classify(facts(index_lag), NOW, window=today), [])
        # A gap with no dates applies to every window.
        unreadable = collection(unreadable=1)
        self.assertEqual(len(classify(facts(unreadable), NOW, window=last_week)), 1)

    def test_verdict_found_clean_miss_degraded_miss(self):
        window = (NOW - timedelta(days=1), NOW + timedelta(days=1))
        cases = (
            (facts(), True, "found"),
            (facts(), False, "clean-miss"),
            (facts(lagging(90)), False, "degraded-miss"),
            (facts(lagging(90)), True, "found"),
        )
        cfg = config.get_config()
        for given, found, verdict in cases:
            with mock.patch.object(gaps, "gather", return_value=given):
                cov = gaps.coverage(cfg, ["claude"], window, found, now=NOW)
            self.assertEqual(cov["verdict"], verdict)
            self.assertEqual(cov["scope"], "store")

    def test_index_gaps_and_globals(self):
        found = classify(
            facts(
                collection(index=IndexFacts(False)),
                collection(
                    collection="codex-sessions",
                    source="codex",
                    index=IndexFacts(True, 4, 0, "cairn"),
                ),
                rg=False,
            ),
            NOW,
        )
        self.assertEqual(
            self.kinds(found),
            [
                ("collection-missing", "warn"),
                ("index-count", "warn"),
                ("rg-missing", "info"),
            ],
        )
        # Registered, no stamp, and an age cairn cannot read: index-lag is unknown.
        unknown = classify(facts(collection(index=IndexFacts(True, 10, None, None))), NOW)
        self.assertEqual(self.kinds(unknown), [("index-stamp-missing", "info")])
        missing = classify(facts(qmd="missing"), NOW)
        self.assertEqual(self.kinds(missing), [("index-unknown", "warn")])
        self.assertIn("temporal recall still works", missing[0].message)

    def test_per_file_lag_ignores_other_documents(self):
        # The newest export is from a minute ago (another session, an exchange), but
        # one native file changed 90 minutes after its own export's activity.
        changed = NOW - timedelta(minutes=30)
        given = collection(
            last_export_at=NOW - timedelta(minutes=1),
            unexported=((changed, changed - timedelta(minutes=90)),),
        )
        found = classify(facts(given), NOW)
        self.assertEqual(self.kinds(found), [("export-lag", "warn")])
        self.assertEqual(found[0].facts, {"lag_s": 90 * 60, "basis": "file", "sessions": 1})
        self.assertIn("not exported for 1 session", found[0].message)
        self.assertEqual(found[0].interval, (changed - timedelta(minutes=90), changed))
        # A file with no export is behind from its mtime to now.
        fresh = collection(unexported=((NOW - timedelta(minutes=10), None),))
        self.assertEqual(self.kinds(classify(facts(fresh), NOW)), [("export-lag", "info")])
        stale = collection(unexported=((NOW - timedelta(hours=3), None),))
        found = classify(facts(stale), NOW)
        self.assertEqual(self.kinds(found), [("export-lag", "warn")])
        self.assertEqual(found[0].interval, (None, NOW - timedelta(hours=3)))
        self.assertEqual(classify(facts(collection(unexported=())), NOW), [])

    def test_export_lag_boundary_and_clock_skew(self):
        native = NOW - timedelta(minutes=1)  # lagging()'s native change
        grace = timedelta(seconds=gaps.EXPORT_GRACE_S)
        at_grace = lagging(90, newest_activity_at=native - grace)
        self.assertEqual(self.kinds(classify(facts(at_grace), NOW)), [("export-lag", "info")])
        past = lagging(90, newest_activity_at=native - grace - timedelta(seconds=1))
        self.assertEqual(self.kinds(classify(facts(past), NOW)), [("export-lag", "warn")])
        future = lagging(90, native_newest_at=NOW + timedelta(minutes=5))
        found = classify(facts(future), NOW)
        self.assertTrue(found[0].message.endswith("(clock skew)"))
        self.assertEqual(found[0].interval, (NOW - timedelta(minutes=91), NOW))
        near = lagging(90, native_newest_at=NOW + timedelta(seconds=gaps.CLOCK_SKEW_S))
        self.assertNotIn("clock skew", classify(facts(near), NOW)[0].message)

    def test_window_boundary_is_half_open(self):
        lag = lagging(90)  # unexported interval (NOW-91m, NOW-1m]
        low, high = NOW - timedelta(minutes=91), NOW - timedelta(minutes=1)
        self.assertEqual(classify(facts(lag), NOW, window=(high - timedelta(hours=1), low)), [])
        self.assertEqual(len(classify(facts(lag), NOW, window=(high, NOW))), 1)
        self.assertEqual(classify(facts(lag), NOW, window=(high + timedelta(seconds=1), NOW)), [])

    def test_unreadable_exports_of_a_disabled_source_still_warn(self):
        disabled = collection(
            source="cursor", collection="cursor-sessions", enabled=False, unreadable=1, index=None
        )
        found = classify(facts(disabled), NOW)
        self.assertEqual(
            self.kinds(found), [("unreadable-exports", "warn"), ("source-disabled", "info")]
        )

    def test_repair_commands_parse(self):
        import argparse

        from cairn import cli
        from cairn.registry import SOURCES

        window = argparse.ArgumentParser(parents=[cli._window_parent()])
        for source in SOURCES:
            command = gaps.export_command(source.source).split()
            self.assertEqual(command[:3], ["cairn", source.source, "export"])
            if source.windowed:
                self.assertEqual(command[3:], ["--all"])
                window.parse_args(command[3:])
            else:
                self.assertEqual(command[3:], [])
        never = collection(
            source="service-docs",
            collection="service-docs",
            session=False,
            exported_docs=0,
            index=None,
        )
        self.assertEqual(classify(facts(never), NOW)[0].fix, "cairn service-docs export")

    def test_skipped_digest_sessions_are_a_warn(self):
        skipped = [
            {"ref": "claude:a", "reason": "corrupt"},
            {"ref": "claude:b", "reason": "legacy"},
            {"ref": "codex:c", "reason": "corrupt"},
        ]
        found = gaps.skipped_gaps(skipped)
        self.assertEqual(
            [(g.kind, g.severity, g.collection, g.fix) for g in found],
            [
                ("unreadable-exports", "warn", "claude-code-sessions", "cairn claude export --all"),
                ("unreadable-exports", "warn", "codex-sessions", None),
            ],
        )
        self.assertEqual(found[0].facts, {"count": 2, "skipped": ["claude:a", "claude:b"]})
        window = (NOW - timedelta(days=1), NOW)
        with mock.patch.object(gaps, "gather", return_value=facts()):
            cov = gaps.coverage(config.get_config(), ["claude"], window, False, now=NOW)
            self.assertEqual(cov["verdict"], "clean-miss")
            cov = gaps.coverage(
                config.get_config(), ["claude"], window, False, skipped=skipped, now=NOW
            )
        self.assertEqual(cov["verdict"], "degraded-miss")

    def test_is_deterministic(self):
        given = facts(
            lagging(90),
            collection(collection="codex-sessions", source="codex", unreadable=1),
            rg=False,
        )
        first = json.dumps(gaps.to_json(given, classify(given, NOW), NOW), sort_keys=True)
        second = json.dumps(gaps.to_json(given, classify(given, NOW), NOW), sort_keys=True)
        self.assertEqual(first, second)


# An invented `qmd collection list` in QMD's block format; the stub prints it and
# logs every call so a test can prove coverage never asks QMD.
COLLECTION_LIST = """\
Collections (2):

claude-code-sessions (qmd://claude-code-sessions/)
  Pattern:  **/*.md
  Files:    {claude_files}
  Updated:  {updated}

codex-sessions (qmd://codex-sessions/)
  Pattern:  **/*.md
  Files:    0
  Updated:  5m ago
"""

QMD_STUB = """\
import sys, time
from pathlib import Path
here = Path(__file__).resolve().parent
with open(here / "qmd-calls.log", "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
time.sleep({sleep})
print({listing!r})
"""


class GapsFixture(StoreFixture):
    def stub_qmd(self, *, claude_files=1, updated="5m ago", sleep=0.0) -> Path:
        listing = COLLECTION_LIST.format(claude_files=claude_files, updated=updated)
        return self.fake_qmd(QMD_STUB.format(sleep=sleep, listing=listing))

    def qmd_calls(self) -> list[str]:
        path = self.root / "qmd-calls.log"
        return path.read_text().splitlines() if path.exists() else []

    def export(self, source: str = "claude") -> None:
        self.run_cli(source, "export", "--all")

    def cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = recall.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def gather(self, **kwargs) -> Facts:
        config.get_config.cache_clear()
        return gaps.gather(config.get_config(), **kwargs)

    def by_name(self, given: Facts, name: str) -> CollectionFacts:
        return next(c for c in given.collections if c.collection == name)

    def age_exports(self, seconds: float) -> None:
        stamp = time.time() - seconds
        for path in (self.root / "data").rglob("*.md"):
            os.utime(path, (stamp, stamp))


class TestGather(GapsFixture):
    def test_current_session_file_is_excluded(self):
        self.native("claude", "live-session-0001")
        claude = self.by_name(self.gather(index=False), "claude-code-sessions")
        self.assertIsNotNone(claude.native_newest_at)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "live-session-0001"}):
            claude = self.by_name(self.gather(index=False), "claude-code-sessions")
        self.assertIsNone(claude.native_newest_at)
        self.assertEqual(claude.native_basis, "file")

    def test_non_session_collection_is_not_parsed(self):
        docs = self.root / "docs-src"
        docs.mkdir()
        (docs / "guide.md").write_text("# Guide\n")
        data = json.loads(self.config.read_text())
        data["collections"]["service-docs"] = {
            "service_sources": {"api": {"path": str(docs), "description": "API docs"}}
        }
        self.config.write_text(json.dumps(data))
        mirrored = self.root / "data" / "service-docs" / "api"
        mirrored.mkdir(parents=True)
        (mirrored / "guide.md").write_text("---\ntitle: Guide\n---\n# Guide\n")
        loaded: list[Path] = []
        real = gaps.load_catalog

        def spy(output_dir: Path):
            catalog = real(output_dir)
            loaded.append(output_dir)
            if output_dir.name == "service-docs":
                self.assertEqual(catalog.stats["parsed"], 0)
            return catalog

        with mock.patch.object(gaps, "load_catalog", spy):
            given = self.gather(index=False)
        self.assertNotIn("service-docs", [p.name for p in loaded])
        docs_facts = self.by_name(given, "service-docs")
        self.assertEqual(docs_facts.markdown_files, 1)
        self.assertEqual(docs_facts.native_basis, "file")
        self.assertTrue(docs_facts.store_exists)

    def test_stamp_for_other_database_is_ignored(self):
        self.stub_qmd()
        self.native("claude", "stamp-0001")
        self.export()
        qmd.record_update(time.time_ns(), ["claude-code-sessions"])
        self.assertEqual(self.gather().stamp_basis, "cairn")
        with mock.patch.dict(os.environ, {"INDEX_PATH": str(self.root / "other.sqlite")}):
            given = self.gather()
        self.assertIsNone(given.stamp_basis)
        claude = self.by_name(given, "claude-code-sessions")
        self.assertEqual(claude.index.basis if claude.index else None, "qmd-relative")

    def test_unindexed_count_uses_stamp_started_ns(self):
        self.stub_qmd(claude_files=2)
        for sid in ("stamp-0001", "stamp-0002"):
            self.native("claude", sid)
        self.export()
        self.age_exports(120)
        started = time.time_ns() - 60 * 10**9  # between the two export ages
        newer = next((self.root / "data" / "claude-code-sessions").rglob("*.md"))
        os.utime(newer, None)  # now: after the stamp
        qmd.record_update(started, ["claude-code-sessions"])
        claude = self.by_name(self.gather(), "claude-code-sessions")
        self.assertEqual(claude.index, IndexFacts(True, 2, 1, "cairn"))
        found = classify(self.gather(), datetime.now(UTC))
        lag = [g for g in found if g.kind == "index-lag"]
        self.assertEqual(len(lag), 1)
        self.assertIn("1 files changed after the last qmd update", lag[0].message)

    def test_qmd_relative_fallback_is_a_lower_bound(self):
        self.stub_qmd(claude_files=2, updated="1h ago")
        for sid in ("rel-0001", "rel-0002"):
            self.native("claude", sid)
        self.export()
        files = sorted((self.root / "data" / "claude-code-sessions").rglob("*.md"))
        # One file changed 20 minutes ago (after "1h ago" by any reading), one 45
        # minutes ago (inside the rounding half-unit, so not claimed).
        for path, minutes in zip(files, (20, 45), strict=True):
            stamp = time.time() - minutes * 60
            os.utime(path, (stamp, stamp))
        claude = self.by_name(self.gather(), "claude-code-sessions")
        self.assertEqual(claude.index, IndexFacts(True, 2, 1, "qmd-relative"))
        kinds = [g.kind for g in classify(self.gather(), datetime.now(UTC))]
        self.assertIn("index-stamp-missing", kinds)

    def touch(self, path: Path, seconds_ago: float) -> None:
        stamp = time.time() - seconds_ago
        os.utime(path, (stamp, stamp))

    def test_other_writes_do_not_hide_a_session_export_lag(self):
        first = self.native("claude", "pf-0001")
        self.native("claude", "pf-0002")
        self.export()
        self.age_exports(3 * 3600)
        # pf-0001 changed an hour after its export and was not exported again.
        self.touch(first, 2 * 3600)
        folder = self.root / "data" / "claude-code-sessions"
        other = next(p for p in folder.rglob("*.md") if "pf-0002" in p.read_text())
        os.utime(other, None)  # a note on the other session, written now
        (folder / "exchange-codex-0001.md").write_text(
            "---\ntype: peer-exchange\ntitle: x\n---\nbody\n"
        )
        claude = self.by_name(self.gather(index=False), "claude-code-sessions")
        self.assertEqual(len(claude.unexported or ()), 1)
        lag = [g for g in classify(self.gather(index=False), datetime.now(UTC))]
        self.assertEqual([g.kind for g in lag], ["export-lag"])
        self.assertEqual(lag[0].facts["sessions"], 1)
        # Exporting the session clears it.
        self.export()
        claude = self.by_name(self.gather(index=False), "claude-code-sessions")
        self.assertEqual(claude.unexported, ())

    def test_session_records_recall_rejects_are_unreadable(self):
        self.native("claude", "ok-0001")
        self.export()
        folder = self.root / "data" / "claude-code-sessions"
        (folder / "wrong-source.md").write_text(
            "---\ntype: claude-session\nsession_id: wrong-0001\nsource: codex\n---\nbody\n"
        )
        (folder / "no-id.md").write_text("---\ntype: claude-session\ntitle: x\n---\nbody\n")
        claude = self.by_name(self.gather(index=False), "claude-code-sessions")
        self.assertEqual((claude.exported_docs, claude.unreadable), (1, 2))
        kinds = [g.kind for g in classify(self.gather(index=False), datetime.now(UTC))]
        self.assertIn("unreadable-exports", kinds)

    @unittest.skipIf(os.geteuid() == 0, "root reads every folder")
    def test_unreadable_native_folder_is_reported(self):
        self.native("claude", "perm-0001")
        locked = self.claude / "-locked"
        locked.mkdir()
        (locked / "hidden.jsonl").write_text("{}\n")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        claude = self.by_name(self.gather(index=False), "claude-code-sessions")
        self.assertIsNotNone(claude.native_newest_at)
        self.assertIsNotNone(claude.store_error)
        assert claude.store_error is not None
        self.assertTrue(claude.store_error.startswith("native store read incompletely: "))
        found = classify(self.gather(index=False), datetime.now(UTC))
        self.assertIn(("store-missing", "info"), [(g.kind, g.severity) for g in found])

    def test_corrupt_stamp_time_is_absent(self):
        for started in (10**100, 0, -5):
            qmd.stamp_path().parent.mkdir(parents=True, exist_ok=True)
            qmd.stamp_path().write_text(
                json.dumps(
                    {
                        "version": qmd.STAMP_VERSION,
                        "database": "x",
                        "started_ns": started,
                        "collections": [],
                    }
                )
            )
            self.assertIsNone(qmd.read_stamp(), started)

    def test_stamp_write_failure_warns_and_keeps_the_old_stamp(self):
        qmd.record_update(1_000, ["claude-code-sessions"])
        err = io.StringIO()
        with (
            mock.patch.object(qmd, "atomic_write", side_effect=OSError("disk full")),
            redirect_stderr(err),
        ):
            qmd.record_update(2_000, ["claude-code-sessions"])
        self.assertEqual(
            err.getvalue(), "Warning: could not record the qmd update time: disk full\n"
        )
        stamp = qmd.read_stamp()
        self.assertEqual(stamp["started_ns"] if stamp else None, 1_000)

    def test_malformed_stamps_are_absent(self):
        path = qmd.stamp_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        good = {"version": qmd.STAMP_VERSION, "database": "x", "started_ns": 5, "collections": []}
        for bad in ("not json", "[]", {**good, "version": 2}, {**good, "started_ns": True}):
            path.write_text(bad if isinstance(bad, str) else json.dumps(bad))
            self.assertIsNone(qmd.read_stamp(), bad)
        path.write_text(json.dumps(good))
        self.assertIsNotNone(qmd.read_stamp())

    def test_session_id_in_a_project_folder_name_does_not_hide_the_project(self):
        # The live session's id appears in the encoded working directory of another
        # session's project folder; only the live session's own file is skipped.
        folder = self.claude / "-tmp-live-session-0001-worktree"
        folder.mkdir()
        (folder / "other-0001.jsonl").write_text("{}\n")
        self.native("claude", "live-session-0001")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "live-session-0001"}):
            claude = self.by_name(self.gather(index=False), "claude-code-sessions")
        self.assertIsNotNone(claude.native_newest_at)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "live-session-0001"}):
            native = gaps._native("claude", config.get_config().collections["claude-code-sessions"])
        self.assertEqual([ids for ids, _ in native.files or ()], [("other-0001",)])

    def test_claude_probe_skips_legacy_agent_files_and_reads_subagents(self):
        parent = self.native("claude", "sub-0001")
        legacy = parent.parent / "agent-legacy.jsonl"
        legacy.write_text("{}\n")
        child = parent.with_suffix("") / "subagents" / "agent-a1.jsonl"
        child.parent.mkdir(parents=True)
        child.write_text("{}\n")
        self.touch(parent, 600)
        self.touch(legacy, 60)
        self.touch(child, 300)
        native = gaps._native("claude", config.get_config().collections["claude-code-sessions"])
        self.assertEqual([ids for ids, _ in native.files or ()], [("sub-0001",)])
        self.assertEqual(native.newest_at, gaps._from_ns(child.stat().st_mtime_ns))

    def test_codex_and_antigravity_compare_files_with_their_exports(self):
        for source in ("codex", "antigravity"):
            self.native(source, f"{source}-pf-0001")
            self.export(source)
        given = self.gather(index=False)
        for name in ("codex-sessions", "antigravity-sessions"):
            self.assertEqual(self.by_name(given, name).unexported, (), name)
        rollout = self.codex / "rollout-codex-pf-0001.jsonl"
        self.touch(rollout, -5)  # changed after its export
        codex = self.by_name(self.gather(index=False), "codex-sessions")
        self.assertEqual(len(codex.unexported or ()), 1)

    def test_unreadable_store_exists(self):
        self.claude.chmod(0)
        self.addCleanup(self.claude.chmod, 0o700)
        if os.access(self.claude, os.R_OK):
            self.skipTest("root reads every folder")
        claude = self.by_name(self.gather(index=False), "claude-code-sessions")
        self.assertTrue(claude.store_exists)
        self.assertEqual(claude.store_error, f"store {self.claude} is not readable")

    def test_empty_qmd_listing_is_available_and_reports_missing_collections(self):
        self.fake_qmd('print("No collections.")')
        self.native("claude", "empty-0001")
        self.export()
        given = self.gather()
        self.assertEqual(given.qmd, "available")
        kinds = [g.kind for g in classify(given, datetime.now(UTC))]
        self.assertIn("collection-missing", kinds)
        self.assertNotIn("index-unknown", kinds)
        self.fake_qmd('print("something else")')
        self.assertEqual(self.gather().qmd, "failed")


class TestCli(GapsFixture):
    def test_gaps_text_lists_warn_before_info_and_ends_with_status(self):
        self.stub_qmd(claude_files=0)
        self.native("claude", "never-0001")  # native, never exported
        with mock.patch.object(gaps.shutil, "which", return_value=None):  # no rg
            code, out, _ = self.cli("gaps")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("warn  never-exported      claude-code-sessions: "))
        self.assertIn("; run cairn claude export --all", lines[0])
        severities = [line.split()[0] for line in lines[:-1]]
        self.assertEqual(severities, sorted(severities, key=lambda s: s != "warn"))
        self.assertIn("info  rg-missing          rg not on PATH: exact-match search is off", lines)
        self.assertRegex(lines[-1], r"^status: degraded \(\d+ warn, \d+ info\)$")

    def test_gaps_clean_is_one_line(self):
        self.stub_qmd(claude_files=0)
        qmd.record_update(time.time_ns(), ["claude-code-sessions", "codex-sessions"])
        with mock.patch.object(gaps.shutil, "which", return_value="/usr/bin/rg"):
            code, out, _ = self.cli("gaps")
        self.assertEqual(code, 0)
        self.assertEqual(out, "status: clean (checked 3 collections, qmd, rg)\n")

    def test_gaps_json_shape_and_schema_version(self):
        self.stub_qmd()
        self.native("claude", "json-0001")
        self.export()
        code, out, _ = self.cli("gaps", "--json", "--source", "claude")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(
            set(data),
            {"schema_version", "status", "checked_at", "gaps", "collections", "index"} | {"rg"},
        )
        self.assertEqual([c["collection"] for c in data["collections"]], ["claude-code-sessions"])
        row = data["collections"][0]
        self.assertEqual(row["exported_docs"], 1)
        self.assertEqual(row["native_basis"], "file")
        self.assertEqual(row["index"]["registered"], True)
        self.assertEqual(row["index"]["files"], 1)
        self.assertEqual(data["index"]["qmd"], "available")
        self.assertRegex(data["checked_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        for gap in data["gaps"]:
            self.assertEqual(
                set(gap),
                {"kind", "severity", "scope", "collection", "source", "message", "fix", "facts"},
            )
            self.assertIn(gap["kind"], gaps.KINDS)

    def test_qmd_missing_gives_index_unknown_and_exit_0(self):
        code, out, _ = self.cli("gaps", "--json")  # qmd_binary is /missing/qmd
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["index"]["qmd"], "missing")
        unknown = [g for g in data["gaps"] if g["kind"] == "index-unknown"]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(
            unknown[0]["message"],
            "qmd not found: topic search cannot run; temporal recall still works",
        )
        self.assertEqual(data["status"], "degraded")

    def test_qmd_timeout_gives_index_unknown(self):
        self.stub_qmd(sleep=5)
        with mock.patch.object(gaps, "QMD_PROBE_TIMEOUT_S", 0.5):
            code, out, _ = self.cli("gaps", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["index"]["qmd"], "failed")
        unknown = [g for g in data["gaps"] if g["kind"] == "index-unknown"]
        self.assertIn("timed out", unknown[0]["message"])
        self.assertFalse(
            any(g["kind"] in ("index-lag", "collection-missing") for g in data["gaps"])
        )

    def test_source_filter_rejects_unknown_source(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            recall.main(["gaps", "--source", "nonesuch"])
        self.assertEqual(raised.exception.code, 2)


class TestCoverage(GapsFixture):
    def setUp(self):
        super().setUp()
        self.stub_qmd()

    def test_a_disabled_source_not_named_does_not_degrade_coverage(self):
        data = json.loads(self.config.read_text())
        data["collections"]["cursor-sessions"] = {
            "store": str(self.root / "cursor"),
            "sync": {"enabled": False},
        }
        self.config.write_text(json.dumps(data))
        self.native("claude", "cov-0001")
        self.export()
        args = ("list", "2026-03-28", "--all-projects", "--json", "--coverage", "--min-msgs", "0")
        cov = json.loads(self.cli(*args)[1])["coverage"]
        self.assertEqual(cov["status"], "clean")
        self.assertEqual(
            [(g["kind"], g["severity"]) for g in cov["gaps"]], [("source-disabled", "info")]
        )
        cov = json.loads(self.cli(*args, "--source", "cursor")[1])["coverage"]
        self.assertEqual(cov["verdict"], "degraded-miss")

    def test_list_json_without_flag_is_still_an_array(self):
        self.native("claude", "cov-0001")
        self.export()
        code, out, _ = self.cli("list", "2026-03-28", "--all-projects", "--json", "--min-msgs", "0")
        self.assertEqual(code, 0)
        self.assertIsInstance(json.loads(out), list)

    def test_list_json_coverage_wraps_sessions(self):
        self.native("claude", "cov-0001")
        self.export()
        code, out, _ = self.cli(
            "list", "2026-03-28", "--all-projects", "--json", "--coverage", "--min-msgs", "0"
        )
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(set(data), {"sessions", "coverage"})
        self.assertEqual(len(data["sessions"]), 1)
        cov = data["coverage"]
        self.assertEqual(cov["verdict"], "found")
        self.assertEqual(cov["status"], "clean")
        self.assertEqual(cov["scope"], "store")
        self.assertEqual(
            cov["window"], {"start": "2026-03-28T00:00:00Z", "end": "2026-03-29T00:00:00Z"}
        )
        self.assertEqual(cov["sources"], ["claude", "codex", "antigravity", "cursor", "granola"])

    def test_coverage_never_calls_qmd(self):
        self.native("claude", "cov-0001")
        self.export()
        self.cli("list", "2026-03-28", "--all-projects", "--json", "--coverage", "--min-msgs", "0")
        self.cli("list", "2026-03-28", "--all-projects")
        self.cli("digest", "2026-03-28", "--all-projects", "--json", "--out", str(self.root / "o"))
        self.assertEqual(self.qmd_calls(), [])

    def test_digest_json_has_coverage_and_exit_codes_unchanged(self):
        self.native("claude", "never-0001")  # native activity, never exported
        out = str(self.root / "out")
        code, stdout, _ = self.cli("digest", "2026-03-28", "--all-projects", "--json", "--out", out)
        self.assertEqual(code, 3)
        data = json.loads(stdout)
        self.assertEqual(data["sessions"], [])
        self.assertEqual(data["skipped"], [])
        self.assertEqual(data["coverage"]["verdict"], "degraded-miss")
        self.assertEqual(data["coverage"]["sources"], ["claude", "codex", "antigravity", "cursor"])
        self.assertEqual([g["kind"] for g in data["coverage"]["gaps"]], ["never-exported"])

    def test_text_mode_prints_one_stderr_note_when_degraded(self):
        self.native("claude", "never-0001")
        code, _, err = self.cli("list", "2026-03-28", "--all-projects")
        self.assertEqual(code, 0)
        notes = [line for line in err.splitlines() if line.startswith("Note: coverage")]
        self.assertEqual(len(notes), 1)
        self.assertTrue(
            notes[0].startswith("Note: coverage degraded: claude-code-sessions: native files exist")
        )
        self.assertTrue(notes[0].endswith("run cairn recall gaps)"))
        self.export()
        code, _, err = self.cli("list", "2026-03-28", "--all-projects")
        self.assertNotIn("Note: coverage", err)
