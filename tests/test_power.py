"""``cairn.power``: the mains-or-battery probe behind the nightly embed policy.

``pmset`` and the sysfs tree are fixtures; nothing here runs a real probe.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cairn import power

AC = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t62%; charging; 0:58 remaining\n"
BATTERY = "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t61%; discharging\n"
UPS = "Now drawing from 'UPS Power'\n"


class TestPmset(unittest.TestCase):
    def test_first_line_decides(self):
        self.assertEqual(power.parse_pmset(AC), "ac")
        self.assertEqual(power.parse_pmset(BATTERY), "battery")
        self.assertEqual(power.parse_pmset(UPS), "battery")

    def test_unreadable_output_is_unknown(self):
        for text in (None, "", "   \n", "Battery Warning: None\n"):
            self.assertEqual(power.parse_pmset(text), "unknown", repr(text))

    def test_run_pmset_failures_give_none(self):
        with patch("cairn.power.subprocess.run", side_effect=FileNotFoundError("pmset")):
            self.assertIsNone(power.run_pmset())
        with patch(
            "cairn.power.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["pmset"], power.PMSET_TIMEOUT_S),
        ):
            self.assertIsNone(power.run_pmset())
        failed = subprocess.CompletedProcess(["pmset"], 1, AC, "")
        with patch("cairn.power.subprocess.run", return_value=failed):
            self.assertIsNone(power.run_pmset())
        ok = subprocess.CompletedProcess(["pmset"], 0, AC, "")
        with patch("cairn.power.subprocess.run", return_value=ok):
            self.assertEqual(power.run_pmset(), AC)

    def test_power_source_on_macos_uses_pmset(self):
        with (
            patch("cairn.power.sys.platform", "darwin"),
            patch("cairn.power.run_pmset", return_value=BATTERY),
        ):
            self.assertEqual(power.power_source(), "battery")
        with (
            patch("cairn.power.sys.platform", "darwin"),
            patch("cairn.power.run_pmset", side_effect=RuntimeError("boom")),
        ):
            self.assertEqual(power.power_source(), "unknown")


class TestSysfs(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def supply(self, name: str, kind: str, online: str | None = None) -> None:
        directory = self.root / name
        directory.mkdir()
        (directory / "type").write_text(kind + "\n")
        if online is not None:
            (directory / "online").write_text(online + "\n")

    def test_mains_online_is_ac(self):
        self.supply("AC", "Mains", "1")
        self.supply("BAT0", "Battery")
        self.assertEqual(power.read_sysfs(self.root), "ac")

    def test_mains_offline_with_battery_is_battery(self):
        self.supply("AC", "Mains", "0")
        self.supply("BAT0", "Battery")
        self.assertEqual(power.read_sysfs(self.root), "battery")

    def test_no_supplies_is_unknown(self):
        self.assertEqual(power.read_sysfs(self.root), "unknown")
        self.assertEqual(power.read_sysfs(self.root / "missing"), "unknown")

    def test_power_source_on_linux_reads_sysfs_root(self):
        self.supply("AC", "Mains", "1")
        with (
            patch("cairn.power.sys.platform", "linux"),
            patch("cairn.power.SYSFS_ROOT", self.root),
        ):
            self.assertEqual(power.power_source(), "ac")

    def test_other_platforms_are_unknown(self):
        with patch("cairn.power.sys.platform", "win32"):
            self.assertEqual(power.power_source(), "unknown")


if __name__ == "__main__":
    unittest.main()
