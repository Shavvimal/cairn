"""Admin commands behind the ``cairn`` dispatcher: ``config``, ``cron``, ``doctor``.

These are the side-effectful / introspection commands the ``/setup`` skill drives:
write the per-user config, manage the hourly sync crontab entry, and report health.
All cron/hook entry points are resolved to a PATH-independent invocation so they
survive cron's minimal environment.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import (
    Collection,
    ConfigError,
    example_template_text,
    expand,
    get_config,
    load_config_from,
    user_config_path,
)
from .qmd import list_collections
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
    """Machine-readable summary of one collection's sync policy + store/sources."""
    view = {
        "name": name,
        "enabled": coll.sync.enabled,
        "since": coll.sync.since,
        "on_hook": coll.sync.on_hook,
        "store": str(coll.store) if coll.store else None,
        # service-docs is the store-less collection; sessions all have a store.
        "type": "service-docs" if coll.store is None else "sessions",
    }
    if coll.service_sources:
        view["service_sources"] = {s.name: str(s.path) for s in coll.service_sources}
    return view


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
        for src_name, src_path in (c.get("service_sources") or {}).items():
            print(f"      docs[{src_name}] = {src_path}")
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


def _edit_config(apply) -> int:
    """Read the in-effect config, apply ``apply(data)``, write it back, validate, restore.

    ``apply`` mutates the parsed JSON in place and returns the success message to print;
    it may raise ``ValueError`` to reject the edit with a message. If the written file
    fails to load we put the original back (§8: never leave a half-valid config behind -
    fail loud and undo). Shared by ``set`` / ``add-service-doc`` / ``remove-service-doc``.
    """
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
        message = apply(data)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1

    target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    try:
        load_config_from(target)
    except ConfigError as e:
        target.write_text(original, encoding="utf-8")
        print(f"Rejected: that change made the config invalid ({e}). Restored.", file=sys.stderr)
        return 1
    get_config.cache_clear()  # so later reads in this process see the change
    print(message)
    return 0


def _config_set(key: str, value: str) -> int:
    """Write one config value to the in-effect config file, failing loud if it invalidates it."""

    def apply(data: dict) -> str:
        try:
            _set_dotted(data, key, value)
        except ValueError as e:
            raise ValueError(f"Cannot set {key!r}: {e}") from e
        return f"Set {key} = {value}"

    return _edit_config(apply)


def _template_service_sources() -> dict:
    """The bundled template's placeholder ``service_sources`` (used to detect 'untouched')."""
    data = json.loads(example_template_text())
    return data.get("collections", {}).get("service-docs", {}).get("service_sources", {})


def _config_add_service_doc(name: str, path: str, description: str | None) -> int:
    """Add (or update) one ``service-docs`` source folder. Validates the path exists.

    Service docs are configured by *folder*, not by a single store, so they need their
    own command (``cairn config set`` only handles scalar collection fields). The first
    real add clears the bundled placeholder so a fresh config doesn't keep mirroring a
    non-existent ``~/Code/your-product/...`` path.
    """
    expanded = expand(path)
    if not expanded.is_dir():
        print(f"Path not found or not a directory: {expanded}", file=sys.stderr)
        return 1
    desc = description or f"{name} service docs"

    def apply(data: dict) -> str:
        spec = data.get("collections", {}).get("service-docs")
        if not isinstance(spec, dict):
            raise ValueError("no 'service-docs' collection in config")
        sources = spec.get("service_sources")
        if not isinstance(sources, dict) or sources == _template_service_sources():
            sources = {}  # drop the untouched placeholder on first real add
        sources[name] = {"path": path, "description": desc}
        spec["service_sources"] = sources
        return f"Added service doc source {name!r} -> {path}"

    rc = _edit_config(apply)
    if rc == 0:
        coll = get_config().collections.get("service-docs")
        if coll is not None and not coll.sync.enabled:
            print("note: service-docs is disabled; enable it with")
            print("  cairn config set service-docs.enabled true")
    return rc


def _config_remove_service_doc(name: str) -> int:
    """Remove one ``service-docs`` source folder by name."""

    def apply(data: dict) -> str:
        spec = data.get("collections", {}).get("service-docs")
        if not isinstance(spec, dict):
            raise ValueError("no 'service-docs' collection in config")
        sources = spec.get("service_sources")
        if not isinstance(sources, dict) or name not in sources:
            known = ", ".join(sorted(sources)) if isinstance(sources, dict) else "(none)"
            raise ValueError(f"unknown service doc {name!r}; known: {known}")
        del sources[name]
        return f"Removed service doc source {name!r}"

    return _edit_config(apply)


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
    p_add = sub.add_parser(
        "add-service-doc", help="add a docs folder to the service-docs collection"
    )
    p_add.add_argument("name", metavar="NAME", help="service name (becomes the subfolder)")
    p_add.add_argument("path", metavar="PATH", help="path to the docs folder to mirror")
    p_add.add_argument(
        "-d", "--description", metavar="TEXT", help="QMD context description for this service"
    )
    p_rm = sub.add_parser(
        "remove-service-doc", help="remove a docs folder from the service-docs collection"
    )
    p_rm.add_argument("name", metavar="NAME", help="service name to remove")
    args = parser.parse_args(argv)

    if args.action == "init":
        enable = args.enable.split(",") if args.enable is not None else None
        return _config_init(data_root=args.data_root, enable=enable)
    if args.action == "show":
        return _config_show(args.json)
    if args.action == "set":
        return _config_set(args.key, args.value)
    if args.action == "add-service-doc":
        return _config_add_service_doc(args.name, args.path, args.description)
    if args.action == "remove-service-doc":
        return _config_remove_service_doc(args.name)
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


def _qmd_vector_count(qmd_binary: str) -> int | None:
    """The global embedded-vector count from ``qmd status`` (``None`` if unreadable).

    ``qmd status`` reports a single global ``Vectors: N embedded`` line (there is no
    per-collection vector count), so this is necessarily an index-wide health check.
    """
    try:
        result = subprocess.run([qmd_binary, "status"], capture_output=True, text=True)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    m = re.search(r"Vectors:\s*([\d,]+)", result.stdout)
    return int(m.group(1).replace(",", "")) if m else None


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

    # qmd collections registered (an unregistered dir is invisible to search).
    # warn, never fail: a fresh install legitimately has nothing registered until
    # the first 'cairn sync' runs - which now registers them automatically.
    if config is not None:
        try:
            registered = list_collections(config.qmd_binary)
        except (RuntimeError, OSError) as e:
            registered = None
            _warn("qmd collections", f"could not list: {e}")
        if registered is not None:
            for name, coll in sorted(config.collections.items()):
                if not coll.sync.enabled:
                    continue
                out = config.output_dir(name)
                if not out.is_dir() or next(out.rglob("*.md"), None) is None:
                    continue  # nothing exported yet -> nothing to register
                if name in registered:
                    _ok(f"collection [{name}]", "registered in qmd")
                else:
                    _warn(
                        f"collection [{name}]",
                        "has markdown but is not a qmd collection; run 'cairn sync'",
                    )

    # embeddings present (an index with 0 vectors returns no search results).
    # warn, never fail: zero is the expected state before the first embed.
    if config is not None:
        vectors = _qmd_vector_count(config.qmd_binary)
        if vectors is None:
            _warn("embeddings", "could not read 'qmd status'")
        elif vectors > 0:
            _ok("embeddings", f"{vectors} vectors embedded")
        else:
            _warn("embeddings", "0 vectors - run 'cairn sync --cron' to embed")

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
