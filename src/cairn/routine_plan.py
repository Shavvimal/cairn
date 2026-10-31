"""Routine planning: paths, templates, the day set and stamp reads (docs/routine-spec.md
sections 3, 6.0, 6.2.5 and 7). Pure apart from reading files; ``routine_run`` acts on it.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .config import (
    PROFILES,
    TEMPLATE_TOKEN_RE,
    CairnConfig,
    RoutineSpec,
    expand_home,
    resolve_profile,
    routine_paths,
)
from .mutations import ROUTINE_OWNER_PREFIX
from .storage import atomic_write

ATTEMPT_CAP = 3
EXIT_EMPTY = 3
# Failure reasons that never consume one of the ATTEMPT_CAP attempts.
UNCOUNTED_REASONS = ("incomplete-input", "render-error", "budget-exhausted", "stale-source")
_ATTEMPT_RE = re.compile(r"^attempt-(\d+)$")


class RoutineError(Exception):
    """A usage, config or render error: the run exits 2 and writes no stamp.

    ``code`` names the failure in the error registry (``cairn errors CODE``).
    """

    def __init__(self, message: str = "", code: str = "ROUTINE_USAGE") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def state_dir(config: CairnConfig, name: str) -> Path:
    """``$XDG_STATE_HOME/cairn/routines/<name>``: the routine's stamps and lock."""
    return Path(routine_paths(config, name)["state_dir"])


def log_path(config: CairnConfig, name: str) -> Path:
    """``<data_root>/logs/routine-<name>.log``."""
    return Path(routine_paths(config, name)["log"])


def day_root(config: CairnConfig, name: str, day: date) -> Path:
    """``<data_root>/routines/<name>/<DAY>``: the parent of every attempt dir."""
    return config.data_root / "routines" / name / day.isoformat()


def next_attempt(config: CairnConfig, name: str, day: date) -> int:
    """1 + the highest ``attempt-<k>`` already on disk for ``day``, else 1."""
    root = day_root(config, name, day)
    numbers = [0]
    if root.is_dir():
        for child in root.iterdir():
            match = _ATTEMPT_RE.match(child.name)
            if match:
                numbers.append(int(match.group(1)))
    return max(numbers) + 1


def attempt_dir(config: CairnConfig, name: str, day: date, number: int) -> Path:
    """``<data_root>/routines/<name>/<DAY>/attempt-<n>``."""
    return day_root(config, name, day) / f"attempt-{number}"


def stamp_path(sdir: Path, kind: str, day: date) -> Path:
    """The ``done`` or ``failed`` stamp file of ``day`` in the state directory."""
    return sdir / f"{kind}-{day.isoformat()}.json"


def read_json(path: Path) -> dict | None:
    """A JSON object from ``path``, or ``None`` when it is missing or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path: Path, data: dict | list) -> None:
    """Write ``data`` as indented JSON, atomically."""
    atomic_write(path, json.dumps(data, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Templates (section 3)
# ---------------------------------------------------------------------------


def template_variables(
    config: CairnConfig, spec: RoutineSpec, day: date, attempt: Path
) -> dict[str, str]:
    """The exact variable set of section 3.1 for one day and attempt."""
    values = {
        "DAY": day.isoformat(),
        "DATA_ROOT": str(config.data_root),
        "ATTEMPT_DIR": str(attempt),
        "DIGEST_DIR": str(attempt / "digest"),
    }
    for key, path in spec.outputs.items():
        values[key.upper()] = str(path)
    return values


def _unknown(spec: RoutineSpec, token: str, where: str, variables: dict) -> RoutineError:
    """The exit-2 error for an unknown ``{{token}}`` at ``where``, listing the known ones."""
    return RoutineError(
        f"routine {spec.name}: unknown template variable {{{{{token}}}}} in {where}; "
        f"known: {', '.join(sorted(variables))}",
        code="ROUTINE_TEMPLATE_UNKNOWN_VAR",
    )


def render_text(text: str, variables: dict[str, str], unknown) -> str:
    """One pass of ``{{NAME}}`` substitution; a substituted value is never rescanned.

    ``unknown(name, offset)`` builds the exception raised for a name not in
    ``variables``. Text that does not match the token regex stays literal.
    """

    def substitute(match: re.Match) -> str:
        """The value of one ``{{NAME}}`` match, or raise for an unknown name."""
        name = match.group(1)
        if name not in variables:
            raise unknown(name, match.start())
        return variables[name]

    return TEMPLATE_TOKEN_RE.sub(substitute, text)


def render_argv(
    spec: RoutineSpec, argv: tuple[str, ...], where: str, variables: dict[str, str]
) -> list[str]:
    """Render each argv element, then expand a leading ``~`` / ``~/`` (as a shell would)."""
    rendered: list[str] = []
    for index, element in enumerate(argv):
        value = render_text(
            element,
            variables,
            lambda name, _pos, i=index: _unknown(spec, name, f"{where}[{i}]", variables),
        )
        rendered.append(expand_home(value))
    return rendered


@dataclass
class Rendered:
    """One attempt's rendered prompt and argv lists."""

    prompt: str
    prepare: list[str] | None
    validate: list[str]
    profile_args: list[str] | None  # None for a built-in profile


