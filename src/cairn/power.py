"""Where the machine draws power from: ``ac``, ``battery`` or ``unknown``.

The nightly embed (``cairn sync --nightly``) reads this before each QMD embed
session so a laptop on battery can defer the GPU work. ``unknown`` counts as mains
power: a desktop has no battery, and a probe that fails must never stop a job.

macOS asks ``pmset -g batt``; its first line is ``Now drawing from 'AC Power'``,
``'Battery Power'`` or ``'UPS Power'``. Linux reads the kernel's power-supply class
under ``/sys/class/power_supply``: a ``Mains`` supply that is ``online`` is ``ac``,
otherwise any ``Battery`` supply is ``battery``. The two probes are module-level
seams so the tests can replace them.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PMSET_TIMEOUT_S = 5
SYSFS_ROOT = Path("/sys/class/power_supply")


def run_pmset() -> str | None:
    """The stdout of ``pmset -g batt``, or ``None`` when it cannot be read."""
    try:
        result = subprocess.run(
            ["pmset", "-g", "batt"], capture_output=True, text=True, timeout=PMSET_TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def parse_pmset(text: str | None) -> str:
    """Classify ``pmset -g batt`` output; anything unexpected is ``unknown``."""
    if not text:
        return "unknown"
    first = text.strip().splitlines()[0] if text.strip() else ""
    if "'AC Power'" in first:
        return "ac"
    if "'Battery Power'" in first or "'UPS Power'" in first:
        return "battery"
    return "unknown"


def read_sysfs(root: Path) -> str:
    """Classify the power supplies under ``root`` (the Linux sysfs ABI)."""
    try:
        supplies = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return "unknown"
    saw_battery = False
    for supply in supplies:
        try:
            kind = (supply / "type").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if kind == "Mains":
            try:
                online = (supply / "online").read_text(encoding="utf-8").strip()
            except OSError:
                online = ""
            if online == "1":
                return "ac"
        elif kind == "Battery":
            saw_battery = True
    return "battery" if saw_battery else "unknown"


def power_source() -> str:
    """``ac``, ``battery`` or ``unknown``. Never raises."""
    try:
        if sys.platform == "darwin":
            return parse_pmset(run_pmset())
        if sys.platform.startswith("linux"):
            return read_sysfs(SYSFS_ROOT)
    except Exception:  # a probe must never stop a scheduled job
        return "unknown"
    return "unknown"


__all__ = ["SYSFS_ROOT", "parse_pmset", "power_source", "read_sysfs", "run_pmset"]
