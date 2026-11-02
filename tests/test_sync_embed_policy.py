"""``cairn sync --light|--nightly``, the embed loop, the power policy and ``cairn embed``.

QMD is a ``#!python`` stub that reads ``scenario.json``: a ``status`` scenario may hold
``outs``, one output per call, so a loop sees the pending count fall (or not). The
power probe and the local clock are patched; the sources are disabled so no export
subprocess runs.
"""

from __future__ import annotations

import io
import json
import os
import sys
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from cairn import config, qmd, sync
from cairn.storage import lock_path, locked
from tests.test_shared_store import StoreFixture

COMPLETE = "Documents\n  Total:    4 files indexed\n  Vectors:  9 embedded\n"


def pending(count: int) -> str:
    if count == 0:
        return COMPLETE
    return COMPLETE + f"  Pending:  {count} need embedding (run 'qmd embed')\n"


STUB = """
import json, sys
from pathlib import Path
here = Path(__file__).resolve().parent
scenario = json.loads((here / "scenario.json").read_text())
command = " ".join(sys.argv[1:3]) if sys.argv[1] == "collection" else sys.argv[1]
with open(here / "calls.log", "a") as log:
    log.write(command + "\\n")
spec = dict(scenario.get(command, {}))
if "outs" in spec:
    counter = here / f"{command}-count"
    n = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(n + 1))
    outs = spec["outs"]
    spec["out"] = outs[min(n, len(outs) - 1)]
if command == "collection list":
    print("qmd://codex-sessions/\\nqmd://claude-code-sessions/\\nqmd://antigravity-sessions/")
sys.stdout.write(spec.get("out", ""))
sys.stderr.write(spec.get("err", ""))
sys.exit(spec.get("rc", 0))
"""

EMBED_OK = "✓ Done! Embedded 2 chunks from 2 documents\n"