def check_inputs(spec: RoutineSpec) -> None:
    """The static checks of section 3.5: prompt file readable, ``cd`` a directory."""
    if not spec.prompt_file.is_file():
        raise RoutineError(
            f"routine {spec.name}: prompt_file not found: {spec.prompt_file}",
            code="ROUTINE_INPUT_INVALID",
        )
    if not spec.cd.is_dir():
        raise RoutineError(
            f"routine {spec.name}: cd is not a directory: {spec.cd}",
            code="ROUTINE_INPUT_INVALID",
        )


def render_day(config: CairnConfig, spec: RoutineSpec, day: date, attempt: Path) -> Rendered:
    """Render the prompt, ``prepare``, ``validate`` and the profile args in memory.

    Writes nothing, so an unknown variable leaves no attempt dir behind.
    """
    check_inputs(spec)
    variables = template_variables(config, spec, day, attempt)
    try:
        raw = spec.prompt_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RoutineError(
            f"routine {spec.name}: prompt_file not readable: {spec.prompt_file} ({exc})",
            code="ROUTINE_INPUT_INVALID",
        ) from None

    def prompt_unknown(name: str, offset: int) -> RoutineError:
        """The unknown-variable error, naming the prompt file line."""
        line = raw.count("\n", 0, offset) + 1
        return _unknown(spec, name, f"{spec.prompt_file} line {line}", variables)

    prompt = render_text(raw, variables, prompt_unknown)
    if not prompt.strip():
        raise RoutineError(
            f"routine {spec.name}: {spec.prompt_file} renders to an empty prompt",
            code="ROUTINE_INPUT_INVALID",
        )
    prepare = (
        render_argv(spec, spec.prepare, "prepare", variables) if spec.prepare is not None else None
    )
    validate = render_argv(spec, spec.validate, "validate", variables)
    profile_args = None
    if spec.profile not in PROFILES:
        policy = config.invoke.for_peer(spec.peer)
        _base, extra = resolve_profile(policy, spec.profile, peer=spec.peer)
        where = f"invoke.{spec.peer}.profiles.{spec.profile}.extra_args"
        profile_args = render_argv(spec, extra, where, variables)
    return Rendered(prompt, prepare, validate, profile_args)


# ---------------------------------------------------------------------------
# Day set (section 7.1) and refresh window (7.2)
# ---------------------------------------------------------------------------


def utc_today() -> date:
    """Today's date in UTC (patched in tests)."""
    return datetime.now(UTC).date()


def window(today: date, catch_up_days: int) -> list[date]:
    """The catch-up window, oldest first: ``today - catch_up_days`` .. ``today - 1``."""
    return [today - timedelta(days=k) for k in range(catch_up_days, 0, -1)]


def attempts_used(failed: dict | None) -> int:
    """The counted attempts recorded in a failed stamp, 0 without one."""
    return int(failed.get("attempts") or 0) if failed else 0


