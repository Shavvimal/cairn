"""Admin commands behind the ``cairn`` dispatcher: ``config``, ``cron``, ``doctor``.

These are the side-effectful / introspection commands the ``/setup`` skill drives:
write the per-user config, manage the hourly sync crontab entry, and report health.
All cron/hook entry points are resolved to a PATH-independent invocation so they
survive cron's minimal environment.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, fields
from pathlib import Path

from . import __version__
from .config import (
    BILLING_PEERS,
    INVOKE_PEERS,
    PROFILE_PEERS,
    ROUTINE_REQUIRED,
    BillingPolicy,
    CairnConfig,
    Collection,
    ConfigError,
    PeerPolicy,
    RoutineSpec,
    example_template_text,
    expand,
    get_config,
    load_config_from,
    routine_paths,
    user_config_path,
)
from .qmd import (
    TESTED_QMD_VERSION,
    IndexStatus,
    StatusFormatError,
    index_status,
    list_collections,
    parse_version,
    qmd_version,
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
        # A routine may only refresh enabled sources: drop the ones just disabled so
        # the template's example routine still loads.
        _drop_routine_sources(data, lambda source: source not in wanted)
    return json.dumps(data, indent=2) + "\n"


def _drop_routine_sources(data: dict, drop: Callable[[str], bool]) -> list[str]:
    """Remove every source ``drop`` selects from each routine's ``sources``, in place.

    A routine may only name an enabled collection, so disabling one (``config init``
    choices, ``config set <collection>.enabled false``) drops it here first. Returns one
    note per routine changed.
    """
    notes: list[str] = []
    routines = data.get("routines")
    for name, routine in routines.items() if isinstance(routines, dict) else ():
        if not isinstance(routine, dict) or not isinstance(routine.get("sources"), list):
            continue
        dropped = [s for s in routine["sources"] if drop(s)]
        if dropped:
            routine["sources"] = [s for s in routine["sources"] if not drop(s)]
            notes.append(
                f"note: removed {', '.join(dropped)} from routines.{name}.sources "
                "(a routine may only refresh an enabled collection)"
            )
    return notes


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
        # Sessions all have a store; a store-less collection is service-docs or plain
        # notes (for example the journal a routine writes).
        "type": (
            "service-docs"
            if coll.service_sources or name == "service-docs"
            else "notes"
            if coll.store is None
            else "sessions"
        ),
    }
    if coll.service_sources:
        view["service_sources"] = {s.name: str(s.path) for s in coll.service_sources}
    return view


def _invoke_view(cfg: CairnConfig) -> dict:
    """Machine-readable ``invoke`` block: one policy dict per peer."""
    return {peer: asdict(cfg.invoke.for_peer(peer)) for peer in INVOKE_PEERS}


def _routine_view(cfg: CairnConfig, spec: RoutineSpec) -> dict:
    """One routine for ``config show --json``: the fields as written plus ``resolved``."""
    resolved: dict = {
        "profile": spec.profile,
        "prompt_file": str(spec.prompt_file),
        "cd": str(spec.cd),
        "outputs": {key: str(path) for key, path in spec.outputs.items()},
    }
    resolved.update({key: str(value) for key, value in routine_paths(cfg, spec.name).items()})
    return {**spec.raw, "resolved": resolved}


def _peer_summary(policy: PeerPolicy) -> str:
    """One-line human summary of a peer's invoke defaults."""
    text = (
        f"model={policy.model or '(peer default)'} effort={policy.effort or '(peer default)'} "
        f"profile={policy.profile} timeout={policy.timeout_s}s idle={policy.idle_timeout_s}s "
        f"quiet={policy.quiet_after_s}s stall={policy.stall_after_s}s"
    )
    if policy.billing is not None:
        text += f" billing={policy.billing.route}"
        if policy.billing.project:
            text += f" project={policy.billing.project}"
    return text


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
                    "invoke": _invoke_view(cfg),
                    "routines": {
                        name: _routine_view(cfg, spec) for name, spec in cfg.routines.items()
                    },
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
    print("invoke      :")
    for peer in INVOKE_PEERS:
        policy = cfg.invoke.for_peer(peer)
        print(f"  {peer:22} {_peer_summary(policy)}")
        for name, profile in sorted(policy.profiles.items()):
            extra = f" + {' '.join(profile.extra_args)}" if profile.extra_args else ""
            print(f"  {peer} profile {name} = base {profile.base}{extra}")
    if cfg.routines:
        print("routines    :")
    for name, spec in cfg.routines.items():
        sources = ",".join(spec.sources) or "(none)"
        print(
            f"  {name:22} {spec.peer}/{spec.profile} at {spec.at} local, "
            f"catch-up {spec.catch_up_days}d, sources {sources}"
        )
        print(f"      prompt_file = {spec.prompt_file}")
        print(f"      cd          = {spec.cd}")
        for key, path in spec.outputs.items():
            print(f"      outputs.{key} = {path}")
    return 0


