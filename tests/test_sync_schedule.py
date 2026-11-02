"""``cairn cron install|uninstall|status`` and the scheduler module behind them.

``launchctl`` is a recorder, ``is_macos`` is patched, and the crontab is a list the
test hands in: no test loads a LaunchAgent or writes a crontab.
"""

from __future__ import annotations

import io
import json
import os
import plistlib
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cairn import admin, config, schedule
from cairn.config import ConfigError
from tests.test_shared_store import StoreFixture

LEGACY_LINE = (
    "7 * * * * /usr/bin/env CAIRN_CONFIG=/tmp/c.json /opt/cairn sync --cron "
    ">> /tmp/ctx/logs/context-sync.log 2>&1 # cairn sync"
)


class Launchctl:
    """A launchd that records calls. ``fail_bootstrap`` names labels whose bootstrap
    fails; ``stuck`` names labels that stay loaded after a bootout."""

    def __init__(self, loaded: bool = True, bootstrap_rc: int = 0, fail_bootstrap=(), stuck=()):
        self.calls: list[tuple[str, ...]] = []
        self.loaded = loaded
        self.bootstrap_rc = bootstrap_rc
        self.fail_bootstrap = set(fail_bootstrap)
        self.stuck = set(stuck)

    def __call__(self, *args, timeout=None):
        self.calls.append(args)
        label = args[-1].rsplit("/", 1)[-1]
        if args[0] == "bootstrap":
            failing = (
                self.bootstrap_rc
                or label.replace(".plist", "")
                in {f"{s}.plist" for s in self.fail_bootstrap} | self.fail_bootstrap
            )
            code = (self.bootstrap_rc or 5) if failing else 0
            return subprocess.CompletedProcess(["launchctl", *args], code, "", "nope")
        if args[0] == "bootout":
            code = 1 if label in self.stuck else 0
            return subprocess.CompletedProcess(["launchctl", *args], code, "", "")
        code = 0 if (args[0] != "print" or self.loaded or label in self.stuck) else 113
        return subprocess.CompletedProcess(["launchctl", *args], code, "", "")


