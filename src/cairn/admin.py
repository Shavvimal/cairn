"""Admin commands behind the ``cairn`` dispatcher: ``config``, ``cron``, ``doctor``.

These are the side-effectful / introspection commands the ``/setup`` skill drives:
write the per-user config, manage the hourly sync crontab entry, and report health.
All cron/hook entry points are resolved to a PATH-independent invocation so they
survive cron's minimal environment.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import (
    Collection,
    ConfigError,
    example_template_text,
    get_config,
    load_config_from,
    user_config_path,
)
from .sync import SOURCE_COLLECTIONS

_CRON_TAG = "cairn sync"  # substring used to find/replace our line idempotently


def _cairn_invocation() -> list[str]:
    """A PATH-independent way to invoke the ``cairn`` CLI from cron.

    Prefer the installed console script's absolute path; fall back to
    ``<python> -m cairn`` (``sys.executable`` is always absolute).
    """
    found = shutil.which("cairn")
    return [found] if found else [sys.executable, "-m", "cairn"]


def _log_file() -> Path:
    return get_config().data_root / "logs" / "context-sync.log"


# ---------------------------------------------------------------------------
# cairn config
# ---------------------------------------------------------------------------


def _apply_choices(template: str, data_root: str | None, enable: list[str] | None) -> str:
    """Bake the setup choices into the bundled template (config-as-data).

    The user's integration picks become ``sync.enabled`` flags the engine reads;
    ``data_root`` is set if provided. With no choices we return the template
    verbatim so its inline ``//`` comments and formatting survive.
    """
    if data_root is None and enable is None:
        return template

    data = json.loads(template)
    if data_root is not None:
        data["data_root"] = data_root
    if enable is not None:
        wanted = {s.strip() for s in enable if s.strip()}
        for source, collection in SOURCE_COLLECTIONS.items():
            spec = data.get("collections", {}).get(collection)
            if isinstance(spec, dict):
                spec.setdefault("sync", {})["enabled"] = source in wanted
    return json.dumps(data, indent=2) + "\n"


def _config_init(data_root: str | None = None, enable: list[str] | None = None) -> int:
    target = user_config_path()
    if target.is_file():
        print(f"Config already exists: {target}")
        print("Edit it directly, or delete it first to regenerate from the template.")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_apply_choices(example_template_text(), data_root, enable), encoding="utf-8")
    print(f"Created {target} from the bundled template.")
    if enable is not None:
        print(f"Enabled integrations: {', '.join(enable) or '(none)'}")
    print("Now edit it for this machine: each enabled collection's store, qmd_binary,")
    print("and the project_groups / repo_catalog / project_descriptions catalog.")
    return 0


def _config_show_path() -> int:
    try:
        print(get_config().config_path)
        return 0
    except ConfigError as e:
        print(f"No config loaded: {e}", file=sys.stderr)
        print(f"It would live at: {user_config_path()}", file=sys.stderr)
        return 1


def _collection_view(name: str, coll: Collection) -> dict:
    """Machine-readable summary of one collection's sync policy + store."""
    return {
        "name": name,
        "enabled": coll.sync.enabled,
        "since": coll.sync.since,
        "on_hook": coll.sync.on_hook,
        "store": str(coll.store) if coll.store else None,
        "type": "service-docs" if coll.service_sources else "sessions",
    }


def _config_show(as_json: bool) -> int:
    """Print the resolved config - the read surface skills/setup query."""
    try:
        cfg = get_config()
    except ConfigError as e:
        print(f"No config loaded: {e}", file=sys.stderr)
        print(f"It would live at: {user_config_path()}", file=sys.stderr)
        return 1

    collections = [_collection_view(name, coll) for name, coll in sorted(cfg.collections.items())]
    if as_json:
        print(
            json.dumps(
                {
                    "config_path": str(cfg.config_path),
                    "data_root": str(cfg.data_root),
                    "qmd_binary": cfg.qmd_binary,
                    "cron_schedule": cfg.cron_schedule,
                    "collections": collections,
                },
                indent=2,
            )
        )
        return 0

    print(f"config_path : {cfg.config_path}")
    print(f"data_root   : {cfg.data_root}")
    print(f"qmd_binary  : {cfg.qmd_binary}")
    print(f"cron        : {cfg.cron_schedule}")
    print("collections :")
    for c in collections:
        state = "enabled " if c["enabled"] else "disabled"
        hook = "" if c["on_hook"] is None else f" on_hook={c['on_hook']}"
        store = f" store={c['store']}" if c["store"] else ""
        print(f"  {c['name']:22} [{state}] since={c['since']}{hook}{store}")
    return 0