# Friendly dotted keys -> where they live in the JSON. Collection fields accept either
# the collection name (claude-code-sessions) or its source alias (claude) as the prefix.
_TOP_LEVEL_KEYS = {"data_root", "qmd_binary"}
_COLLECTION_SYNC_FIELDS = {"enabled", "since", "on_hook"}
_BOOL_FIELDS = {"enabled", "on_hook"}
_INVOKE_FIELDS = {f.name for f in fields(PeerPolicy)} - {"billing", "profiles"}
_INVOKE_INT_FIELDS = {"timeout_s", "idle_timeout_s", "quiet_after_s", "stall_after_s"}
_INVOKE_NULLABLE = {"model", "effort"}
_BILLING_FIELDS = {f.name for f in fields(BillingPolicy)}
_BILLING_NULLABLE = {"project", "location"}
_BILLING_KEY_HINT = "invoke.antigravity.billing.route|project|location"


def _coerce_invoke(field: str, value: str) -> object:
    """Coerce a CLI string for ``invoke.<peer>.<field>``: ints for clocks, null for defaults."""
    if field in _INVOKE_INT_FIELDS:
        try:
            return int(value)
        except ValueError:
            raise ValueError(
                f"{field} expects an integer number of seconds, got {value!r}"
            ) from None
    if field in _INVOKE_NULLABLE and value.strip().lower() in ("null", "none"):
        return None
    return value


def _coerce(field: str, value: str) -> object:
    """Coerce a CLI string to the field's type (bool for enable/on_hook flags)."""
    if field in _BOOL_FIELDS:
        low = value.strip().lower()
        if low not in ("true", "false"):
            raise ValueError(f"{field} expects true|false, got {value!r}")
        return low == "true"
    return value


_ROUTINE_SCALARS = (
    "peer",
    "prompt_file",
    "cd",
    "profile",
    "model",
    "at",
    "day",
    "catch_up_days",
    "timeout_s",
    "budget_usd_per_day",
)
_ROUTINE_ARGV_FIELDS = ("prepare", "validate")
_ROUTINE_SET_FIELDS = (*_ROUTINE_SCALARS, "sources", *_ROUTINE_ARGV_FIELDS)
_PROFILE_SET_FIELDS = ("base", "extra_args")
_ROUTINE_KEY_HINT = (
    "routines.<name> '{...}', routines.<name>.<field>, routines.<name>.outputs.<key>"
)
_PROFILE_KEY_HINT = "invoke.<claude|codex>.profiles.<name> '{...}' or .base|extra_args"


def _is_null(value: str) -> bool:
    return value.strip().lower() in ("null", "none")


def _json_object(key: str, value: str) -> dict:
    if not value.lstrip().startswith("{"):
        raise ValueError(f'{key} takes a JSON object, for example \'{{"base": "edit"}}\'')
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{key} is not valid JSON: {exc}") from None
    if not isinstance(parsed, dict):
        raise ValueError(f"{key} takes a JSON object")
    return parsed


def _json_strings(key: str, value: str) -> list[str]:
    """A JSON array of strings (an argv field accepts nothing else: an element may
    contain a comma)."""
    if not value.lstrip().startswith("["):
        raise ValueError(
            f"{key} takes a JSON array of strings (an argv list), for example '[\"true\"]'"
        )
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{key} is not valid JSON: {exc}") from None
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError(f"{key} takes a JSON array of strings")
    return parsed


