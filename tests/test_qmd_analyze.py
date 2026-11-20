"""Planner statistics for the QMD index: ``qmd.analyze_if_stale`` and the sync stage.

The index here is a small real SQLite file with a table and an index of its own, so
``ANALYZE`` has something to measure. No QMD binary, table or model is involved.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest import mock

from cairn import qmd, sync
from tests.test_sync import SyncFixture


def make_index(path: Path, rows: int = 50) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    with connection:
        connection.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, tag TEXT)")
        connection.execute("CREATE INDEX IF NOT EXISTS notes_tag ON notes(tag)")
        connection.executemany(
            "INSERT INTO notes (tag) VALUES (?)", [(f"tag-{n % 7}",) for n in range(rows)]
        )
    connection.close()


def has_statistics(path: Path) -> bool:
    connection = sqlite3.connect(path)
    try:
        found = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'sqlite_stat1'"
        ).fetchone()
        if found is None:
            return False
        return connection.execute("SELECT count(*) FROM sqlite_stat1").fetchone()[0] > 0
    finally:
        connection.close()


class AnalyzeFixture(SyncFixture):
    def setUp(self):
        super().setUp()
        self.index = self.root / "cache/qmd/index.sqlite"
        self.stamp_file = self.root / "state/cairn/qmd-analyze.json"

    def stamp(self) -> dict | None:
        return json.loads(self.stamp_file.read_text()) if self.stamp_file.exists() else None

    def set_index(self, **values: object) -> None:
        data = json.loads(self.config.read_text())
        data["index"] = values
        self.config.write_text(json.dumps(data))


class TestAnalyzeIfStale(AnalyzeFixture):
    def test_no_index_file_creates_nothing(self):
        self.assertEqual(qmd.analyze_if_stale(), "no-index")
        self.assertFalse(self.index.exists())
        self.assertIsNone(self.stamp())

    def test_first_call_writes_statistics_and_the_stamp(self):
        make_index(self.index)
        self.assertFalse(has_statistics(self.index))
        self.assertEqual(qmd.analyze_if_stale(), "analyzed")
        self.assertTrue(has_statistics(self.index))
        stamp = self.stamp()
        assert stamp is not None
        self.assertEqual(stamp["database"], str(self.index.resolve()))
        self.assertGreater(stamp["size"], 0)

    def test_same_size_is_current_and_growth_past_a_fifth_analyses_again(self):
        make_index(self.index)
        self.assertEqual(qmd.analyze_if_stale(), "analyzed")
        self.assertEqual(qmd.analyze_if_stale(), "current")
        before = self.index.stat().st_size
        make_index(self.index, rows=20000)
        self.assertGreater(self.index.stat().st_size, before * (1 + qmd.ANALYZE_SIZE_DRIFT))
        self.assertEqual(qmd.analyze_if_stale(), "analyzed")
        stamp = self.stamp()
        assert stamp is not None
        self.assertEqual(stamp["size"], self.index.stat().st_size)

    def test_stamp_of_another_database_does_not_count(self):
        make_index(self.index)
        self.stamp_file.parent.mkdir(parents=True, exist_ok=True)
        self.stamp_file.write_text(
            json.dumps(
                {
                    "version": qmd.STAMP_VERSION,
                    "database": "/tmp/other/index.sqlite",
                    "size": self.index.stat().st_size,
                }
            )
        )
        self.assertEqual(qmd.analyze_if_stale(), "analyzed")

    def test_a_file_that_is_not_a_database_raises(self):
        self.index.parent.mkdir(parents=True, exist_ok=True)
        self.index.write_text("not a database, but long enough to be read as a header\n" * 4)
        with self.assertRaises(sqlite3.Error):
            qmd.analyze_if_stale()
        self.assertIsNone(self.stamp())


class TestSyncAnalyzeStage(AnalyzeFixture):
    def test_sync_analyses_before_qmd_update(self):
        make_index(self.index)
        self.assertEqual(self.sync_cron().returncode, 0, self.log_text)
        self.assertTrue(has_statistics(self.index))
        log = self.log_text
        self.assertIn("[qmd-analyze] ok", log)
        self.assertLess(log.index("[qmd-analyze] ok"), log.index("[qmd-update] ok"))

    def test_second_sync_does_not_analyse_again(self):
        make_index(self.index)
        self.assertEqual(self.sync_cron().returncode, 0, self.log_text)
        self.assertEqual(self.sync_cron().returncode, 0, self.log_text)
        self.assertEqual(self.log_text.count("[qmd-analyze]"), 1)

    def test_analyze_false_leaves_the_index_alone(self):
        make_index(self.index)
        self.set_index(analyze=False)
        self.assertEqual(self.sync_cron().returncode, 0, self.log_text)
        self.assertFalse(has_statistics(self.index))
        self.assertNotIn("[qmd-analyze]", self.log_text)

    def test_unreadable_index_is_logged_and_the_update_still_runs(self):
        self.index.parent.mkdir(parents=True, exist_ok=True)
        self.index.write_text("not a database, but long enough to be read as a header\n" * 4)
        self.assertEqual(self.sync_cron().returncode, 0, self.log_text)
        self.assertIn("[qmd-analyze] skipped - ", self.log_text)
        self.assertIn("[qmd-update] ok", self.log_text)


class TestIndexTimeouts(AnalyzeFixture):
    def test_update_timeout_comes_from_the_config(self):
        self.set_index(update_timeout_s=7)
        seen: list[tuple[str, int]] = []

        def step(name, command, timeout=120):
            seen.append((name, timeout))
            return True

        cfg = sync.get_config()
        with mock.patch.object(sync, "run_step", step):
            sync._index_stages(cfg, lambda name, ok: None)
        self.assertEqual(seen, [("qmd-update", 7)])

    def test_failed_update_names_the_key_and_the_error_code(self):
        self.set_index(update_timeout_s=9)
        self.scenario({"update": {"rc": 1, "err": "boom\n"}})
        self.assertEqual(self.sync_cron().returncode, 1)
        self.assertIn("index.update_timeout_s is 9", self.log_text)
        self.assertIn("cairn errors QMD_UPDATE_SLOW", self.log_text)

    def test_collection_add_timeout_comes_from_the_config(self):
        self.set_index(collection_timeout_s=11)
        self.native("codex", "registered")
        self.run_cli("codex", "export", "--since", "30d", "-q", check=False)
        calls: list[tuple[list[str], float]] = []

        class Done:
            returncode = 0
            stdout = ""
            stderr = ""

        def capped(command, timeout):
            calls.append((command, timeout))
            return Done()

        cfg = sync.get_config()
        with mock.patch.object(qmd, "run_capped", capped):
            sync._ensure_collections(cfg)
        adds = [timeout for command, timeout in calls if command[1:3] == ["collection", "add"]]
        self.assertEqual(adds, [11])

    def test_collection_add_timeout_message_names_the_key(self):
        target = self.root / "notes"
        target.mkdir()
        (target / "a.md").write_text("# a\n")
        expired = qmd.subprocess.TimeoutExpired("qmd", 3)

        def capped(command, timeout):
            raise expired

        with (
            mock.patch.object(qmd, "run_capped", capped),
            self.assertRaisesRegex(
                RuntimeError, r"timed out after 3s.*index\.collection_timeout_s"
            ),
        ):
            qmd.ensure_collection("notes", target, "qmd", set(), timeout=3)