class ScheduleFixture(StoreFixture):
    def setUp(self):
        super().setUp()
        self.crontab: list[str] = []
        self.written: list[list[str]] = []
        self.launchctl = Launchctl()

    def configure(self, **cron) -> None:
        data = json.loads(self.config.read_text())
        data["cron"] = cron
        self.config.write_text(json.dumps(data))
        config.get_config.cache_clear()

    def cli(self, *argv: str, macos: bool = True) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            patch("cairn.routine.launchctl", self.launchctl),
            patch("cairn.routine.is_macos", return_value=macos),
            patch("cairn.admin._current_crontab", side_effect=lambda: list(self.crontab)),
            patch("cairn.admin._write_crontab", side_effect=self._write),
            patch("cairn.admin._cairn_invocation", return_value=["/opt/cairn"]),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = admin.cron_main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def _write(self, lines: list[str]) -> None:
        self.written.append(list(lines))
        self.crontab = list(lines)

    @property
    def agents(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents"


class TestCalendarIntervals(StoreFixture):
    def test_subset_converts(self):
        self.assertEqual(schedule.calendar_intervals("7 * * * *"), [{"Minute": 7}])
        self.assertEqual(
            schedule.calendar_intervals("5,35 * * * *"), [{"Minute": 5}, {"Minute": 35}]
        )
        self.assertEqual(
            schedule.calendar_intervals("*/20 * * * *"),
            [{"Minute": 0}, {"Minute": 20}, {"Minute": 40}],
        )
        self.assertEqual(
            schedule.calendar_intervals("0 3 * * *", daily=True), [{"Hour": 3, "Minute": 0}]
        )

    def test_other_forms_are_config_invalid_with_the_hint(self):
        for expr in ("7 9 * * *", "7 9 * * 1", "7 * 1 * *", "a * * * *", "7 * * *", "*/0 * * * *"):
            with self.assertRaises(ConfigError) as caught:
                schedule.calendar_intervals(expr)
            self.assertEqual(caught.exception.code, "CONFIG_INVALID")
            self.assertIn(schedule.LAUNCHD_HINT, str(caught.exception))


class TestCronBackend(ScheduleFixture):
    def test_cron_backend_writes_light_and_nightly_lines(self):
        self.crontab = ["# unrelated"]
        code, out, _err = self.cli("install", macos=False)
        self.assertEqual(code, 0, out)
        lines = self.written[-1]
        self.assertEqual(lines[0], "# unrelated")
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[1].startswith("7 * * * * "), lines[1])
        self.assertTrue(
            lines[1].endswith("sync --light >> " + self._log() + " 2>&1 # cairn sync light")
        )
        self.assertTrue(lines[2].startswith("0 3 * * * "), lines[2])
        self.assertIn("/opt/cairn sync --nightly", lines[2])
        self.assertTrue(lines[2].endswith("# cairn sync nightly"))
        self.assertIn("Installed cairn sync light (crontab): hourly at minute 7", out)
        self.assertIn("Installed cairn sync nightly (crontab): daily at 03:00 local", out)

    def _log(self) -> str:
        return str(self.root / "data" / "logs" / "context-sync.log")

    def test_cron_backend_background_prefix_on_macos(self):
        self.configure(backend="cron")
        code, _out, _err = self.cli("install", macos=True)
        self.assertEqual(code, 0)
        for line in self.written[-1]:
            self.assertIn(" /usr/sbin/taskpolicy -b /usr/bin/env ", line)

    def test_cron_backend_nice_ionice_on_linux(self):
        which = {"nice": "/usr/bin/nice", "ionice": "/usr/bin/ionice"}
        with patch("cairn.schedule.shutil.which", side_effect=lambda name: which.get(name)):
            code, _out, err = self.cli("install", macos=False)
        self.assertEqual(code, 0)
        for line in self.written[-1]:
            self.assertIn(" /usr/bin/nice -n 10 /usr/bin/ionice -c 3 /usr/bin/env ", line)
        self.assertNotIn("ionice", err)
        with patch(
            "cairn.schedule.shutil.which",
            side_effect=lambda name: which.get(name) if name == "nice" else None,
        ):
            code, _out, err = self.cli("install", macos=False)
        self.assertEqual(code, 0)
        for line in self.written[-1]:
            self.assertIn(" /usr/bin/nice -n 10 /usr/bin/env ", line)
            self.assertNotIn("ionice", line)
        self.assertIn("ionice not found", err)

    def test_cron_backend_normal_priority_has_no_prefix(self):
        self.configure(priority="normal")
        code, _out, _err = self.cli("install", macos=False)
        self.assertEqual(code, 0)
        for line in self.written[-1]:
            self.assertTrue(line.split(" ", 5)[5].startswith("/usr/bin/env "), line)

    def test_install_removes_legacy_line_and_says_so(self):
        self.crontab = ["# keep", LEGACY_LINE]
        code, out, _err = self.cli("install", macos=False)
        self.assertEqual(code, 0)
        self.assertNotIn(LEGACY_LINE, self.written[-1])
        self.assertEqual(self.written[-1][0], "# keep")
        self.assertIn("Removed the old hourly 'cairn sync --cron' crontab entry", out)

    def test_install_is_idempotent(self):
        self.cli("install", macos=False)
        first = self.written[-1]
        code, out, _err = self.cli("install", macos=False)
        self.assertEqual(code, 0)
        self.assertEqual(self.written[-1], first)
        self.assertNotIn("Removed", out)

    def test_embed_hourly_installs_one_cron_job(self):
        self.configure(embed="hourly")
        self.cli("install", macos=False)
        lines = self.written[-1]
        self.assertEqual(len(lines), 1)
        self.assertIn("nice -n 10", lines[0])
        self.assertIn("sync --cron", lines[0])
        self.assertTrue(lines[0].endswith("# cairn sync light"))

    def test_embed_off_installs_light_only(self):
        self.configure(embed="off")
        self.cli("install", macos=False)
        lines = self.written[-1]
        self.assertEqual(len(lines), 1)
        self.assertIn("sync --light", lines[0])

    def test_uninstall_strips_lines_then_reports_nothing(self):
        self.cli("install", macos=False)
        code, out, _err = self.cli("uninstall", macos=False)
        self.assertEqual(code, 0)
        self.assertEqual(self.written[-1], [])
        self.assertEqual(out.count("Removed crontab entry"), 2)
        code, out, _err = self.cli("uninstall", macos=False)
        self.assertEqual(code, 0)
        self.assertIn("No cairn sync job found; nothing to remove.", out)

    def test_cron_status_reports_mismatch_and_legacy(self):
        self.cli("install", macos=False)
        code, out, _err = self.cli("status", macos=False)
        self.assertEqual(code, 0, out)
        self.assertIn("backend    : cron", out)
        self.assertIn("legacy     : none", out)
        self.configure(schedule="5 * * * *")
        self.crontab.append(LEGACY_LINE)
        code, out, _err = self.cli("status", "--json", macos=False)
        self.assertEqual(code, 1)
        report = json.loads(out)
        light = report["jobs"][0]
        self.assertTrue(light["installed"])
        self.assertFalse(light["matches_config"])
        self.assertEqual(light["schedule"], "7 * * * *")
        self.assertEqual([e["mode"] for e in report["legacy_entries"]], ["cron"])

    def test_crontab_read_failure_stops_install_with_a_code(self):
        with patch(
            "cairn.admin._current_crontab",
            side_effect=ConfigError("`crontab -l` failed (exit 1): denied"),
        ):
            out, err = io.StringIO(), io.StringIO()
            with (
                patch("cairn.routine.is_macos", return_value=False),
                patch("cairn.admin._write_crontab", side_effect=self._write),
                redirect_stdout(out),
                redirect_stderr(err),
            ):
                code = admin.cron_main(["install"])
        self.assertEqual(code, 2)
        self.assertEqual(self.written, [])
        self.assertIn("CONFIG_INVALID", err.getvalue())