def _coerce_routine(key: str, field: str, value: str) -> object:
    """Coerce a CLI string for ``routines.<name>.<field>``."""
    if field in ("catch_up_days", "timeout_s"):
        if field == "timeout_s" and _is_null(value):
            return None
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"{field} expects an integer, got {value!r}") from None
    if field == "budget_usd_per_day":
        if _is_null(value):
            return None
        try:
            return float(value)
        except ValueError:
            raise ValueError(f"{field} expects a number or null, got {value!r}") from None
    if field in ("profile", "model") and _is_null(value):
        return None
    if field == "sources":
        if value.lstrip().startswith("["):
            return _json_strings(key, value)
        return [item.strip() for item in value.split(",") if item.strip()]
    if field == "prepare" and _is_null(value):
        return None
    if field in _ROUTINE_ARGV_FIELDS:
        return _json_strings(key, value)
    return value


def _missing_routine(name: str) -> ValueError:
    return ValueError(
        f"routine {name!r} does not exist; create it with: cairn config set routines.{name} "
        "'{\"peer\": ..., ...}'"
    )


def _set_routine(data: dict, key: str, parts: list[str], value: str) -> None:
    """``routines.<name>``, ``routines.<name>.<field>``, ``routines.<name>.outputs.<key>``."""
    routines = data.get("routines")
    if routines is None:
        routines = data["routines"] = {}
    if not isinstance(routines, dict):
        raise ValueError("'routines' in the config is not an object")
    name = parts[1]
    if len(parts) == 2:
        routines[name] = _json_object(key, value)
        return
    routine = routines.get(name)
    if not isinstance(routine, dict):
        raise _missing_routine(name)
    field = parts[2]
    if len(parts) == 4 and field == "outputs":
        outputs = routine.get("outputs")
        if not isinstance(outputs, dict):
            outputs = routine["outputs"] = {}
        if _is_null(value):
            outputs.pop(parts[3], None)
        else:
            outputs[parts[3]] = value
        return
    if len(parts) == 3 and field in _ROUTINE_SET_FIELDS:
        routine[field] = _coerce_routine(key, field, value)
        return
    known = ", ".join((*_ROUTINE_SET_FIELDS, "outputs.<key>"))
    raise ValueError(f"unknown routine field {'.'.join(parts[2:])!r}; known: {known}")


