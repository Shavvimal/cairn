"""The scheduled sync jobs: an hourly light sync and a nightly embed.

``cairn cron install`` installs the jobs that ``cron.*`` in the config describes
(:class:`cairn.config.SyncSchedule`). On macOS they are LaunchAgents
(``dev.cairn.sync.light`` and ``dev.cairn.sync.nightly``) with ``ProcessType
Background`` and ``LowPriorityIO``: launchd runs a job missed during sleep on wake,
and the priority is declarative. Elsewhere they are crontab lines under ``nice``
and ``ionice``. ``cron.backend: "cron"`` keeps cron on macOS, where the priority
prefix is ``taskpolicy -b``.

Every ``launchctl`` call goes through :func:`cairn.routine.launchctl` and every
crontab read or write through :mod:`cairn.admin`, which the tests replace. The
reader :func:`inspect` serves ``cairn cron status`` and the doctor's ``sync
schedule`` group.
"""

from __future__ import annotations

import plistlib
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import routine
from .config import SYNC_LABEL_PREFIX, CairnConfig, ConfigError
from .qmd import read_embed_stamp, read_owed
from .storage import atomic_write

JOB_NAMES = ("light", "nightly")
LAUNCHD_HINT = "set cron.backend to \"cron\", or use a schedule of the form 'M * * * *'"
_MODE_FLAGS = {"--hook": "hook", "--light": "light", "--nightly": "nightly", "--cron": "cron"}


@dataclass(frozen=True)
class Job:
    """One scheduled job: its name, the ``cairn sync`` mode it runs and its schedule."""

    name: str  # light | nightly
    mode: str  # light | nightly | cron
    schedule: str  # a 5-field crontab expression

    @property
    def label(self) -> str:
        return f"{SYNC_LABEL_PREFIX}{self.name}"

    @property
    def tag(self) -> str:
        return f"# cairn sync {self.name}"


def backend(config: CairnConfig) -> str:
    """``launchd`` or ``cron`` for this config on this platform."""
    chosen = config.schedule.backend
    if chosen == "auto":
        return "launchd" if routine.is_macos() else "cron"
    if chosen == "launchd" and not routine.is_macos():
        raise ConfigError('cron.backend is "launchd" but this is not macOS; set it to "cron"')
    return chosen


def jobs(config: CairnConfig) -> list[Job]:
    """The jobs ``cron.embed`` asks for."""
    policy = config.schedule
    if policy.embed == "hourly":
        return [Job("light", "cron", policy.schedule)]
    light = Job("light", "light", policy.schedule)
    if policy.embed == "off":
        return [light]
    hour, minute = policy.embed_at.split(":")
    return [light, Job("nightly", "nightly", f"{int(minute)} {int(hour)} * * *")]


def calendar_intervals(expr: str, *, daily: bool = False) -> list[dict[str, int]]:
    """``StartCalendarInterval`` entries for the crontab subset launchd can express.

    ``M * * * *``, ``M1,M2 * * * *`` and ``*/N * * * *`` convert; a non-``*`` hour,
    day, month or weekday field, or anything else, is refused with ``CONFIG_INVALID``.
    With ``daily`` (the nightly job, built from ``cron.embed_at``), the form is
    ``MINUTE HOUR * * *``.
    """
    fields = expr.split()
    if len(fields) != 5 or fields[2:] != ["*", "*", "*"]:
        raise ConfigError(
            f"cron.schedule {expr!r} cannot be expressed as a LaunchAgent; {LAUNCHD_HINT}"
        )
    minute, hour = fields[0], fields[1]
    if daily:
        if minute.isdigit() and hour.isdigit() and int(minute) < 60 and int(hour) < 24:
            return [{"Hour": int(hour), "Minute": int(minute)}]
        raise ConfigError(f"the nightly schedule {expr!r} is not MINUTE HOUR * * *")
    if hour != "*":
        raise ConfigError(
            f"cron.schedule {expr!r} cannot be expressed as a LaunchAgent; {LAUNCHD_HINT}"
        )
    if minute.startswith("*/") and minute[2:].isdigit() and 1 <= int(minute[2:]) <= 59:
        step = int(minute[2:])
        return [{"Minute": m} for m in range(0, 60, step)]
    parts = minute.split(",")
    if all(part.isdigit() and int(part) < 60 for part in parts):
        return [{"Minute": int(part)} for part in parts]
    raise ConfigError(
        f"cron.schedule {expr!r} cannot be expressed as a LaunchAgent; {LAUNCHD_HINT}"
    )