class TestLaunchdBackend(ScheduleFixture):
    def test_launchd_plist_keys(self):
        cfg = config.get_config()
        light, nightly = schedule.jobs(cfg)
        environment = {"PATH": "/usr/bin:/bin", "CAIRN_CONFIG": str(self.config)}
        data = plistlib.loads(schedule.render_plist(cfg, light, environment, ["/opt/cairn"]))
        self.assertEqual(data["Label"], "dev.cairn.sync.light")
        self.assertEqual(data["ProcessType"], "Background")
        self.assertTrue(data["LowPriorityIO"])
        self.assertFalse(data["RunAtLoad"])
        self.assertEqual(data["ProgramArguments"], ["/opt/cairn", "sync", "--light"])
        self.assertEqual(data["EnvironmentVariables"], environment)
        self.assertEqual(data["StartCalendarInterval"], [{"Minute": 7}])
        self.assertEqual(data["StandardOutPath"], str(self.root / "data/logs/context-sync.log"))
        night = plistlib.loads(schedule.render_plist(cfg, nightly, environment, ["/opt/cairn"]))
        self.assertEqual(night["Label"], "dev.cairn.sync.nightly")
        self.assertEqual(night["ProgramArguments"][-2:], ["sync", "--nightly"])
        self.assertEqual(night["StartCalendarInterval"], [{"Hour": 3, "Minute": 0}])
        self.configure(priority="normal")
        cfg = config.get_config()
        data = plistlib.loads(schedule.render_plist(cfg, light, environment, ["/opt/cairn"]))
        self.assertEqual(data["ProcessType"], "Standard")
        self.assertNotIn("LowPriorityIO", data)

    def test_plist_environment_includes_the_state_home(self):
        # The jobs write the embed stamp under $XDG_STATE_HOME; the installing shell's
        # value must travel with the job or doctor reads a different file.
        self.cli("install")
        data = plistlib.loads((self.agents / "dev.cairn.sync.light.plist").read_bytes())
        self.assertEqual(
            data["EnvironmentVariables"]["XDG_STATE_HOME"],
            str(Path(os.environ["XDG_STATE_HOME"]).resolve()),
        )

    def test_launchd_install_order_and_crontab_cleanup(self):
        self.crontab = ["# keep", LEGACY_LINE]
        code, out, _err = self.cli("install")
        self.assertEqual(code, 0, out)
        uid = os.getuid()
        light = self.agents / "dev.cairn.sync.light.plist"
        nightly = self.agents / "dev.cairn.sync.nightly.plist"
        self.assertEqual(
            self.launchctl.calls,
            [
                ("bootout", f"gui/{uid}/dev.cairn.sync.light"),
                ("bootstrap", f"gui/{uid}", str(light)),
                ("bootout", f"gui/{uid}/dev.cairn.sync.nightly"),
                ("bootstrap", f"gui/{uid}", str(nightly)),
            ],
        )
        self.assertTrue(light.is_file() and nightly.is_file())
        self.assertEqual(self.written[-1], ["# keep"])
        self.assertIn("Removed the old hourly 'cairn sync --cron' crontab entry", out)
        self.assertIn(
            "Installed dev.cairn.sync.light: hourly at minute 7, background priority, log", out
        )
        self.assertIn(
            "Installed dev.cairn.sync.nightly: daily at 03:00 local, background priority, "
            "on battery: defer",
            out,
        )
        # Idempotent: the same files, no Removed line, no crontab write.
        self.written.clear()
        code, out, _err = self.cli("install")
        self.assertEqual(code, 0)
        self.assertNotIn("Removed", out)
        self.assertEqual(self.written, [])

    def test_embed_off_removes_the_nightly_agent(self):
        self.cli("install")
        self.configure(embed="off")
        self.launchctl.calls.clear()
        code, out, _err = self.cli("install")
        self.assertEqual(code, 0)
        self.assertFalse((self.agents / "dev.cairn.sync.nightly.plist").exists())
        self.assertIn(
            ("bootout", f"gui/{os.getuid()}/dev.cairn.sync.nightly"), self.launchctl.calls
        )
        self.assertIn("Removed dev.cairn.sync.nightly", out)

    def test_launchd_uninstall_removes_both_and_crontab(self):
        self.cli("install")
        self.crontab.append(LEGACY_LINE)
        self.launchctl.calls.clear()
        code, out, _err = self.cli("uninstall")
        self.assertEqual(code, 0)
        self.assertEqual(list(self.agents.glob("dev.cairn.sync.*")), [])
        self.assertEqual(
            [c for c in self.launchctl.calls if c[0] == "bootout"],
            [
                ("bootout", f"gui/{os.getuid()}/dev.cairn.sync.light"),
                ("bootout", f"gui/{os.getuid()}/dev.cairn.sync.nightly"),
            ],
        )
        self.assertNotIn(LEGACY_LINE, self.crontab)
        self.assertIn("Removed dev.cairn.sync.light", out)
        self.assertIn("Removed dev.cairn.sync.nightly", out)
        code, out, _err = self.cli("uninstall")
        self.assertEqual(code, 0)
        self.assertIn("No cairn sync job found; nothing to remove.", out)

    def test_launchd_bootstrap_failure_exits_1(self):
        self.launchctl = Launchctl(bootstrap_rc=5)
        code, _out, err = self.cli("install")
        self.assertEqual(code, 1)
        self.assertIn("launchctl bootstrap failed (exit 5): nope", err)

    def test_second_bootstrap_failure_is_visible_to_status_and_doctor(self):
        # The light job loads, the nightly bootstrap fails: install exits 1, and the
        # unloaded nightly job keeps status at exit 1 and makes doctor warn.
        self.launchctl = Launchctl(loaded=False, fail_bootstrap={"dev.cairn.sync.nightly"})
        code, _out, _err = self.cli("install")
        self.assertEqual(code, 1)
        self.assertTrue((self.agents / "dev.cairn.sync.nightly.plist").is_file())
        code, out, _err = self.cli("status", "--json")
        self.assertEqual(code, 1)
        nightly = json.loads(out)["jobs"][1]
        self.assertTrue(nightly["installed"] and nightly["matches_config"])
        self.assertIs(nightly["loaded"], False)
        out_, err_ = io.StringIO(), io.StringIO()
        with (
            patch("cairn.routine.launchctl", self.launchctl),
            patch("cairn.routine.is_macos", return_value=True),
            patch("cairn.admin._current_crontab", return_value=[]),
            redirect_stdout(out_),
            redirect_stderr(err_),
        ):
            admin._check_schedule(config.get_config())
        self.assertIn(
            "warn sync schedule - the nightly job is installed but not loaded; "
            "run 'cairn cron install'",
            out_.getvalue(),
        )
        self.assertNotIn("ok   sync schedule - launchd", out_.getvalue())

    def test_bootout_failure_keeps_the_plist_and_fails(self):
        self.cli("install")
        self.launchctl = Launchctl(stuck={"dev.cairn.sync.nightly"})
        code, _out, err = self.cli("uninstall")
        self.assertEqual(code, 1)
        self.assertIn("launchctl bootout failed for dev.cairn.sync.nightly", err)
        self.assertFalse((self.agents / "dev.cairn.sync.light.plist").exists())
        self.assertTrue((self.agents / "dev.cairn.sync.nightly.plist").exists())
        self.configure(backend="cron")
        code, _out, _err = self.cli("install")
        self.assertEqual(code, 1)
        self.assertTrue((self.agents / "dev.cairn.sync.nightly.plist").exists())

    def test_uninstall_needs_no_valid_config(self):
        self.cli("install")
        data = json.loads(self.config.read_text())
        data["cron"] = {"embed": "weekly"}
        self.config.write_text(json.dumps(data))
        config.get_config.cache_clear()
        code, out, _err = self.cli("uninstall")
        self.assertEqual(code, 0, out)
        self.assertEqual(list(self.agents.glob("dev.cairn.sync.*")), [])

    def test_line_mode_reads_tokens_not_substrings(self):
        line = (
            "0 3 * * * /usr/bin/env 'CAIRN_CONFIG=/tmp/sync --light/config.json' "
            "/opt/cairn sync --nightly >> /tmp/l 2>&1 # cairn sync nightly"
        )
        self.assertEqual(schedule._line_mode(line), "nightly")
        self.assertEqual(schedule._line_mode(LEGACY_LINE), "cron")
        self.assertEqual(
            schedule._line_mode("7 * * * * /opt/cairn sync >> /tmp/l # cairn sync"), "all"
        )
        self.assertIsNone(schedule._line_mode("7 * * * * echo 'unterminated # cairn sync"))
        self.assertEqual(schedule._argv_mode(["/opt/cairn", "sync", "--light"]), "light")
        self.assertEqual(schedule._argv_mode(["/opt/cairn", "sync"]), "all")
        self.assertIsNone(schedule._argv_mode(["/opt/cairn", "doctor"]))

    def test_launchd_backend_off_macos_is_config_invalid(self):
        self.configure(backend="launchd")
        code, _out, err = self.cli("install", macos=False)
        self.assertEqual(code, 2)
        self.assertIn("CONFIG_INVALID", err)
        self.assertIn("not macOS", err)

    def test_schedule_outside_the_subset_exits_2_before_any_write(self):
        self.configure(schedule="7 9 * * *")
        code, _out, err = self.cli("install")
        self.assertEqual(code, 2)
        self.assertIn(schedule.LAUNCHD_HINT, err)
        self.assertEqual(self.launchctl.calls, [])
        self.assertEqual(self.written, [])

    def test_switching_to_cron_removes_the_agents(self):
        self.cli("install")
        self.configure(backend="cron")
        code, out, _err = self.cli("install")
        self.assertEqual(code, 0)
        self.assertEqual(list(self.agents.glob("dev.cairn.sync.*")), [])
        self.assertEqual(len(self.written[-1]), 2)
        self.assertIn("Removed dev.cairn.sync.light", out)

    def test_cron_status_json_reports_mismatch(self):
        self.cli("install")
        self.configure(schedule="5 * * * *")
        code, out, _err = self.cli("status", "--json")
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertEqual(report["backend"], "launchd")
        light, nightly = report["jobs"]
        self.assertEqual(light["label_or_tag"], "dev.cairn.sync.light")
        self.assertTrue(light["installed"] and light["loaded"])
        self.assertFalse(light["matches_config"])
        self.assertEqual(light["schedule"], [{"Minute": 7}])
        self.assertTrue(nightly["matches_config"])
        self.assertIsNone(report["embed_stamp"])
        self.assertIsNone(report["owed"])
        self.assertEqual(report["legacy_entries"], [])

    def test_cron_status_text(self):
        self.cli("install")
        self.launchctl.loaded = False
        code, out, _err = self.cli("status")
        self.assertEqual(code, 1, out)  # installed but not loaded is not healthy
        self.assertIn("backend    : launchd", out)
        self.assertIn(
            "light      : dev.cairn.sync.light installed, not loaded, hourly at minute 7, background",
            out,
        )
        self.assertIn(
            "nightly    : dev.cairn.sync.nightly installed, not loaded, daily at 03:00 local, on battery: defer",
            out,
        )
        self.assertIn("last embed : none recorded", out)
        self.assertIn("owed       : none", out)
        self.assertIn("legacy     : none", out)
        code, out, _err = self.cli("status", "--json")
        self.assertEqual(code, 1)
        self.assertIs(json.loads(out)["jobs"][0]["loaded"], False)
        self.launchctl.loaded = True
        code, out, _err = self.cli("status")
        self.assertEqual(code, 0, out)
        self.assertIn(
            "light      : dev.cairn.sync.light loaded, hourly at minute 7, background", out
        )

    def test_status_with_nothing_installed_exits_1(self):
        code, out, _err = self.cli("status")
        self.assertEqual(code, 1)
        self.assertIn("light      : dev.cairn.sync.light not installed", out)


if __name__ == "__main__":
    sys.exit(0)
