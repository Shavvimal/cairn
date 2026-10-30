"""``cairn.routine_state``: the file-only status classifier and the status exit codes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from cairn import routine_state
from cairn.health import process_identity
from tests.test_routine import Children, Launchctl, RoutineFixture

NOW = datetime(2026, 11, 12, 6, 0, tzinfo=UTC)


class TestClassify(RoutineFixture):
    def classify(self, now: datetime = NOW, macos: bool = False) -> routine_state.Classification:
        return routine_state.classify(self.cfg, self.cfg.routines["daily"], now, macos=macos)

    def last_run(self, hours_ago: float, exit_code: int = 0) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        finished = (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
        (self.state / "last-run.json").write_text(
            json.dumps({"finished_at": finished, "exit": exit_code})
        )

    def test_state_precedence_disabled_paused_running_never_stale_fresh(self):
        self.assertEqual(self.classify().state, "never_run")
        self.last_run(72)
        self.assertEqual(self.classify().state, "stale")
        self.last_run(1)
        self.assertEqual(self.classify().state, "fresh")
        identity = process_identity(os.getpid())
        assert identity is not None
        (self.state / "running.json").write_text(
            json.dumps({"pid": identity[0], "start_token": identity[1]})
        )
        (self.state / "last-run.json").unlink()
        self.assertEqual(self.classify().state, "running")
        (self.state / "paused.json").write_text(json.dumps({"reason": "away"}))
        self.assertEqual(self.classify().state, "paused")
        (self.state / "disabled.json").write_text("{broken")
        verdict = self.classify()
        self.assertEqual(verdict.state, "disabled")
        self.assertEqual(verdict.disabled, {"reason": "(unreadable marker)"})
        self.assertEqual(verdict.paused, {"reason": "away"})

    def test_stale_after_two_cadences(self):
        self.last_run(47)
        verdict = self.classify()
        self.assertEqual((verdict.state, verdict.exit), ("fresh", 0))
        self.assertEqual(verdict.last_run_age_s, 47 * 3600)
        self.last_run(49)
        verdict = self.classify()
        self.assertEqual((verdict.state, verdict.exit), ("stale", 1))
        self.last_run(-5)  # clock skew: a future finish reads as age 0
        self.assertEqual((self.classify().state, self.classify().last_run_age_s), ("fresh", 0.0))

    def test_attention_codes_from_stamps(self):
        self.last_run(1, exit_code=1)
        (self.state / "failed-2026-11-09.json").write_text(json.dumps({"attempts": 1}))
        (self.state / "failed-2026-11-10.json").write_text(json.dumps({"attempts": 3}))
        (self.state / "failed-2026-11-11.json").write_text("{")
        verdict = self.classify()
        self.assertEqual(
            verdict.attention,
            ["failed-day", "attempts-exhausted", "corrupt-stamp", "last-run-exit-1"],
        )
        self.assertEqual((verdict.state, verdict.exit), ("fresh", 1))
        (self.state / "last-run.json").write_text("not json")
        verdict = self.classify()
        self.assertEqual(verdict.state, "never_run")
        self.assertIn("last-run-exit-unknown", verdict.attention)

    def test_launchd_missing_only_on_macos(self):
        self.last_run(1)
        self.assertEqual(self.classify(macos=True).attention, ["launchd-missing"])
        self.assertEqual(self.classify(macos=False).attention, [])
        with (
            patch("cairn.routine.is_macos", return_value=True),
            patch("cairn.routine.launchctl", Launchctl()),
        ):
            code, out, _err = self.cli("status", "daily", "--json")
        self.assertEqual((code, json.loads(out)["attention"]), (1, ["launchd-missing"]))
        plist = (
            self.cfg.data_root.parent / "home/Library/LaunchAgents/dev.cairn.routine.daily.plist"
        )
        plist.parent.mkdir(parents=True)
        plist.write_text("x")
        self.assertEqual(self.classify(macos=True).attention, [])

    def test_interrupted_run_when_identity_dead(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.last_run(1)
        (self.state / "running.json").write_text(
            json.dumps({"pid": child.pid, "start_token": "gone"})
        )
        verdict = self.classify()
        self.assertEqual((verdict.state, verdict.attention), ("fresh", ["interrupted-run"]))
        self.assertEqual(verdict.exit, 1)


class TestStatusExit(RoutineFixture):
    def status(self, fake: Launchctl | None = None) -> tuple[int, dict]:
        with (
            patch("cairn.routine.is_macos", return_value=False),
            patch("cairn.routine.launchctl", fake or Launchctl()),
        ):
            code, out, _err = self.cli("status", "daily", "--json")
        return code, json.loads(out)

    def test_status_exit_codes_0_1_2(self):
        self.assertEqual(self.status()[1]["state"], "never_run")
        self.assertEqual(self.status()[0], 1)
        self.cli("run", "daily", "--day", "2026-11-10", children=Children(validate=1))
        code, report = self.status()
        self.assertEqual((code, report["attention"]), (1, ["failed-day", "last-run-exit-1"]))
        self.cli("run", "daily", children=Children())
        code, report = self.status()
        self.assertEqual((code, report["state"], report["attention"]), (0, "fresh", []))
        self.cli("pause", "daily", "--reason", "away")
        code, report = self.status()
        self.assertEqual((code, report["state"], report["paused"]["reason"]), (2, "paused", "away"))
        with patch("cairn.routine.is_macos", return_value=False):
            code, out, _err = self.cli("status", "daily")
        self.assertEqual(code, 2)
        self.assertIn("state: paused (exit 2)", out)
        code, out, _err = self.cli("status", "nightly", "--json")
        self.assertEqual((code, out), (2, ""))  # a usage error prints no JSON

    def test_status_never_calls_launchctl_for_exit(self):
        def wedged(*_args):
            raise AssertionError("launchctl must not decide the exit code")

        self.cli("run", "daily", children=Children())
        with patch("cairn.routine.launchctl", wedged):
            verdict = routine_state.classify(self.cfg, self.cfg.routines["daily"], macos=True)
        self.assertEqual((verdict.state, verdict.attention), ("fresh", ["launchd-missing"]))
        plist = (
            self.cfg.data_root.parent / "home/Library/LaunchAgents/dev.cairn.routine.daily.plist"
        )
        plist.parent.mkdir(parents=True)
        plist.write_text("x")
        with (
            patch("cairn.routine.is_macos", return_value=True),
            patch("cairn.routine.launchctl", Launchctl(loaded=False)),
        ):
            code, out, _err = self.cli("status", "daily", "--json")
        report = json.loads(out)
        self.assertEqual((code, report["installed"], report["state"]), (0, "unloaded", "fresh"))