# Friendly dotted keys -> where they live in the JSON. Collection fields accept either
# the collection name (claude-code-sessions) or its source alias (claude) as the prefix.
_TOP_LEVEL_KEYS = {"data_root", "qmd_binary"}
_COLLECTION_SYNC_FIELDS = {"enabled", "since", "on_hook"}
_BOOL_FIELDS = {"enabled", "on_hook"}


def _coerce(field: str, value: str) -> object:
    """Coerce a CLI string to the field's type (bool for enable/on_hook flags)."""
    if field in _BOOL_FIELDS:
        low = value.strip().lower()
        if low not in ("true", "false"):
            raise ValueError(f"{field} expects true|false, got {value!r}")
        return low == "true"
    return value


def _set_dotted(data: dict, key: str, value: str) -> None:
    """Apply ``key=value`` to the config dict in place. Raises ValueError on a bad key."""
    if key == "cron.schedule":
        data.setdefault("cron", {})["schedule"] = value
        return
    if key in _TOP_LEVEL_KEYS:
        data[key] = value
        return

    parts = key.split(".")
    if len(parts) == 2:
        prefix, field = parts
        collection = SOURCE_COLLECTIONS.get(prefix, prefix)
        colls = data.get("collections")
        if not isinstance(colls, dict) or collection not in colls:
            known = ", ".join(sorted(colls)) if isinstance(colls, dict) else "(none)"
            raise ValueError(f"unknown collection {collection!r}; known: {known}")
        spec = colls[collection]
        if field == "store":
            spec["store"] = value
            return
        if field in _COLLECTION_SYNC_FIELDS:
            spec.setdefault("sync", {})[field] = _coerce(field, value)
            return

    raise ValueError(
        f"unsupported key {key!r}. Use data_root, qmd_binary, cron.schedule, or "
        "<collection>.store|enabled|since|on_hook"
    )


def _config_set(key: str, value: str) -> int:
    """Write one config value to the in-effect config file, failing loud if it invalidates it."""
    try:
        target = get_config().config_path
    except ConfigError:
        target = user_config_path()
    if not target.is_file():
        print(f"No config at {target}. Run 'cairn config init' first.", file=sys.stderr)
        return 1

    original = target.read_text(encoding="utf-8")
    try:
        data = json.loads(original)
    except json.JSONDecodeError as e:
        print(f"Config at {target} is not valid JSON: {e}", file=sys.stderr)
        return 1

    try:
        _set_dotted(data, key, value)
    except ValueError as e:
        print(f"Cannot set {key!r}: {e}", file=sys.stderr)
        return 1

    target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    # Validate the file we just wrote; restore it if the change broke the config (§8: never
    # leave a half-valid config behind - fail loud and undo).
    try:
        load_config_from(target)
    except ConfigError as e:
        target.write_text(original, encoding="utf-8")
        print(f"Rejected: {key}={value} made the config invalid ({e}). Restored.", file=sys.stderr)
        return 1
    get_config.cache_clear()  # so later reads in this process see the change
    print(f"Set {key} = {value}")
    return 0


def config_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cairn config", description="Manage the per-user config")
    sub = parser.add_subparsers(dest="action", metavar="ACTION", required=True)
    p_init = sub.add_parser("init", help="create the per-user config from the bundled template")
    p_init.add_argument("--data-root", metavar="DIR", help="where exported markdown is written")
    p_init.add_argument(
        "--enable",
        metavar="SOURCES",
        help=(
            "comma-separated integrations to enable, e.g. 'claude,codex,granola' "
            f"(choices: {', '.join(SOURCE_COLLECTIONS)}); the rest are written disabled"
        ),
    )
    sub.add_parser("path", help="print the resolved config path")
    p_show = sub.add_parser(
        "show", help="print the resolved config (use --json for machine output)"
    )
    p_show.add_argument("--json", action="store_true", help="emit JSON on stdout")
    p_set = sub.add_parser(
        "set", help="set one config value (e.g. claude.store ~/.claude/projects)"
    )
    p_set.add_argument(
        "key",
        metavar="KEY",
        help="data_root | qmd_binary | cron.schedule | <collection>.store|enabled|since|on_hook",
    )
    p_set.add_argument("value", metavar="VALUE")
    args = parser.parse_args(argv)

    if args.action == "init":
        enable = args.enable.split(",") if args.enable is not None else None
        return _config_init(data_root=args.data_root, enable=enable)
    if args.action == "show":
        return _config_show(args.json)
    if args.action == "set":
        return _config_set(args.key, args.value)
    return _config_show_path()


# ---------------------------------------------------------------------------
# cairn cron
# ---------------------------------------------------------------------------