def compute_days(
    spec: RoutineSpec, sdir: Path, today: date, day: date | None = None, force: bool = False
) -> tuple[list[date], list[tuple[date, str]]]:
    """``(days to run, [(day, skip reason)])`` per section 7.1."""
    candidates = [day] if day is not None else window(today, spec.catch_up_days)
    days: list[date] = []
    skipped: list[tuple[date, str]] = []
    for candidate in candidates:
        if not force:
            if stamp_path(sdir, "done", candidate).is_file():
                if day is not None:
                    skipped.append((candidate, "done"))
                continue
            failed = stamp_path(sdir, "failed", candidate)
            if failed.is_file() and read_json(failed) is None:
                # Fail closed: a stamp that cannot be read must not reset the cap or
                # the day's spend. --force runs the day anyway (a new history starts).
                skipped.append((candidate, "corrupt-stamp"))
                continue
            if attempts_used(read_json(failed)) >= ATTEMPT_CAP:
                skipped.append((candidate, "attempts-exhausted"))
                continue
        days.append(candidate)
    return days, skipped


def refresh_since(days: list[date]) -> str:
    """Local midnight of the earliest day minus one is at or before its UTC start."""
    return (min(days) - timedelta(days=1)).isoformat()


def refresh_argv(source: str, since: str) -> list[str]:
    """``cairn <source> export --since <since>`` with this interpreter."""
    return [sys.executable, "-m", "cairn", source, "export", "--since", since]


def prior_history(sdir: Path, day: date) -> list[dict]:
    """Earlier attempts of ``day``: the failed stamp's history, else (a ``--force``
    re-run of a done day) the done stamp's history plus its own attempt."""
    failed = read_json(stamp_path(sdir, "failed", day))
    if failed is not None:
        return list(failed.get("history") or [])
    done = read_json(stamp_path(sdir, "done", day))
    if done is None:
        return []
    entry = {
        "attempt": done.get("attempt"),
        "reason": done.get("status"),
        "cost_usd": done.get("cost_usd"),
        "finished_at": done.get("finished_at"),
    }
    return [*(done.get("history") or []), entry]


def spent_on(sdir: Path, day: date) -> float:
    """The cost of ``day``'s earlier attempts; a ``null`` cost counts as 0."""
    return sum(float(h.get("cost_usd") or 0) for h in prior_history(sdir, day))


# ---------------------------------------------------------------------------
# Child argv and environment
# ---------------------------------------------------------------------------

DEPTH_ENV = "CAIRN_INVOKE_DEPTH"


def child_environment(
    name: str | None = None, day: date | None = None, attempt: Path | None = None
) -> dict[str, str]:
    """``os.environ`` minus the caller-identity names, plus the routine names (6.2.2)."""
    from .invoke import CALLER_ENV, SCRUBBED_ENV

    drop = {*SCRUBBED_ENV, DEPTH_ENV, CALLER_ENV}
    env = {key: value for key, value in os.environ.items() if key not in drop}
    if name is not None and day is not None and attempt is not None:
        env.update(
            CAIRN_ROUTINE=name,
            CAIRN_ROUTINE_DAY=day.isoformat(),
            CAIRN_ROUTINE_ATTEMPT_DIR=str(attempt),
        )
    return env


def invoke_argv(
    spec: RoutineSpec, attempt: Path, *, config_profile: bool, remaining_usd: float | None
) -> list[str]:
    """The ``cairn invoke`` argv of 6.2.1 step 6; ``--extra`` stays last (REMAINDER)."""
    argv = [sys.executable, "-m", "cairn", "invoke", spec.peer]
    argv += ["--prompt-file", str(attempt / "prompt.md"), "--cd", str(spec.cd)]
    argv += ["--profile", spec.profile]
    if config_profile:
        argv += ["--profile-args-file", str(attempt / "profile-args.json")]
    if spec.model:
        argv += ["--model", spec.model]
    if spec.timeout_s:
        argv += ["--timeout-s", str(spec.timeout_s)]
    argv += ["--owner", f"{ROUTINE_OWNER_PREFIX}{spec.name}", "--json"]
    if remaining_usd is not None:
        argv += ["--extra", "--max-budget-usd", f"{remaining_usd:.2f}"]
    return argv


def parse_result(stdout: str) -> dict | None:
    """The last non-empty stdout line as a JSON object, else ``None``."""
    for line in reversed(stdout.splitlines()):
        if line.strip():
            try:
                data = json.loads(line)
            except ValueError:
                return None
            return data if isinstance(data, dict) else None
    return None


def result_cost(result: dict | None) -> float | None:
    """``usage.total_cost_usd`` from an invoke result line, or ``None``."""
    usage = (result or {}).get("usage")
    cost = usage.get("total_cost_usd") if isinstance(usage, dict) else None
    return float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else None