def describe_schedule(expr: str) -> str:
    """``hourly at minute 7``, ``daily at 03:00 local`` or the expression itself."""
    fields = expr.split()
    if len(fields) == 5 and fields[2:] == ["*", "*", "*"]:
        minute, hour = fields[0], fields[1]
        if hour == "*" and minute.isdigit():
            return f"hourly at minute {int(minute)}"
        if hour.isdigit() and minute.isdigit():
            return f"daily at {int(hour):02d}:{int(minute):02d} local"
        if hour == "*" and minute.startswith("*/"):
            return f"every {minute[2:]} minutes"
    return expr


def log_file(config: CairnConfig) -> Path:
    return config.data_root / "logs" / "context-sync.log"


def plist_path(job: Job) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{job.label}.plist"


def _plists() -> list[Path]:
    """Every ``dev.cairn.sync.*`` plist on disk."""
    directory = Path.home() / "Library" / "LaunchAgents"
    try:
        return sorted(directory.glob(f"{SYNC_LABEL_PREFIX}*.plist"))
    except OSError:
        return []


def render_plist(
    config: CairnConfig, job: Job, environment: dict[str, str], invocation: list[str]
) -> bytes:
    """The LaunchAgent plist for ``job`` (``plistlib`` escapes the XML)."""
    log = str(log_file(config))
    payload: dict[str, object] = {
        "EnvironmentVariables": environment,
        "Label": job.label,
        "ProcessType": "Background" if config.schedule.priority == "background" else "Standard",
        "ProgramArguments": [*invocation, "sync", f"--{job.mode}"],
        "RunAtLoad": False,
        "StandardErrorPath": log,
        "StandardOutPath": log,
        "StartCalendarInterval": calendar_intervals(job.schedule, daily=job.name == "nightly"),
    }
    if config.schedule.priority == "background":
        payload["LowPriorityIO"] = True
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def priority_prefix(config: CairnConfig) -> list[str]:
    """The argv prefix that runs a crontab job at background priority."""
    if config.schedule.priority != "background":
        return []
    if routine.is_macos():
        return ["/usr/sbin/taskpolicy", "-b"]
    nice = shutil.which("nice") or "nice"
    prefix = [nice, "-n", "10"]
    ionice = shutil.which("ionice")
    if ionice:
        prefix += [ionice, "-c", "3"]
    else:
        print("note: ionice not found; the cron jobs run under nice only", file=sys.stderr)
    return prefix


def crontab_line(
    config: CairnConfig,
    job: Job,
    environment: dict[str, str],
    invocation: list[str],
    prefix: list[str],
) -> str:
    argv = [
        *prefix,
        "/usr/bin/env",
        *[f"{key}={value}" for key, value in environment.items()],
        *invocation,
        "sync",
        f"--{job.mode}",
    ]
    command = f"{shlex.join(argv)} >> {shlex.quote(str(log_file(config)))} 2>&1"
    # Cron interprets percent signs even inside shell quotes. A stable comment
    # identifies our entry even when the executable itself needs shell quoting.
    command = command.replace("%", r"\%")
    return f"{job.schedule} {command} {job.tag}"


def _argv_mode(argv: list[str]) -> str | None:
    """The ``cairn sync`` mode an argv runs: the token after ``sync``, else ``None``."""
    for i, token in enumerate(argv):
        if token == "sync":
            rest = argv[i + 1 : i + 2]
            if not rest or rest[0] == "--all":
                return "all"
            return _MODE_FLAGS.get(rest[0], "all" if not rest[0].startswith("--") else None)
    return None


def _line_mode(line: str) -> str | None:
    """The ``cairn sync`` mode a crontab line runs, read from its tokens (a quoted
    path that happens to hold ``sync --light`` does not count)."""
    command = " ".join(line.split()[5:]) if len(line.split()) > 5 else line
    command = command.split(" # ", 1)[0]
    try:
        tokens = shlex.split(command.replace(r"\%", "%"))
    except ValueError:
        return None
    return _argv_mode(tokens)


def _line_priority(line: str) -> str:
    return "background" if ("taskpolicy -b" in line or "nice -n" in line) else "normal"


def _line_schedule(line: str) -> str:
    return " ".join(line.split()[:5])


def _domain() -> str:
    return routine._domain()


def _bootout(label: str) -> bool:
    return routine.launchctl("bootout", f"{_domain()}/{label}").returncode == 0


def _unload(label: str) -> tuple[bool, bool]:
    """Boot ``label`` out; return ``(was loaded, unloaded)``.

    ``launchctl bootout`` exits non-zero both for a job launchd does not know and
    for a job it could not stop, so the probe decides which: a job that was loaded
    and is still loaded after the bootout is a failure, and its plist must stay.
    """
    loaded = _loaded(label)
    if _bootout(label):
        return bool(loaded), True
    if loaded and _loaded(label):
        print(f"launchctl bootout failed for {label}; the job is still loaded", file=sys.stderr)
        return True, False
    return bool(loaded), True


