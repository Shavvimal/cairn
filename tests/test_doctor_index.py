"""``cairn doctor`` QMD runtime and index checks, default and ``--strict-index``.

A ``#!python`` stub stands in for qmd and records every subcommand, which proves
doctor only probes (``--version``, ``status``, ``collection list``) and never
embeds, installs or upgrades.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from cairn import __version__, admin, config
from tests.test_shared_store import StoreFixture

COMPLETE = "Documents\n  Total:    4 files indexed\n  Vectors:  9 embedded\n"
PENDING = COMPLETE + "  Pending:  2 need embedding (run 'qmd embed')\n"
EMPTY = "Documents\n  Total:    0 files indexed\n  Vectors:  0 embedded\n"
NO_VECTORS = "Documents\n  Total:    4 files indexed\n  Vectors:  0 embedded\n"

STUB = """
import json, os, sys
from pathlib import Path
here = Path(__file__).resolve().parent
spec = json.loads((here / "doctor.json").read_text())
with open(here / "doctor-calls.log", "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1] == "--version":
    print(spec["version"])
elif sys.argv[1] == "status":
    if spec.get("crash"):
        os.kill(os.getpid(), 11)
    print(spec["status"])
"""


class TestDoctorIndex(StoreFixture):
    def stub(self, status: str = COMPLETE, version: str = "qmd 2.5.3 (179d450)", **extra):
        self.fake_qmd(STUB)
        (self.root / "doctor.json").write_text(
            json.dumps({"status": status, "version": version, **extra})
        )

    crontab: tuple[str, ...] = ()

    def doctor(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with (
            mock.patch.object(admin, "_current_crontab", return_value=list(self.crontab)),
            redirect_stdout(out),
        ):
            code = admin.doctor_main(list(args))
        return code, out.getvalue()

    def embed_stamp(self, hours_ago: float, outcome: str = "complete") -> None:
        from datetime import UTC, datetime, timedelta

        from cairn import qmd

        payload = qmd.record_embed(outcome, "nightly", 1, qmd.IndexStatus(4, 9, 0))
        when = (datetime.now(UTC) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload["last_embed_at"] = when
        payload["finished_at"] = when
        qmd.embed_stamp_path().write_text(json.dumps(payload))

    def calls(self) -> set[str]:
        path = self.root / "doctor-calls.log"
        return (
            {line.split()[0] for line in path.read_text().splitlines()} if path.exists() else set()
        )

    def test_missing_executable_is_critical(self):
        code, out = self.doctor()  # StoreFixture's qmd_binary is /missing/qmd
        self.assertEqual(code, 1)
        self.assertIn("FAIL qmd - '/missing/qmd' not found", out)
        self.assertIn("(e.g. npm install -g @tobilu/qmd) [QMD_MISSING]\n", out)

    def test_version_succeeds_but_runtime_crashes(self):
        self.stub(crash=True)
        for args in ((), ("--strict-index",)):
            code, out = self.doctor(*args)
            self.assertEqual(code, 1)
            self.assertIn("ok   qmd version - qmd 2.5.3", out)
            self.assertIn("FAIL qmd runtime", out)
            self.assertIn("killed by signal 11", out)
            self.assertIn("so search cannot work [QMD_RUNTIME_FAILED]\n", out)

    def test_old_version_warns_but_is_judged_by_the_index(self):
        self.stub(version="qmd 1.1.5")
        code, out = self.doctor("--strict-index")
        self.assertEqual(code, 0, out)
        self.assertIn("warn qmd version - qmd 1.1.5: older than 2.5.3", out)

    def test_unknown_version_warns(self):
        self.stub(version="qmd 2.6.0-beta.1")
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("is not a release version", out)

    def test_unrecognised_status(self):
        self.stub(status="Some new format")
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("warn qmd runtime - ran, but", out)
        self.assertIn("the semantic index is not verified: could not read 'qmd status'", out)
        self.assertEqual(self.doctor("--strict-index")[0], 1)

    def test_empty_index_passes_both_modes_without_claiming_search_works(self):
        self.stub(status=EMPTY)
        for args in ((), ("--strict-index",)):
            code, out = self.doctor(*args)
            self.assertEqual(code, 0, out)
            self.assertIn("the index is empty, so no search can be verified yet", out)
            self.assertIn("All critical checks passed, but the semantic index is not verified", out)

    def test_no_vectors_warns_by_default_and_fails_strict(self):
        self.stub(status=NO_VECTORS)
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("warn embeddings - 4 documents, 0 vectors, 0 pending: keyword search", out)
        self.assertIn("run 'cairn embed' [QMD_INDEX_INCOMPLETE]\n", out)
        code, out = self.doctor("--strict-index")
        self.assertEqual(code, 1)
        self.assertIn("FAIL embeddings", out)
        self.assertIn("run 'cairn embed' [QMD_INDEX_INCOMPLETE]\n", out)

    def test_pending_warns_by_default_and_fails_strict(self):
        self.stub(status=PENDING)
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("warn embeddings - 4 documents, 9 vectors, 2 pending", out)
        self.assertIn("not verified", out)
        self.assertEqual(self.doctor("--strict-index")[0], 1)

    def test_pending_is_ok_after_recent_nightly_embed(self):
        self.stub(status=PENDING)
        self.embed_stamp(hours_ago=2)
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn(
            "ok   embeddings - 4 documents, 9 vectors, 2 pending; the nightly embed at 03:00 "
            "covers them",
            out,
        )
        self.assertNotIn("QMD_INDEX_INCOMPLETE", out)
        self.assertIn("All critical checks passed.", out)
        code, out = self.doctor("--strict-index")
        self.assertEqual(code, 1)
        self.assertIn("FAIL embeddings", out)

    def test_pending_warns_without_recent_embed(self):
        self.stub(status=PENDING)
        self.embed_stamp(hours_ago=40)
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("warn embeddings - 4 documents, 9 vectors, 2 pending", out)
        self.assertIn("run 'cairn embed'", out)
        self.assertIn("[QMD_INDEX_INCOMPLETE]", out)

    def test_pending_warns_when_embed_is_not_nightly(self):
        self.stub(status=PENDING)
        self.embed_stamp(hours_ago=2)
        data = json.loads(self.config.read_text())
        data["cron"] = {"embed": "hourly"}
        self.config.write_text(json.dumps(data))
        config.get_config.cache_clear()
        _code, out = self.doctor()
        self.assertIn("warn embeddings", out)

    def test_embed_overdue_warns(self):
        self.stub()
        _code, out = self.doctor()
        self.assertIn("no embed finished since the install; check 'cairn cron status'", out)
        self.assertIn("[EMBED_OVERDUE]", out)
        self.embed_stamp(hours_ago=50)
        _code, out = self.doctor()
        self.assertIn("[EMBED_OVERDUE]", out)
        self.embed_stamp(hours_ago=2)
        _code, out = self.doctor()
        self.assertNotIn("EMBED_OVERDUE", out)

    def test_schedule_lines(self):
        self.stub()
        _code, out = self.doctor()
        self.assertIn("warn sync schedule - no light job; run 'cairn cron install'", out)
        self.assertIn("warn sync schedule - no nightly job; run 'cairn cron install'", out)
        self.assertNotIn("ok   sync schedule", out)
        from cairn import schedule

        with mock.patch("cairn.routine.is_macos", return_value=False):
            self.crontab = tuple(
                schedule.crontab_line(
                    config.get_config(),
                    job,
                    {"PATH": "/usr/bin"},
                    ["/opt/cairn"],
                    ["/usr/bin/nice", "-n", "10"],
                )
                for job in schedule.jobs(config.get_config())
            )
            _code, out = self.doctor()
        self.assertIn(
            "ok   sync schedule - cron: light hourly at :7, nightly 03:00 (background)", out
        )
        with mock.patch("cairn.routine.is_macos", return_value=False):
            self.crontab = tuple(line.replace("/usr/bin/nice -n 10 ", "") for line in self.crontab)
            _code, out = self.doctor()
        self.assertIn("the cron line runs at normal priority; run 'cairn cron install'", out)

    def test_legacy_cron_line_warns_sync_schedule_legacy(self):
        self.stub()
        self.crontab = (
            "7 * * * * /usr/bin/env X=1 /opt/cairn sync --cron >> /tmp/l 2>&1 # cairn sync",
        )
        _code, out = self.doctor()
        self.assertIn(
            "the old hourly 'cairn sync --cron' entry still embeds every hour; "
            "run 'cairn cron install' [SYNC_SCHEDULE_LEGACY]",
            out,
        )

    def test_two_schedulers_warn(self):
        import plistlib

        self.stub()
        self.crontab = ("7 * * * * /opt/cairn sync --light >> /tmp/l 2>&1 # cairn sync light",)
        agents = Path.home() / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        (agents / "dev.cairn.sync.light.plist").write_bytes(plistlib.dumps({"Label": "x"}))
        with mock.patch("cairn.routine.is_macos", return_value=True):
            _code, out = self.doctor()
        self.assertIn("two schedulers run cairn sync; run 'cairn cron install'", out)

    def test_owed_marker_is_reported(self):
        from cairn import qmd

        self.stub()
        qmd.write_owed("battery")
        _code, out = self.doctor()
        self.assertIn("ok   sync schedule - embed owed since", out)
        self.assertIn("(battery); the next light sync on AC power runs it", out)

    def test_malformed_pending_line_fails_strict(self):
        self.stub(status=COMPLETE + "  Pending:  unknown need embedding\n")
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("could not read 'qmd status'", out)
        self.assertEqual(self.doctor("--strict-index")[0], 1)

    def test_complete_index_passes_and_composes_with_other_options(self):
        self.stub()
        manifest = self.root / "plugin.json"
        manifest.write_text(json.dumps({"name": "cairn", "version": __version__}))
        code, out = self.doctor(
            "--strict-index", "--harness", "claude", "--plugin-manifest", str(manifest)
        )
        self.assertEqual(code, 0, out)
        self.assertIn("ok   qmd runtime", out)
        self.assertIn("ok   embeddings - 4 documents, 9 vectors, 0 pending", out)
        self.assertTrue(out.rstrip().endswith("All critical checks passed."), out)
        self.assertLessEqual(self.calls(), {"--version", "status", "collection"})


if __name__ == "__main__":
    import unittest

    unittest.main()