def _set_profile(data: dict, key: str, parts: list[str], value: str) -> None:
    """``invoke.<peer>.profiles.<name>`` and ``invoke.<peer>.profiles.<name>.<field>``."""
    peer, name = parts[1], parts[3]
    if peer not in PROFILE_PEERS:
        raise ValueError(f"profiles are {', '.join(PROFILE_PEERS)}-only; use {_PROFILE_KEY_HINT}")
    profiles = data.setdefault("invoke", {}).setdefault(peer, {}).setdefault("profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError(f"invoke.{peer}.profiles in the config is not an object")
    if len(parts) == 4:
        profiles[name] = _json_object(key, value)
        return
    profile = profiles.get(name)
    if not isinstance(profile, dict):
        raise ValueError(
            f"profile {name!r} does not exist; create it with: cairn config set "
            f'invoke.{peer}.profiles.{name} \'{{"base": "edit", "extra_args": []}}\''
        )
    field = parts[4]
    if field == "base":
        profile["base"] = value
    elif field == "extra_args":
        profile["extra_args"] = _json_strings(key, value)
    else:
        raise ValueError(f"unknown profile field {field!r}; known: base, extra_args")


def _set_dotted(data: dict, key: str, value: str) -> list[str]:
    """Apply ``key=value`` to the config dict in place. Raises ValueError on a bad key.

    Returns the notes to print about knock-on edits (a disabled collection dropped from
    the routines' ``sources``).
    """
    if key == "cron.schedule":
        data.setdefault("cron", {})["schedule"] = value
        return []
    if key in _TOP_LEVEL_KEYS:
        data[key] = value
        return []

    parts = key.split(".")
    if parts[0] == "routines" and 2 <= len(parts) <= 4:
        _set_routine(data, key, parts, value)
        return []
    if parts[0] == "invoke" and len(parts) in (4, 5) and parts[2] == "profiles":
        _set_profile(data, key, parts, value)
        return []
    if len(parts) == 4 and parts[0] == "invoke" and parts[2] == "billing":
        _, peer, _, field = parts
        if peer not in BILLING_PEERS:
            raise ValueError(
                f"billing is a {', '.join(BILLING_PEERS)}-only setting; use {_BILLING_KEY_HINT}"
            )
        if field not in _BILLING_FIELDS:
            raise ValueError(f"unknown billing field {field!r}; use {_BILLING_KEY_HINT}")
        billing = data.setdefault("invoke", {}).setdefault(peer, {}).setdefault("billing", {})
        null = field in _BILLING_NULLABLE and value.strip().lower() in ("null", "none")
        billing[field] = None if null else value
        return []
    if len(parts) == 3 and parts[0] == "invoke":
        _, peer, field = parts
        if peer not in INVOKE_PEERS:
            raise ValueError(f"unknown invoke peer {peer!r}; known: {', '.join(INVOKE_PEERS)}")
        if field == "billing":
            raise ValueError(f"billing is an object; set {_BILLING_KEY_HINT}")
        if field == "profiles":
            raise ValueError(f"profiles is an object of named profiles; set {_PROFILE_KEY_HINT}")
        if field not in _INVOKE_FIELDS:
            known = ", ".join(sorted(_INVOKE_FIELDS))
            raise ValueError(f"unknown invoke field {field!r}; known: {known}")
        invoke = data.setdefault("invoke", {})
        invoke.setdefault(peer, {})[field] = _coerce_invoke(field, value)
        return []
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
            return []
        if field in _COLLECTION_SYNC_FIELDS:
            coerced = _coerce(field, value)
            spec.setdefault("sync", {})[field] = coerced
            if field == "enabled" and coerced is False:
                return _drop_routine_sources(
                    data, lambda source: SOURCE_COLLECTIONS.get(source) == collection
                )
            return []

    raise ValueError(
        f"unsupported key {key!r}. Use data_root, qmd_binary, cron.schedule, "
        "<collection>.store|enabled|since|on_hook, invoke.<codex|claude|antigravity>."
        "model|effort|profile|timeout_s|idle_timeout_s|quiet_after_s|stall_after_s, "
        f"{_BILLING_KEY_HINT}, {_PROFILE_KEY_HINT}, or {_ROUTINE_KEY_HINT}"
    )


def _unset_dotted(data: dict, key: str) -> str:
    """Remove ``key`` from the config dict; returns the success message."""
    parts = key.split(".")
    if parts[0] == "invoke" and len(parts) == 4 and parts[2] == "profiles":
        profiles = ((data.get("invoke") or {}).get(parts[1]) or {}).get("profiles")
        if not isinstance(profiles, dict) or parts[3] not in profiles:
            raise ValueError(f"no profile {parts[3]!r} in invoke.{parts[1]}.profiles")
        del profiles[parts[3]]
        return f"Removed {key}"
    if parts[0] == "routines" and 2 <= len(parts) <= 4:
        routines = data.get("routines")
        name = parts[1]
        if not isinstance(routines, dict) or not isinstance(routines.get(name), dict):
            raise _missing_routine(name)
        if len(parts) == 2:
            del routines[name]
            return f"Removed {key}; its launchd job stays until 'cairn routine uninstall {name}'"
        routine = routines[name]
        field = parts[2]
        if len(parts) == 4 and field == "outputs":
            outputs = routine.get("outputs")
            if not isinstance(outputs, dict) or parts[3] not in outputs:
                raise ValueError(f"no output {parts[3]!r} in routines.{name}.outputs")
            del outputs[parts[3]]
            return f"Removed {key}"
        if len(parts) == 3:
            if field in ROUTINE_REQUIRED:
                raise ValueError(f"{key} is required; set it instead")
            if field not in routine:
                raise ValueError(f"{key} is not set")
            del routine[field]
            return f"Removed {key}"
    raise ValueError(
        f"unsupported key {key!r} for unset. Use routines.<name>, "
        "routines.<name>.<optional field>, routines.<name>.outputs.<key> or "
        "invoke.<claude|codex>.profiles.<name>"
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
            notes = _set_dotted(data, key, value)
        except ValueError as e:
            raise ValueError(f"Cannot set {key!r}: {e}") from e
        return "\n".join([f"Set {key} = {value}", *notes])

    return _edit_config(apply)


def _config_unset(key: str) -> int:
    """Remove one routine, routine field, output or profile, failing loud if it
    invalidates the config (a profile a routine still uses is restored)."""

    def apply(data: dict) -> str:
        try:
            return _unset_dotted(data, key)
        except ValueError as e:
            raise ValueError(f"Cannot unset {key!r}: {e}") from e

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
        help=(
            "data_root | qmd_binary | cron.schedule | <collection>.store|enabled|since|on_hook"
            f" | invoke.<codex|claude|antigravity>.<field> | {_BILLING_KEY_HINT}"
            f" | {_PROFILE_KEY_HINT} | {_ROUTINE_KEY_HINT} (objects and argv lists as JSON)"
        ),
    )
    p_set.add_argument("value", metavar="VALUE")
    p_unset = sub.add_parser(
        "unset", help="remove a routine, a routine field or output, or an invoke profile"
    )
    p_unset.add_argument(
        "key",
        metavar="KEY",
        help=(
            "routines.<name> | routines.<name>.<optional field> | "
            "routines.<name>.outputs.<key> | invoke.<claude|codex>.profiles.<name>"
        ),
    )
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
    if args.action == "unset":
        return _config_unset(args.key)
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
    from .runtime import background_environment

    config = get_config()
    environment = background_environment(config)
    argv = [
        "/usr/bin/env",
        *[f"{k}={v}" for k, v in environment.items()],
        *_cairn_invocation(),
        "sync",
        "--cron",
    ]
    logfile = _log_file()
    logfile.parent.mkdir(parents=True, exist_ok=True)
    command = f"{shlex.join(argv)} >> {shlex.quote(str(logfile))} 2>&1"
    # Cron interprets percent signs even inside shell quotes. A stable comment
    # identifies our entry even when the executable itself needs shell quoting.
    command = command.replace("%", r"\%")
    line = f"{config.cron_schedule} {command} # cairn sync"
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


def _check_qmd_runtime(qmd_binary: str) -> tuple[int, IndexStatus | None, str | None]:
    """Probe the configured QMD: version (diagnostic) and ``qmd status`` (runtime).

    Returns ``(critical_failures, status, reason)``; ``reason`` says why ``status``
    is ``None``. The embeddings check reuses this one status result. Doctor never
    embeds, installs or upgrades anything.
    """
    tested = ".".join(map(str, TESTED_QMD_VERSION))
    try:
        raw = qmd_version(qmd_binary)
    except RuntimeError as e:
        _warn("qmd version", str(e))
    else:
        version = parse_version(raw)
        if version is None:
            _warn("qmd version", f"{raw!r} is not a release version; cairn is tested with {tested}")
        elif version < TESTED_QMD_VERSION:
            _warn(
                "qmd version",
                f"{raw}: older than {tested}, the release cairn's embedding checks were "
                "tested with; keyword search can still work. Read the QMD compatibility "
                "notes in the setup skill before changing it",
            )
        else:
            _ok("qmd version", raw)
    try:
        status = index_status(qmd_binary)
    except StatusFormatError as e:
        _warn("qmd runtime", f"ran, but {e}")
        return 0, None, str(e)
    except RuntimeError as e:
        _fail("qmd runtime", f"{e} - QMD cannot open its index, so search cannot work")
        return 1, None, "the QMD runtime probe failed"
    _ok("qmd runtime", "'qmd status' succeeded")
    return 0, status, None


def _check_embeddings(
    status: IndexStatus | None, reason: str | None, strict: bool
) -> tuple[int, str | None]:
    """Report index/embedding state; return ``(critical_failures, caveat)``.

    Default mode only warns about incomplete or unverified indexing, and returns a
    caveat for the summary line. ``--strict-index`` turns those into failures,
    except an empty index, which cannot be verified either way.
    """
    if status is None:
        caveat = f"could not read 'qmd status': {reason}"
        (_fail if strict else _warn)("embeddings", caveat)
        return int(strict), caveat
    counts = status.describe()
    if status.state == "empty":
        caveat = "the index is empty, so no search can be verified yet"
        _warn("embeddings", f"{caveat}; run 'cairn sync --cron'")
        return 0, caveat
    if status.state == "no-vectors":
        caveat = f"{counts}: keyword search can work, semantic search is not ready"
        (_fail if strict else _warn)("embeddings", f"{caveat}; run 'cairn sync --cron'")
        return int(strict), caveat
    if status.state == "pending":
        caveat = f"{counts}: semantic search misses the pending documents"
        (_fail if strict else _warn)(
            "embeddings",
            f"{caveat}; the next 'cairn sync --cron' continues. If this count does not fall "
            "across runs, investigate instead of retrying",
        )
        return int(strict), caveat
    _ok("embeddings", counts)
    return 0, None


def _parse_semver(value: str) -> tuple[int, int, int] | None:
    """Parse ``X.Y.Z`` into a comparable tuple, or ``None`` if it isn't plain semver."""
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value.strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def _plugin_manifest_path(explicit: str | None, harness: str | None = None) -> Path | None:
    """Locate the installed plugin's ``plugin.json``: ``--plugin-manifest`` else the
    plugin-context env var (``CLAUDE_PLUGIN_ROOT``); ``None`` when neither is available."""
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    from .harnesses import detect_harness, get_harness

    harness = harness or detect_harness()
    if harness is None:
        return None
    root = os.environ.get("PLUGIN_ROOT") or os.environ.get("CLAUDE_PLUGIN_ROOT")
    if root:
        for relative in get_harness(harness).manifest_paths:
            path = Path(root).expanduser() / relative
            if path.is_file():
                return path
    return None


def _check_plugin_drift(manifest_path: Path, harness: str = "claude") -> None:
    """Warn when the installed engine version differs from the plugin version.

    The plugin (skills + hook) auto-updates; the ``cairn`` engine is upgraded by hand,
    so the two can drift. We read the plugin's declared version and compare it to this
    engine's ``__version__``. Warn-only - never a critical failure.
    """
    from .harnesses import get_harness

    try:
        plugin_v = json.loads(manifest_path.read_text(encoding="utf-8")).get("version")
    except (OSError, json.JSONDecodeError):
        return
    if not plugin_v:
        return  # version-less manifest (commit-SHA mode) - nothing to compare
    pv, ev = _parse_semver(str(plugin_v)), _parse_semver(__version__)
    if pv is None or ev is None:
        return
    if pv == ev:
        _ok("plugin/engine", f"in sync ({__version__})")
    elif pv > ev:
        _warn(
            "plugin/engine drift",
            f"plugin v{plugin_v} is newer than engine v{__version__} - run 'uv tool upgrade cairn'",
        )
    else:
        _warn(
            "plugin/engine drift",
            f"engine v{__version__} is newer than plugin v{plugin_v} - "
            f"run {get_harness(harness).update_command}",
        )


def _check_rg() -> None:
    """ripgrep on PATH - the exact-match engine for the /search and /recall skills.

    Warn-only: the CLI and qmd search work without it; the skills fall back to BM25.
    """
    rg_path = shutil.which("rg")
    if rg_path:
        _ok("rg", rg_path)
    else:
        _warn(
            "rg",
            "not found - exact-match search in /search and /recall is off "
            "(install ripgrep, e.g. brew install ripgrep)",
        )


def _check_invoke(config: CairnConfig) -> None:
    """Report the ``cairn invoke`` defaults and whether Codex's live model catalogue exists.

    Warn-only: invoke is optional. Without ``$CODEX_HOME/models_cache.json`` the
    Codex self-check refuses to run (no static model list to fall back on).
    """
    from .harnesses.codex_models import models_cache_path

    summary = "; ".join(
        f"{peer}: {_peer_summary(config.invoke.for_peer(peer))}" for peer in INVOKE_PEERS
    )
    _ok("invoke defaults", summary)
    _check_antigravity_billing(config)
    _check_antigravity_permissions(config)
    if shutil.which("codex") and not models_cache_path().is_file():
        _warn(
            "codex model cache",
            f"{models_cache_path()} not found - run 'codex' once so 'cairn invoke codex' "
            "can check models (or pass --skip-self-check)",
        )


def _check_routines(config: CairnConfig) -> None:
    """Warn-only routines section: per routine its launchd job, last run, window stamps
    and journal location."""
    from .routine import doctor_checks

    for level, label, detail in doctor_checks(config):
        (_ok if level == "ok" else _warn)(label, detail)


def _check_antigravity_billing(config: CairnConfig) -> None:
    """Report the Antigravity billing route and, for ``gcp``, whether the ADC file and
    quota project it needs are in place (the same check ``cairn invoke`` runs before a
    spawn). Warn-only; the route is optional."""
    from .harnesses.antigravity import (
        ADC_AUTH_ENV,
        adc_token_check,
        billing_problems,
        resolved_project,
    )

    billing = config.invoke.antigravity.billing
    if billing is None:
        return
    if billing.route == "gcp":
        problems = billing_problems(billing, os.environ)
        if problems:
            _warn("antigravity billing", f"route gcp is not ready: {problems[0]}")
            return
        detail = f"route gcp via ADC, quota project {resolved_project(billing, os.environ)}"
        verdict, reason = adc_token_check(os.environ)
        if verdict == "ok":
            _ok("antigravity billing", f"{detail}, token ok")
        elif verdict == "refused":
            _warn("antigravity billing", f"route gcp is not ready: {reason}")
        else:
            _warn("antigravity billing", f"{detail}; token not verified: {reason}")
        return
    host = f"; the host shell exports {ADC_AUTH_ENV}" if os.environ.get(ADC_AUTH_ENV) else ""
    _ok("antigravity billing", f"route {billing.route}{host}")


def _check_antigravity_permissions(config: CairnConfig) -> None:
    """Report the rules headless agy applies: warn when no shell command may run at
    all (every ``command`` call would be auto-denied), else list both rule sets so the
    operator can see what a peer may and may not do before a run."""
    from .harnesses.antigravity_registration import permission_report, permission_warning

    if not shutil.which("agy"):
        return
    warning = permission_warning(config)
    if warning:
        _warn("antigravity permissions", warning)
        return
    report = permission_report(config)
    _ok(
        "antigravity permissions",
        f"{len(report['allow'])} allow and {len(report['deny'])} deny rules in {report['path']}",
    )
    print(f"       allow: {', '.join(report['allow']) or '(none)'}")
    print(f"       deny:  {', '.join(report['deny']) or '(none)'}")


def _check_exporters(config: CairnConfig) -> None:
    """Warn-only exporter lines: each enabled adapter's spec target."""
    from .registry import BY_NAME
    from .sources.spec_targets import SPEC_TARGETS

    for source, target in SPEC_TARGETS.items():
        coll = config.collections.get(BY_NAME[source].collection)
        if coll is None or not coll.sync.enabled:
            continue
        _ok(f"exporter [{source}]", f"adapter {target.adapter_version}, {target.describe()}")


def doctor_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cairn doctor", description="Check the cairn install")
    parser.add_argument(
        "--plugin-manifest",
        metavar="PATH",
        help="path to the plugin's plugin.json (else $CLAUDE_PLUGIN_ROOT) for a drift check",
    )
    parser.add_argument("--harness", choices=("claude", "codex", "antigravity"))
    parser.add_argument(
        "--strict-index",
        action="store_true",
        help="fail unless the QMD index is readable and fully embedded (setup/acceptance)",
    )
    args = parser.parse_args(argv)
    print(f"cairn {__version__} - health check\n")
    critical_failures = 0
    index_caveat: str | None = None
    qmd_status: IndexStatus | None = None
    status_reason: str | None = "QMD was not probed"

    cairn_bin = shutil.which("cairn")
    if cairn_bin:
        _ok("cairn on PATH", cairn_bin)
    else:
        _warn("cairn on PATH", f"not found; falling back to {sys.executable} -m cairn")

    # plugin/engine version drift (only when we can see the plugin manifest)
    manifest = _plugin_manifest_path(args.plugin_manifest, args.harness)
    if manifest is not None:
        inferred = "codex" if manifest.parent.name != ".claude-plugin" else "claude"
        _check_plugin_drift(manifest, args.harness or inferred)

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
            failures, qmd_status, status_reason = _check_qmd_runtime(qmd)
            critical_failures += failures
        else:
            _fail("qmd", f"{qmd!r} not found - install it (e.g. npm install -g @tobilu/qmd)")
            critical_failures += 1

    # rg (optional exact-match engine for the skills)
    _check_rg()

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

    if config is not None:
        _check_invoke(config)
        _check_routines(config)
        _check_exporters(config)

    # embeddings: index-wide counts from the single 'qmd status' probe above.
    # Zero vectors leaves keyword (BM25) search working; only semantic search waits.
    if config is not None:
        failures, index_caveat = _check_embeddings(qmd_status, status_reason, args.strict_index)
        critical_failures += failures

    if config is not None:
        from .hooks import iso, launcher_path, status
        from .store import SessionStore

        report = status(config, args.harness)
        _ok("exported sessions", str(len(SessionStore(config).records())))
        if launcher_path().is_file():
            _ok("hook launcher", str(launcher_path()))
        else:
            _warn("hook launcher", "not configured; run cairn hook install")
        if args.harness == "codex":
            from .harnesses.codex import Codex

            try:
                loaded = Codex().inspect_hooks(os.getcwd())
                cairn_hooks = [
                    h
                    for h in loaded
                    if h.get("pluginId") == "cairn@cairn"
                    or "cairn hook handle" in h.get("command", "")
                    or "-m cairn hook" in h.get("command", "")
                ]
                if cairn_hooks:
                    for hook in cairn_hooks:
                        _ok(
                            "Codex hook registration",
                            f"{hook.get('source')}: enabled={hook.get('enabled')}, trust={hook.get('trustStatus')}",
                        )
                else:
                    _warn(
                        "Codex hook registration",
                        "none discovered; run cairn hook install --harness codex",
                    )
            except (OSError, ValueError, RuntimeError) as exc:
                _warn("hook inspection", str(exc))
        else:
            _warn("hook trust", report["trust"])
        _ok("observed sync states", json.dumps(report["counts"]))
        problems = [
            r for r in report["requests"] if r.get("state") in ("failed", "exhausted", "invalid")
        ]
        for request in problems[:5]:
            _warn("hook failure", request.get("error", "run cairn hook worker"))
        if len(problems) > 5:
            _warn("hook failure", f"+{len(problems) - 5} more; see cairn hook status")
        observed = [r for r in report["requests"] if r.get("queued_at")]
        if observed:
            latest = max(observed, key=lambda r: r["queued_at"])
            _ok(
                "last hook event",
                f"{latest['event']['harness']} {latest['event']['session_id']}: {latest['state']} at {iso(latest['queued_at'])}",
            )
        else:
            _warn(
                "hook execution",
                "not observed; end a real host session and check cairn hook status",
            )

    # cron
    if any(_CRON_TAG in ln for ln in _current_crontab()):
        _ok("cron", "hourly 'cairn sync --cron' entry present")
    else:
        _warn("cron", "no entry; run 'cairn cron install'")

    print()
    if critical_failures:
        print(f"{critical_failures} critical check(s) failed.")
        return 1
    if index_caveat:
        print(
            f"All critical checks passed, but the semantic index is not verified: {index_caveat}."
        )
    else:
        print("All critical checks passed.")
    return 0


__all__ = ["config_main", "cron_main", "doctor_main"]