def _remove_agent(path: Path) -> bool:
    """Boot out and delete one ``dev.cairn.sync.*`` plist; False when it stays loaded."""
    _was_loaded, unloaded = _unload(path.stem)
    if not unloaded:
        return False
    path.unlink(missing_ok=True)
    print(f"Removed {path.stem}")
    return True


def _loaded(label: str) -> bool | None:
    """Whether launchd knows ``label``; ``None`` when the probe did not answer."""
    try:
        probe = routine.launchctl(
            "print", f"{_domain()}/{label}", timeout=routine.LAUNCHCTL_PROBE_S
        )
    except subprocess.TimeoutExpired:
        return None
    return probe.returncode == 0


def _remove_crontab_lines(current: list[str]) -> list[str]:
    """Write the crontab without cairn lines; return the removed lines."""
    from . import admin

    remaining = admin._without_cairn_lines(current)
    removed = [line for line in current if line not in remaining]
    if removed:
        admin._write_crontab(remaining)
    return removed


def _legacy_note(removed: list[str], config: CairnConfig) -> None:
    if config.schedule.embed != "hourly" and any(_line_mode(ln) == "cron" for ln in removed):
        print("Removed the old hourly 'cairn sync --cron' crontab entry (it embedded every hour).")


def install(config: CairnConfig) -> int:
    """Install the configured jobs for the chosen backend; remove what it does not use."""
    from . import admin

    chosen = backend(config)
    wanted = jobs(config)
    environment = routine.environment_block(config)
    invocation = routine.cairn_invocation()
    current = admin._current_crontab()  # a read failure stops install before any write
    log = log_file(config)
    log.parent.mkdir(parents=True, exist_ok=True)
    if chosen == "launchd":
        rendered = {job: render_plist(config, job, environment, invocation) for job in wanted}
        removed = _remove_crontab_lines(current)
        _legacy_note(removed, config)
        keep = {plist_path(job) for job in wanted}
        for stale in _plists():
            if stale not in keep and not _remove_agent(stale):
                return 1
        for job in wanted:
            path = plist_path(job)
            path.parent.mkdir(parents=True, exist_ok=True)
            _bootout(job.label)  # not loaded is fine: re-install replaces
            atomic_write(path, rendered[job].decode("utf-8"))
            result = routine.launchctl("bootstrap", _domain(), str(path))
            if result.returncode != 0:
                print(
                    f"launchctl bootstrap failed (exit {result.returncode}): "
                    f"{result.stderr.strip() or result.stdout.strip()}",
                    file=sys.stderr,
                )
                return 1
            print(f"Installed {job.label}: {_describe(config, job)}")
        return 0

    prefix = priority_prefix(config)
    lines = [crontab_line(config, job, environment, invocation, prefix) for job in wanted]
    remaining = admin._without_cairn_lines(current)
    removed = [line for line in current if line not in remaining]
    admin._write_crontab([*remaining, *lines])
    _legacy_note(removed, config)
    if routine.is_macos():
        for stale in _plists():
            if not _remove_agent(stale):
                return 1
    for job in wanted:
        print(f"Installed cairn sync {job.name} (crontab): {_describe(config, job)}")
    return 0


def _describe(config: CairnConfig, job: Job) -> str:
    priority = f"{config.schedule.priority} priority"
    when = describe_schedule(job.schedule)
    if job.name == "nightly":
        return f"{when}, {priority}, on battery: {config.schedule.embed_on_battery}"
    return f"{when}, {priority}, log {log_file(config)}"


def uninstall() -> int:
    """Remove every cairn crontab line and every ``dev.cairn.sync.*`` LaunchAgent.

    Needs no config: a job must be removable when the config is missing or invalid.
    """
    from . import admin

    removed = _remove_crontab_lines(admin._current_crontab())
    found = bool(removed)
    failed = False
    for line in removed:
        print(f"Removed crontab entry: {line}")
    if routine.is_macos():
        for stale in _plists():
            found = True
            if not _remove_agent(stale):
                failed = True
    if not found:
        print("No cairn sync job found; nothing to remove.")
    return 1 if failed else 0


def remove_agents() -> bool:
    """Boot out and delete every ``dev.cairn.sync.*`` plist; False when one stays loaded."""
    return all(_remove_agent(path) for path in _plists())