class EmbedFixture(StoreFixture):
    def setUp(self):
        super().setUp()
        data = json.loads(self.config.read_text())
        for spec in data["collections"].values():
            spec["sync"] = {"enabled": False}
        self.config.write_text(json.dumps(data))
        config.get_config.cache_clear()
        self.fake_qmd(STUB)
        self.power = ["ac"]
        self.clock = datetime(2026, 11, 2, 3, 0, 5)

    def configure(self, **cron) -> None:
        data = json.loads(self.config.read_text())
        data["cron"] = cron
        self.config.write_text(json.dumps(data))
        config.get_config.cache_clear()

    def scenario(self, statuses: list[str] | None = None, embed: dict | None = None) -> None:
        spec = {
            "embed": embed if embed is not None else {"out": EMBED_OK},
            "status": {"outs": statuses or [COMPLETE]},
        }
        (self.root / "scenario.json").write_text(json.dumps(spec))
        for stale in ("calls.log", "status-count"):
            (self.root / stale).unlink(missing_ok=True)

    def calls(self) -> list[str]:
        path = self.root / "calls.log"
        return path.read_text().splitlines() if path.exists() else []

    def embeds(self) -> int:
        return self.calls().count("embed")

    @property
    def log_text(self) -> str:
        return (self.root / "data" / "logs" / "context-sync.log").read_text()

    def run_sync(self, *flags: str) -> int:
        out, err = io.StringIO(), io.StringIO()
        with (
            patch("cairn.power.power_source", side_effect=self._power),
            patch("cairn.sync.local_now", side_effect=lambda: self.clock),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            return sync.main(list(flags))

    def run_embed(self, *flags: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            patch("cairn.power.power_source", side_effect=self._power),
            patch("cairn.sync.local_now", side_effect=lambda: self.clock),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = sync.embed_main(list(flags))
        return code, err.getvalue()

    def _power(self) -> str:
        return self.power.pop(0) if len(self.power) > 1 else self.power[0]


class TestModes(EmbedFixture):
    def test_light_mode_never_runs_embed(self):
        self.scenario([pending(5)])
        self.assertEqual(self.run_sync("--light"), 0, self.log_text)
        self.assertIn("update", self.calls())
        self.assertEqual(self.embeds(), 0)
        self.assertIn("=== context-sync (light)", self.log_text)
        self.assertTrue(self.log_text.rstrip().endswith("=== done ==="))
        self.assertIsNone(qmd.read_embed_stamp())

    def test_nightly_on_ac_embeds_and_writes_stamp(self):
        self.scenario([COMPLETE])
        self.assertEqual(self.run_sync("--nightly"), 0, self.log_text)
        self.assertEqual(self.embeds(), 1)
        self.assertIn("[qmd-embed] session 1/3", self.log_text)
        self.assertIn("[qmd-verify] ok - 4 documents, 9 vectors, 0 pending", self.log_text)
        stamp = qmd.read_embed_stamp()
        assert stamp is not None
        self.assertEqual(
            (stamp["outcome"], stamp["mode"], stamp["sessions"], stamp["pending"]),
            ("complete", "nightly", 1, 0),
        )
        self.assertEqual(stamp["last_embed_at"], stamp["last_complete_at"])

    def test_cron_mode_embeds_once_and_writes_stamp(self):
        self.scenario([pending(3)])
        self.assertEqual(self.run_sync("--cron"), 1)
        self.assertEqual(self.embeds(), 1)
        self.assertIn("failed steps: qmd-verify (incomplete)", self.log_text)
        self.assertIn("the next nightly embed or 'cairn embed' continues", self.log_text)
        stamp = qmd.read_embed_stamp()
        assert stamp is not None
        self.assertEqual(
            (stamp["outcome"], stamp["mode"], stamp["pending"]), ("incomplete", "cron", 3)
        )

    def test_mode_flags_are_mutually_exclusive(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            sync.main(["--light", "--cron"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("not allowed with argument", err.getvalue())


class TestPowerPolicy(EmbedFixture):
    def test_nightly_on_battery_defer_writes_marker_and_exits_0(self):
        self.scenario()
        self.power = ["battery"]
        self.assertEqual(self.run_sync("--nightly"), 0, self.log_text)
        self.assertEqual(self.embeds(), 0)
        self.assertIn(
            "[qmd-embed] deferred - on battery power; the next light sync on AC power runs it",
            self.log_text,
        )
        self.assertTrue(self.log_text.rstrip().endswith("=== done ==="))
        owed = qmd.read_owed()
        assert owed is not None
        self.assertEqual(owed["reason"], "battery")
        self.assertIsNone(owed["catchup_on"])
        stamp = qmd.read_embed_stamp()
        assert stamp is not None
        self.assertEqual(stamp["outcome"], "deferred")
        self.assertIsNone(stamp["last_embed_at"])

    def test_nightly_on_battery_skip_writes_no_marker(self):
        self.configure(embed_on_battery="skip")
        self.scenario()
        self.power = ["battery"]
        self.assertEqual(self.run_sync("--nightly"), 0)
        self.assertEqual(self.embeds(), 0)
        self.assertIn(
            "[qmd-embed] skipped - on battery power; the next nightly run tries again",
            self.log_text,
        )
        self.assertIsNone(qmd.read_owed())

    def test_nightly_on_battery_run_embeds(self):
        self.configure(embed_on_battery="run")
        self.scenario()
        self.power = ["battery"]
        self.assertEqual(self.run_sync("--nightly"), 0)
        self.assertEqual(self.embeds(), 1)

    def test_unknown_power_counts_as_ac(self):
        self.scenario()
        self.power = ["unknown"]
        self.assertEqual(self.run_sync("--nightly"), 0)
        self.assertEqual(self.embeds(), 1)

    def test_battery_mid_loop_stops_and_marks_owed(self):
        self.scenario([pending(10), pending(4)])
        self.power = ["ac", "battery"]
        self.assertEqual(self.run_sync("--nightly"), 1)
        self.assertEqual(self.embeds(), 1)
        self.assertIn("[qmd-embed] stopped - on battery power after 1 sessions", self.log_text)
        self.assertIsNotNone(qmd.read_owed())
        stamp = qmd.read_embed_stamp()
        assert stamp is not None
        self.assertEqual((stamp["outcome"], stamp["sessions"]), ("incomplete", 1))


class TestLoop(EmbedFixture):
    def test_loop_continues_while_pending_falls(self):
        self.scenario([pending(10), pending(4), COMPLETE])
        self.assertEqual(self.run_sync("--nightly"), 0, self.log_text)
        self.assertEqual(self.embeds(), 3)
        for n in (1, 2, 3):
            self.assertIn(f"[qmd-embed] session {n}/3", self.log_text)
        self.assertNotIn("failed steps", self.log_text)
        stamp = qmd.read_embed_stamp()
        assert stamp is not None
        self.assertEqual((stamp["outcome"], stamp["sessions"]), ("complete", 3))

    def test_loop_stops_when_pending_stalls(self):
        self.scenario([pending(10), pending(10), COMPLETE])
        self.assertEqual(self.run_sync("--nightly"), 1)
        self.assertEqual(self.embeds(), 2)
        self.assertIn(
            "[qmd-verify] STALLED - pending stayed at 10 after session 2; a document may never "
            "fit in one 30-minute session. Investigate instead of retrying.",
            self.log_text,
        )
        self.assertIn("failed steps: qmd-verify (stalled)", self.log_text)
        stamp = qmd.read_embed_stamp()
        assert stamp is not None
        self.assertEqual(stamp["outcome"], "stalled")

    def test_loop_stops_at_max_sessions(self):
        self.scenario([pending(10), pending(8), pending(6), pending(4)])
        self.assertEqual(self.run_sync("--nightly"), 1)
        self.assertEqual(self.embeds(), 3)
        self.assertIn("failed steps: qmd-verify (incomplete)", self.log_text)
        stamp = qmd.read_embed_stamp()
        assert stamp is not None
        self.assertEqual(
            (stamp["outcome"], stamp["sessions"], stamp["pending"]), ("incomplete", 3, 6)
        )

    def test_deadline_allows_first_session_only(self):
        self.scenario([pending(10), pending(4), COMPLETE])
        self.clock = datetime(2026, 11, 2, 8, 30)
        self.assertEqual(self.run_sync("--nightly"), 1)
        self.assertEqual(self.embeds(), 1)
        self.assertIn(
            "[qmd-embed] stopped - embed_deadline 07:00 reached after 1 sessions", self.log_text
        )

    def test_power_and_deadline_are_checked_under_the_index_lock(self):
        events: list[str] = []
        real = sync.locked_index

        @contextmanager
        def recording(**kwargs):
            events.append("acquire")
            with real(**kwargs):
                yield
            events.append("release")

        def power_probe() -> str:
            events.append("power")
            return "ac"

        self.scenario([COMPLETE])
        with (
            patch("cairn.sync.locked_index", recording),
            patch("cairn.power.power_source", side_effect=power_probe),
        ):
            out = io.StringIO()
            with redirect_stdout(out), patch("cairn.sync.local_now", return_value=self.clock):
                self.assertEqual(sync.main(["--nightly"]), 0, self.log_text)
        # update: acquire/release; session 1: acquire, power probe, release.
        self.assertEqual(events, ["acquire", "release", "acquire", "power", "release"])

    def test_index_lock_released_between_sessions(self):
        events: list[str] = []
        real = sync.locked_index

        @contextmanager
        def recording(**kwargs):
            events.append("acquire")
            with real(**kwargs):
                yield
            events.append("release")

        self.scenario([pending(10), COMPLETE])
        with patch("cairn.sync.locked_index", recording):
            self.assertEqual(self.run_sync("--nightly"), 0, self.log_text)
        self.assertEqual(self.embeds(), 2)
        # update, session 1, session 2: three separate holds, so a waiting hook worker
        # gets the lock between sessions.
        self.assertEqual(events, ["acquire", "release"] * 3)

    def test_embed_that_never_exits_is_failed_and_keeps_last_embed_at(self):
        self.scenario()
        self.assertEqual(self.run_sync("--nightly"), 0)
        before = qmd.read_embed_stamp()
        assert before is not None
        with patch("cairn.sync.run_embed", return_value=(False, False)):
            self.assertEqual(self.run_sync("--nightly"), 1)
        self.assertNotIn("STALLED", self.log_text)
        self.assertIn("failed steps: qmd-embed\n", self.log_text)
        stamp = qmd.read_embed_stamp()
        assert stamp is not None
        self.assertEqual(stamp["outcome"], "failed")
        self.assertEqual(stamp["last_embed_at"], before["last_embed_at"])

    def test_embed_exit_code_fails_the_step_but_verify_decides_the_outcome(self):
        self.scenario(embed={"out": "", "rc": 2})
        self.assertEqual(self.run_sync("--nightly"), 1)
        self.assertIn("[qmd-embed] FAILED (0.0s) - exit 2", self.log_text)
        self.assertIn("failed steps: qmd-embed\n", self.log_text)
        stamp = qmd.read_embed_stamp()
        assert stamp is not None
        self.assertEqual(stamp["outcome"], "complete")


class TestCatchUp(EmbedFixture):
    def test_light_catch_up_runs_once_per_day(self):
        self.scenario([pending(2), pending(2)])
        qmd.write_owed("battery")
        self.assertEqual(self.run_sync("--light"), 1)
        self.assertEqual(self.embeds(), 1)
        self.assertIn("[embed-catchup] running the embed deferred at", self.log_text)
        owed = qmd.read_owed()
        assert owed is not None
        self.assertEqual(owed["catchup_on"], "2026-11-02")
        self.assertEqual(self.run_sync("--light"), 0)
        self.assertEqual(self.embeds(), 1)
        self.clock = datetime(2026, 11, 3, 9, 0)
        self.scenario([COMPLETE])
        self.assertEqual(self.run_sync("--light"), 0)
        self.assertEqual(self.embeds(), 1)
        self.assertIsNone(qmd.read_owed())

    def test_catch_up_skipped_after_failed_update(self):
        self.scenario()
        spec = json.loads((self.root / "scenario.json").read_text())
        spec["update"] = {"rc": 1}
        (self.root / "scenario.json").write_text(json.dumps(spec))
        qmd.write_owed("battery")
        self.assertEqual(self.run_sync("--light"), 1)
        self.assertEqual(self.embeds(), 0)
        self.assertIn("[embed-catchup] skipped - qmd-update failed", self.log_text)
        owed = qmd.read_owed()
        assert owed is not None
        self.assertIsNone(owed["catchup_on"])

    def test_completing_another_database_keeps_this_owed_marker(self):
        qmd.write_owed("battery")
        with patch.dict(os.environ, {"INDEX_PATH": str(self.root / "other.sqlite")}):
            qmd.clear_owed()
        self.assertIsNotNone(qmd.read_owed())
        qmd.clear_owed()
        self.assertIsNone(qmd.read_owed())

    def test_catch_up_waits_for_mains_power(self):
        self.scenario()
        qmd.write_owed("battery")
        self.power = ["battery"]
        self.assertEqual(self.run_sync("--light"), 0)
        self.assertEqual(self.embeds(), 0)
        self.assertIsNotNone(qmd.read_owed())

    def test_catch_up_ignores_marker_for_other_database(self):
        self.scenario()
        with patch.dict(os.environ, {"INDEX_PATH": str(self.root / "other.sqlite")}):
            qmd.write_owed("battery")
        self.assertIsNone(qmd.read_owed())
        self.assertEqual(self.run_sync("--light"), 0)
        self.assertEqual(self.embeds(), 0)

    def test_catch_up_off_by_config(self):
        self.configure(embed_catchup=False)
        self.scenario()
        qmd.write_owed("battery")
        self.assertEqual(self.run_sync("--light"), 0)
        self.assertEqual(self.embeds(), 0)


class TestFollowUp(EmbedFixture):
    def test_deferred_nightly_still_runs_a_coalesced_cron_request(self):
        # A manual --cron arrived while the nightly run exported; the night deferred on
        # battery, so the request's embed (no power policy) is still owed.
        self.scenario()
        self.power = ["battery"]
        sync.request_sync(config.get_config(), "cron")
        self.assertEqual(self.run_sync("--nightly"), 0, self.log_text)
        self.assertIn("[qmd-embed] deferred - on battery power", self.log_text)
        self.assertIn("[coalesced] a skipped 'cron' sync arrived; running its embed", self.log_text)
        self.assertEqual(self.embeds(), 1)
        stamp = qmd.read_embed_stamp()
        assert stamp is not None
        self.assertEqual((stamp["outcome"], stamp["mode"]), ("complete", "cron"))

    def test_embedded_nightly_only_retries_hooks_for_a_cron_request(self):
        self.scenario()
        sync.request_sync(config.get_config(), "cron")
        self.assertEqual(self.run_sync("--nightly"), 0, self.log_text)
        self.assertIn("retrying hook requests", self.log_text)
        self.assertEqual(self.embeds(), 1)

    def test_follow_up_light_run_services_nightly_request(self):
        self.scenario()
        sync.request_sync(config.get_config(), "nightly")
        self.assertEqual(self.run_sync("--light"), 0, self.log_text)
        self.assertIn(
            "[coalesced] a skipped 'nightly' sync arrived; running its embed", self.log_text
        )
        self.assertEqual(self.embeds(), 1)
        stamp = qmd.read_embed_stamp()
        assert stamp is not None
        self.assertEqual(stamp["mode"], "nightly")


class TestEmbedCommand(EmbedFixture):
    def test_embed_runs_update_then_sessions(self):
        self.scenario([pending(3), COMPLETE])
        code, _err = self.run_embed("--sessions", "2")
        self.assertEqual(code, 0, self.log_text)
        self.assertEqual(self.calls().count("update"), 1)
        self.assertEqual(self.embeds(), 2)
        self.assertIn("=== context-sync (embed)", self.log_text)
        stamp = qmd.read_embed_stamp()
        assert stamp is not None
        self.assertEqual(stamp["mode"], "embed")

    def test_embed_command_busy_exits_3(self):
        self.scenario()
        cfg = config.get_config()
        with locked(lock_path(cfg.data_root, "sync")):
            code, err = self.run_embed()
        self.assertEqual(code, 3)
        self.assertIn("another cairn sync is running for", err)
        self.assertIn("run 'cairn embed' again after it finishes", err)
        self.assertEqual(self.embeds(), 0)
        self.assertFalse((cfg.data_root / "sync-requests").exists())

    def test_embed_until_complete_caps_at_12(self):
        self.scenario([pending(30 - n) for n in range(14)])
        code, _err = self.run_embed("--until-complete")
        self.assertEqual(code, 1)
        self.assertEqual(self.embeds(), 12)
        self.assertIn("[qmd-embed] session 12/12", self.log_text)

    def test_embed_respects_power_only_on_request(self):
        self.scenario()
        self.power = ["battery"]
        code, _err = self.run_embed()
        self.assertEqual(code, 0)
        self.assertEqual(self.embeds(), 1)
        self.scenario()
        code, _err = self.run_embed("--respect-power")
        self.assertEqual(code, 0)
        self.assertEqual(self.embeds(), 0)
        self.assertIsNotNone(qmd.read_owed())

    def test_embed_usage_errors_exit_2(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            sync.embed_main(["--sessions", "13"])
        self.assertEqual(caught.exception.code, 2)
        with redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            sync.embed_main(["--sessions", "2", "--until-complete"])
        self.assertEqual(caught.exception.code, 2)


class TestEmbedStamp(StoreFixture):
    def status(self, pending_count: int) -> qmd.IndexStatus:
        return qmd.IndexStatus(4, 9, pending_count)

    def test_round_trip_and_copy_forward(self):
        clock = iter(f"2026-11-0{d}T03:00:00Z" for d in range(1, 9))
        with patch("cairn.qmd._now_iso", side_effect=lambda: next(clock)):
            first = qmd.record_embed("complete", "nightly", 1, self.status(0))
            stamp = qmd.read_embed_stamp()
            self.assertEqual(stamp, first)
            assert stamp is not None
            self.assertEqual(stamp["last_complete_at"], "2026-11-01T03:00:00Z")
            second = qmd.record_embed("incomplete", "nightly", 3, self.status(5))
            self.assertEqual(second["finished_at"], "2026-11-02T03:00:00Z")
            self.assertEqual(second["last_complete_at"], "2026-11-01T03:00:00Z")
            self.assertEqual(second["last_embed_at"], "2026-11-02T03:00:00Z")
            self.assertEqual(second["pending"], 5)
            third = qmd.record_embed("deferred", "nightly", 0, None)
            self.assertEqual(third["finished_at"], "2026-11-03T03:00:00Z")
            self.assertEqual(third["last_embed_at"], "2026-11-02T03:00:00Z")
            self.assertEqual(third["pending"], 5)
            self.assertEqual(third["outcome"], "deferred")
            # A session that never exited (timeout, could not start) refreshed nothing.
            fourth = qmd.record_embed("failed", "nightly", 1, None, exited=False)
            self.assertEqual(fourth["finished_at"], "2026-11-04T03:00:00Z")
            self.assertEqual(fourth["last_embed_at"], "2026-11-02T03:00:00Z")
            fifth = qmd.record_embed("failed", "nightly", 1, self.status(2))
            self.assertEqual(fifth["last_embed_at"], "2026-11-05T03:00:00Z")

    def test_malformed_stamps_are_absent(self):
        good = qmd.record_embed("complete", "cron", 1, self.status(0))
        path = qmd.embed_stamp_path()
        for bad in (
            "not json",
            "[]",
            {**good, "version": 2},
            {**good, "outcome": "done"},
            {**good, "finished_at": "yesterday"},
            {**good, "sessions": "1"},
            {**good, "database": "/elsewhere/index.sqlite"},
        ):
            path.write_text(bad if isinstance(bad, str) else json.dumps(bad))
            self.assertIsNone(qmd.read_embed_stamp(), bad)
        path.write_text(json.dumps(good))
        self.assertEqual(qmd.read_embed_stamp(), good)

    def test_write_failure_warns_and_keeps_the_old_stamp(self):
        qmd.record_embed("complete", "cron", 1, self.status(0))
        err = io.StringIO()
        with (
            patch.object(qmd, "atomic_write", side_effect=OSError("disk full")),
            redirect_stderr(err),
        ):
            qmd.record_embed("failed", "cron", 1, None)
        self.assertEqual(
            err.getvalue(), "Warning: could not record the qmd embed outcome: disk full\n"
        )
        stamp = qmd.read_embed_stamp()
        self.assertEqual(stamp["outcome"] if stamp else None, "complete")

    def test_owed_marker_keeps_its_first_since(self):
        qmd.write_owed("battery")
        first = qmd.read_owed()
        assert first is not None
        qmd.mark_catchup("2026-11-02")
        qmd.write_owed("battery")
        again = qmd.read_owed()
        assert again is not None
        self.assertEqual(again["since"], first["since"])
        self.assertEqual(again["catchup_on"], "2026-11-02")
        qmd.clear_owed()
        self.assertIsNone(qmd.read_owed())
        self.assertFalse(Path(qmd.owed_path()).exists())


if __name__ == "__main__":
    sys.exit(0)