def _current_crontab() -> list[str]:
    """The user's current crontab lines, or ``[]`` when they simply have no crontab.

    ``crontab -l`` exits non-zero both when no crontab exists yet (benign) and on
    real failures (crontab not installed, permission denied). We treat only the
    well-known "no crontab" case as empty and surface anything else loudly - a
    swallowed read error would let install clobber entries or make uninstall falsely
    report "nothing to remove".
    """
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except FileNotFoundError as e:
        raise ConfigError("`crontab` command not found; cannot manage cron entries.") from e
    if result.returncode == 0:
        return result.stdout.strip().split("\n") if result.stdout.strip() else []
    if "no crontab" in result.stderr.lower():
        return []
    raise ConfigError(
        f"`crontab -l` failed (exit {result.returncode}): "
        f"{result.stderr.strip() or 'unknown error'}"
    )


def _write_crontab(lines: list[str]) -> None:
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)


def _without_cairn_lines(lines: list[str]) -> list[str]:
    """Drop any prior cairn/context-sync cron line so re-running is idempotent."""
    return [ln for ln in lines if _CRON_TAG not in ln and "context_sync.py" not in ln]


def _cron_install() -> int:
    schedule = get_config().cron_schedule  # data-driven (config 'cron.schedule')
    line = f"{schedule} {' '.join(_cairn_invocation())} sync --cron >> {_log_file()} 2>&1"
    lines = _without_cairn_lines(_current_crontab())
    lines.append(line)
    _write_crontab(lines)
    print(f"Installed crontab entry:\n  {line}")
    return 0


def _cron_uninstall() -> int:
    current = _current_crontab()
    remaining = _without_cairn_lines(current)
    if len(remaining) == len(current):
        print("No cairn cron entry found; nothing to remove.")
        return 0
    _write_crontab(remaining)
    print("Removed the cairn cron entry.")
    return 0


def cron_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cairn cron", description="Manage the hourly sync crontab entry"
    )
    sub = parser.add_subparsers(dest="action", metavar="ACTION", required=True)
    sub.add_parser("install", help="add/refresh the hourly 'cairn sync --cron' entry")
    sub.add_parser("uninstall", help="remove the cairn cron entry")
    args = parser.parse_args(argv)
    return _cron_install() if args.action == "install" else _cron_uninstall()


# ---------------------------------------------------------------------------
# cairn doctor
# ---------------------------------------------------------------------------


def _ok(label: str, detail: str = "") -> None:
    print(f"  ok   {label}" + (f" - {detail}" if detail else ""))


def _warn(label: str, detail: str = "") -> None:
    print(f"  warn {label}" + (f" - {detail}" if detail else ""))


def _fail(label: str, detail: str = "") -> None:
    print(f"  FAIL {label}" + (f" - {detail}" if detail else ""))


def doctor_main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(prog="cairn doctor", description="Check the cairn install").parse_args(
        argv
    )
    print(f"cairn {__version__} - health check\n")
    critical_failures = 0

    cairn_bin = shutil.which("cairn")
    if cairn_bin:
        _ok("cairn on PATH", cairn_bin)
    else:
        _warn("cairn on PATH", f"not found; falling back to {sys.executable} -m cairn")

    # config
    config = None
    try:
        config = get_config()
        _ok("config", str(config.config_path))
    except ConfigError as e:
        _fail("config", str(e))
        critical_failures += 1

    # qmd
    if config is not None:
        qmd = config.qmd_binary
        qmd_path = shutil.which(qmd) or (qmd if Path(qmd).is_file() else None)
        if qmd_path:
            _ok("qmd", qmd_path)
        else:
            _fail("qmd", f"{qmd!r} not found - install it (e.g. npm install -g @tobilu/qmd)")
            critical_failures += 1

    # data_root writable
    if config is not None:
        data_root = config.data_root
        try:
            data_root.mkdir(parents=True, exist_ok=True)
            _ok("data_root writable", str(data_root))
        except OSError as e:
            _fail("data_root writable", f"{data_root}: {e}")
            critical_failures += 1

    # enabled integrations + their stores (which sources `cairn sync` will run)
    if config is not None:
        for name, coll in sorted(config.collections.items()):
            if not coll.sync.enabled:
                _warn(f"source [{name}]", "disabled in config (sync.enabled=false)")
                continue
            if coll.store is None:
                continue  # e.g. service-docs is store-less (mirrors source dirs)
            if coll.store.exists():
                _ok(f"source [{name}]", f"enabled - store {coll.store}")
            else:
                _warn(f"source [{name}]", f"enabled but store {coll.store} not found")

    # cron
    if any(_CRON_TAG in ln for ln in _current_crontab()):
        _ok("cron", "hourly 'cairn sync --cron' entry present")
    else:
        _warn("cron", "no entry; run 'cairn cron install'")

    print()
    if critical_failures:
        print(f"{critical_failures} critical check(s) failed.")
        return 1
    print("All critical checks passed.")
    return 0


__all__ = ["config_main", "cron_main", "doctor_main"]