def inspect(config: CairnConfig) -> dict:
    """What is installed against what the config wants (read-only; see ``cron status``)."""
    from . import admin

    chosen = backend(config)
    wanted = jobs(config)
    crontab = [ln for ln in admin._current_crontab() if admin._CRON_TAG in ln]
    report: dict = {
        "backend": chosen,
        "jobs": [],
        "embed_stamp": read_embed_stamp(),
        "owed": read_owed(),
        "legacy_entries": [],
    }
    claimed: set[str] = set()
    for job in wanted:
        entry: dict = {
            "name": job.name,
            "label_or_tag": job.label if chosen == "launchd" else job.tag,
            "installed": False,
            "loaded": None,
            "schedule": None,
            "argv_mode": None,
            "priority": None,
            "matches_config": False,
            "wanted_schedule": job.schedule,
            "wanted_mode": job.mode,
        }
        if chosen == "launchd":
            path = plist_path(job)
            if path.is_file():
                entry["installed"] = True
                entry["loaded"] = _loaded(job.label)
                try:
                    data = plistlib.loads(path.read_bytes())
                except (OSError, plistlib.InvalidFileException, ValueError):
                    data = {}
                argv = data.get("ProgramArguments") or []
                entry["argv_mode"] = _argv_mode([str(a) for a in argv])
                entry["priority"] = (
                    "background" if data.get("ProcessType") == "Background" else "normal"
                )
                interval = data.get("StartCalendarInterval")
                entry["schedule"] = interval
                try:
                    same_schedule = interval == calendar_intervals(
                        job.schedule, daily=job.name == "nightly"
                    )
                except ConfigError:
                    same_schedule = False
                entry["matches_config"] = (
                    same_schedule
                    and entry["argv_mode"] == job.mode
                    and entry["priority"] == config.schedule.priority
                )
        else:
            match = next((ln for ln in crontab if ln.rstrip().endswith(job.tag)), None)
            if match is not None:
                claimed.add(match)
                entry["installed"] = True
                entry["loaded"] = True
                entry["schedule"] = _line_schedule(match)
                entry["argv_mode"] = _line_mode(match)
                entry["priority"] = _line_priority(match)
                entry["matches_config"] = (
                    entry["schedule"] == job.schedule
                    and entry["argv_mode"] == job.mode
                    and entry["priority"] == config.schedule.priority
                )
        report["jobs"].append(entry)
    for line in crontab:
        if line not in claimed:
            report["legacy_entries"].append(
                {"kind": "crontab", "mode": _line_mode(line), "text": line}
            )
    if routine.is_macos():
        labels = {job.label for job in wanted} if chosen == "launchd" else set()
        for stale in _plists():
            if stale.stem not in labels:
                report["legacy_entries"].append(
                    {"kind": "launchd", "mode": None, "text": stale.stem}
                )
    report["ok"] = all(
        job["installed"] and job["matches_config"] and job["loaded"] is not False
        for job in report["jobs"]
    )
    return report


def status(config: CairnConfig, as_json: bool) -> int:
    """``cairn cron status``: exit 0 when every configured job is installed and matches."""
    import json

    report = inspect(config)
    if as_json:
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    policy = config.schedule
    print(f"backend    : {report['backend']}")
    for job, entry in zip(jobs(config), report["jobs"], strict=True):
        if not entry["installed"]:
            state = "not installed"
        elif entry["loaded"] is False:
            state = "installed, not loaded"
        elif entry["loaded"] is None:
            state = "installed, launchd did not answer"
        else:
            state = "loaded"
        detail = f"{entry['label_or_tag'].lstrip('# ')} {state}, {describe_schedule(job.schedule)}"
        if job.name == "nightly":
            detail += f", on battery: {policy.embed_on_battery}"
        else:
            detail += f", {policy.priority}"
        if entry["installed"] and not entry["matches_config"]:
            detail += " (differs from the config; run 'cairn cron install')"
        print(f"{job.name:<11}: {detail}")
    stamp = report["embed_stamp"]
    if stamp is None:
        print("last embed : none recorded")
    else:
        sessions = stamp.get("sessions", 0)
        plural = "" if sessions == 1 else "s"
        print(
            f"last embed : {stamp['finished_at']} {stamp['outcome']} "
            f"({sessions} session{plural}, {stamp.get('pending')} pending)"
        )
    owed = report["owed"]
    print(
        "owed       : none"
        if owed is None
        else f"owed       : since {owed['since']} ({owed['reason']})"
    )
    legacy = report["legacy_entries"]
    if not legacy:
        print("legacy     : none")
    for entry in legacy:
        print(f"legacy     : {entry['kind']} {entry['text']}")
    return 0 if report["ok"] else 1


__all__ = [
    "JOB_NAMES",
    "Job",
    "backend",
    "calendar_intervals",
    "crontab_line",
    "describe_schedule",
    "inspect",
    "install",
    "jobs",
    "priority_prefix",
    "remove_agents",
    "render_plist",
    "status",
    "uninstall",
]
