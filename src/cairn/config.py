"""Machine-specific configuration, loaded from JSON (config-as-data).

Everything that varies by machine, user, or deployment - filesystem roots, the
project/repo catalog, native agent-store locations, the QMD binary - lives in a
JSON file, never in code. That is what makes the engine portable: another person
clones the repo, points one config file at their machine, and every skill works.

Resolution order for the config file (decoupled from where the code lives, so the
package can be installed globally and still find a per-user config):

1. ``$CAIRN_CONFIG`` - absolute path, if set (tests, CI, non-standard installs).
2. ``$XDG_CONFIG_HOME/cairn/config.json`` (else ``~/.config/cairn/config.json``) -
   the per-user config, written by ``cairn config init``.
3. The repo root (``cairn.config.json`` then ``cairn.config.example.json``) - the
   legacy/dev fallback so an editable ``pip install -e .`` checkout works unchanged.

A missing file or a missing required key raises :class:`ConfigError`. We never fall
back to built-in path defaults: a silent default would write one person's data into
another's directory layout. Fail loudly, fix the config - see CORE_PRINCIPLES §8.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from functools import lru_cache
from importlib import resources
from pathlib import Path

CONFIG_FILENAME = "cairn.config.json"
EXAMPLE_FILENAME = "cairn.config.example.json"
ENV_OVERRIDE = "CAIRN_CONFIG"

# Per-user config under XDG. The ``cairn/`` dir already namespaces us, so the file
# is just ``config.json``. The template ships inside the package as data.
XDG_APP_DIR = "cairn"
USER_CONFIG_BASENAME = "config.json"
PACKAGED_EXAMPLE = "config.example.json"

_REQUIRED_KEYS = (
    "data_root",
    "collections",
    "project_groups",
    "repo_catalog",
    "project_descriptions",
)

# Default sync cadence when the config omits ``cron.schedule``: hourly at minute 7.
DEFAULT_CRON_SCHEDULE = "7 * * * *"

# The shared permission profiles for ``cairn invoke``. Each harness adapter maps a
# profile to its own exact native flags; the names here are the only valid values.
PROFILES = ("review", "edit", "full")


class ConfigError(RuntimeError):
    """Configuration is missing or invalid. Deliberately never swallowed."""


# ---------------------------------------------------------------------------
# Path / slug helpers
# ---------------------------------------------------------------------------


def expand(value: str) -> Path:
    """Expand ``~`` and ``$VARS`` in a path string into a concrete :class:`Path`."""
    return Path(os.path.expandvars(os.path.expanduser(value)))


def claude_slug(path: Path | str) -> str:
    """Encode a filesystem path the way Claude Code names its project dirs.

    Claude replaces ``/`` and ``.`` with ``-`` - so ``/Users/x/Code`` becomes
    ``-Users-x-Code`` and ``/Users/x/.superset`` becomes ``-Users-x--superset``.
    """
    return str(path).replace("/", "-").replace(".", "-")


def _derive_slug_prefixes(home: Path, code_prefix: Path) -> tuple[str, ...]:
    """Default Claude-dir prefixes to strip, derived from the machine's home.

    Longest first so prefix matching is greedy: the code root, then home-with-dot
    (``~/.hidden`` dirs), then bare home.
    """
    return (
        f"{claude_slug(code_prefix)}-",  # -Users-x-Code-
        f"{claude_slug(home)}--",  # -Users-x--   (home + /.hidden)
        f"{claude_slug(home)}-",  # -Users-x-
    )


# ---------------------------------------------------------------------------
# Typed config view
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceSource:
    """One documentation source for the ``service-docs`` collection."""

    name: str
    path: Path
    description: str


@dataclass(frozen=True)
class SyncPolicy:
    """How ``cairn sync`` should treat a collection - all of it user data.

    This is the "which integrations are enabled" choice the user makes at setup,
    recorded in the config file and *consumed* by :mod:`cairn.sync`. ``enabled``
    gates the source entirely; ``since`` is its export window (a ``parse_since``
    token); ``on_hook`` decides whether the fast SessionEnd hook runs it (``None``
    means enable the originating host's hook. Legacy ``sync --hook`` retains
    its per-source default, enabling Claude only).
    """

    enabled: bool = True
    since: str = "1d"
    on_hook: bool | None = None


@dataclass(frozen=True)
class Collection:
    """A synced collection: its native source store and QMD identity.

    ``name`` is simultaneously the QMD collection name and the output subdirectory
    under ``data_root``. ``store`` is where the source application keeps its native
    data (e.g. ``~/.claude/projects``). ``service_sources`` is populated only for
    ``service-docs``. ``extra`` holds source-specific scalars (e.g. an API base URL).
    ``sync`` is the data-driven enable/window/hook policy (see :class:`SyncPolicy`).
    """

    name: str
    store: Path | None = None
    service_sources: tuple[ServiceSource, ...] = ()
    extra: dict[str, str] = field(default_factory=dict)
    sync: SyncPolicy = field(default_factory=SyncPolicy)

    def require_store(self) -> Path:
        """The native source store, failing loudly if this collection has none.

        Session sources (claude/codex/cursor/granola) always configure a ``store``;
        calling this narrows ``Path | None`` to ``Path`` for the adapters and turns a
        misconfigured collection into a clear error instead of an ``AttributeError``.
        """
        if self.store is None:
            raise ConfigError(f"Collection {self.name!r} has no 'store' configured.")
        return self.store


@dataclass(frozen=True)
class PeerPolicy:
    """Defaults for ``cairn invoke <peer>``: one peer agent's model, profile and clocks.

    ``model``/``effort`` ``None`` lets the peer CLI choose. ``profile`` is one of
    :data:`PROFILES`. All durations are in seconds: ``timeout_s`` is the wall clock,
    ``idle_timeout_s`` kills a run with no activity, and ``quiet_after_s`` /
    ``stall_after_s`` are the activity thresholds ``cairn invoke status`` reports.
    """

    model: str | None = None
    effort: str | None = None
    profile: str = "review"
    timeout_s: int = 1800
    idle_timeout_s: int = 900
    quiet_after_s: int = 120
    stall_after_s: int = 600


@dataclass(frozen=True)
class InvokePolicy:
    """The ``invoke`` block: one :class:`PeerPolicy` per supported peer."""

    codex: PeerPolicy = PeerPolicy()
    claude: PeerPolicy = PeerPolicy()

    def for_peer(self, peer: str) -> PeerPolicy:
        """The policy for ``peer``, failing loudly for a peer cairn cannot invoke."""
        if peer == "codex":
            return self.codex
        if peer == "claude":
            return self.claude
        raise ConfigError(f"No invoke policy for peer {peer!r}. Known: {', '.join(INVOKE_PEERS)}")


@dataclass(frozen=True)
class CairnConfig:
    """Read-only, validated view of ``cairn.config.json``."""

    repo_root: Path | None  # None when installed globally (config from XDG/$CAIRN_CONFIG)
    config_path: Path
    data_root: Path
    qmd_binary: str
    cron_schedule: str
    home: Path
    code_prefix: Path
    superset_prefix: Path
    home_slug_prefixes: tuple[str, ...]
    project_groups: dict[str, str]
    repo_catalog: dict[str, dict[str, str]]
    project_descriptions: dict[str, str]
    collections: dict[str, Collection]
    invoke: InvokePolicy = InvokePolicy()

    def output_dir(self, collection: str) -> Path:
        """Where a collection's exported markdown lives: ``data_root / <collection>``."""
        return self.data_root / collection

    def collection(self, name: str) -> Collection:
        """Look up a collection, failing loudly if it is not configured."""
        try:
            return self.collections[name]
        except KeyError:
            known = ", ".join(sorted(self.collections)) or "(none)"
            raise ConfigError(
                f"No collection {name!r} in {self.config_path}. Known: {known}"
            ) from None


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def find_repo_root(start: Path | None = None) -> Path | None:
    """Walk upward from ``start`` (default: this module) to the dir holding the config.

    The dev/editable fallback: looks for the real config first, then the committed
    example. Returns ``None`` when neither is found (e.g. an isolated global install),
    so the caller can fall through to the XDG path rather than crash.
    """
    here = (start or Path(__file__)).resolve()
    for parent in (here, *here.parents):
        if (parent / CONFIG_FILENAME).is_file() or (parent / EXAMPLE_FILENAME).is_file():
            return parent
    return None


def _xdg_config_home() -> Path:
    """``$XDG_CONFIG_HOME`` if set and non-empty, else ``~/.config``."""
    raw = os.environ.get("XDG_CONFIG_HOME")
    return Path(raw) if raw else Path.home() / ".config"


def user_config_path() -> Path:
    """The per-user config location: ``$XDG_CONFIG_HOME/cairn/config.json``."""
    return _xdg_config_home() / XDG_APP_DIR / USER_CONFIG_BASENAME


def example_template_text() -> str:
    """The bundled config template (shipped as package data)."""
    return resources.files("cairn").joinpath(PACKAGED_EXAMPLE).read_text(encoding="utf-8")


def _config_path() -> Path:
    override = os.environ.get(ENV_OVERRIDE)
    if override:
        path = Path(override).expanduser()
        if not path.is_file():
            raise ConfigError(f"{ENV_OVERRIDE}={override!r} is set but no such file exists.")
        return path

    user = user_config_path()
    if user.is_file():
        return user

    root = find_repo_root()
    if root is not None:
        real = root / CONFIG_FILENAME
        if real.is_file():
            return real
        example = root / EXAMPLE_FILENAME
        if example.is_file():
            return example

    raise ConfigError(
        f"No cairn config found (looked at ${ENV_OVERRIDE}, {user}, and the repo root). "
        f"Run 'cairn config init' to create {user}."
    )


_COLLECTION_KEYS = frozenset({"store", "sync", "extra", "service_sources"})


def _parse_collection(name: str, spec: dict) -> Collection:
    # ``//``-prefixed keys are the JSON-comment convention used in the example
    # config; ignore them. Any other unexpected key is rejected so the config can't
    # silently claim a knob the code never reads (e.g. a stale ``env_var``).
    unknown = {k for k in spec if not k.startswith("//")} - _COLLECTION_KEYS
    if unknown:
        raise ConfigError(
            f"Collection {name!r} has unknown keys: {', '.join(sorted(unknown))}. "
            f"Allowed keys: {', '.join(sorted(_COLLECTION_KEYS))}."
        )
    sources = tuple(
        ServiceSource(name=src_name, path=expand(src["path"]), description=src["description"])
        for src_name, src in (spec.get("service_sources") or {}).items()
    )
    store = spec.get("store")
    sync_spec = spec.get("sync") or {}
    sync = SyncPolicy(
        enabled=bool(sync_spec.get("enabled", True)),
        since=str(sync_spec.get("since", "1d")),
        on_hook=sync_spec.get("on_hook"),  # None -> engine per-source default
    )
    return Collection(
        name=name,
        store=expand(store) if store else None,
        service_sources=sources,
        extra=dict(spec.get("extra") or {}),
        sync=sync,
    )


INVOKE_PEERS = ("codex", "claude")
_PEER_FIELDS = tuple(f.name for f in fields(PeerPolicy))
_PEER_NULLABLE = frozenset({"model", "effort"})
_PEER_INT_FIELDS = frozenset({"timeout_s", "idle_timeout_s", "quiet_after_s", "stall_after_s"})


def _parse_peer(peer: str, spec: object) -> PeerPolicy:
    if not isinstance(spec, dict):
        raise ConfigError(f"invoke.{peer} must be an object, got {type(spec).__name__}")
    values = {k: v for k, v in spec.items() if not k.startswith("//")}
    unknown = set(values) - set(_PEER_FIELDS)
    if unknown:
        raise ConfigError(
            f"invoke.{peer} has unknown keys: {', '.join(sorted(unknown))}. "
            f"Allowed keys: {', '.join(_PEER_FIELDS)}."
        )
    for key in _PEER_NULLABLE & set(values):
        value = values[key]
        if value is not None and not (isinstance(value, str) and value.strip()):
            raise ConfigError(f"invoke.{peer}.{key} must be a non-empty string or null")
    for key in _PEER_INT_FIELDS & set(values):
        value = values[key]
        # bool is an int subclass; a ``true`` timeout is a typo, not one second.
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigError(f"invoke.{peer}.{key} must be a positive integer, got {value!r}")
    profile = values.get("profile", "review")
    if profile not in PROFILES:
        raise ConfigError(
            f"invoke.{peer}.profile must be one of {', '.join(PROFILES)}, got {profile!r}"
        )
    return PeerPolicy(**values)


def _parse_invoke(spec: object) -> InvokePolicy:
    """Parse the optional ``invoke`` block; every omitted peer or field takes its default."""
    if not isinstance(spec, dict):
        raise ConfigError(f"'invoke' must be an object, got {type(spec).__name__}")
    peers = {k: v for k, v in spec.items() if not k.startswith("//")}
    unknown = set(peers) - set(INVOKE_PEERS)
    if unknown:
        raise ConfigError(
            f"invoke has unknown peers: {', '.join(sorted(unknown))}. "
            f"Allowed peers: {', '.join(INVOKE_PEERS)}."
        )
    return InvokePolicy(**{peer: _parse_peer(peer, value) for peer, value in peers.items()})


def _build(data: dict, config_path: Path, repo_root: Path | None) -> CairnConfig:
    missing = [k for k in _REQUIRED_KEYS if k not in data]
    if missing:
        raise ConfigError(f"{config_path} is missing required keys: {', '.join(missing)}")

    home = Path.home()
    resolution = data.get("path_resolution", {})
    code_prefix = expand(resolution.get("code_prefix", str(home / "Code")))
    superset_prefix = expand(
        resolution.get("superset_prefix", str(home / ".superset" / "worktrees"))
    )
    slug_prefixes = tuple(
        resolution.get("home_slug_prefixes") or _derive_slug_prefixes(home, code_prefix)
    )

    collections = {
        name: _parse_collection(name, spec) for name, spec in data["collections"].items()
    }

    cron_schedule = str((data.get("cron") or {}).get("schedule", DEFAULT_CRON_SCHEDULE))
    raw_invoke = data.get("invoke")
    if raw_invoke is None:
        raw_invoke = {}
    if not isinstance(raw_invoke, dict):
        raise ConfigError(
            f"{config_path}: 'invoke' must be an object of per-peer settings, "
            f"got {type(raw_invoke).__name__}"
        )
    invoke = _parse_invoke(raw_invoke)

    return CairnConfig(
        repo_root=repo_root,
        config_path=config_path,
        data_root=expand(data["data_root"]),
        qmd_binary=data.get("qmd_binary", "qmd"),
        cron_schedule=cron_schedule,
        home=home,
        code_prefix=code_prefix,
        superset_prefix=superset_prefix,
        home_slug_prefixes=slug_prefixes,
        project_groups=dict(data["project_groups"]),
        repo_catalog={g: dict(repos) for g, repos in data["repo_catalog"].items()},
        project_descriptions=dict(data["project_descriptions"]),
        collections=collections,
        invoke=invoke,
    )


def load_config_from(path: Path) -> CairnConfig:
    """Load + validate a config from a specific file - no caching, no path resolution.

    Used to validate a freshly written config (``cairn config set``) against the exact
    file we just edited, independent of which path ``_config_path`` would resolve.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigError(f"Failed to read config at {path}: {e}") from e
    return _build(data, path, find_repo_root())


@lru_cache(maxsize=1)
def get_config() -> CairnConfig:
    """Load, validate, and cache the configuration (read once per process)."""
    return load_config_from(_config_path())


__all__ = [
    "CONFIG_FILENAME",
    "DEFAULT_CRON_SCHEDULE",
    "ENV_OVERRIDE",
    "EXAMPLE_FILENAME",
    "INVOKE_PEERS",
    "PROFILES",
    "CairnConfig",
    "Collection",
    "ConfigError",
    "InvokePolicy",
    "PeerPolicy",
    "ServiceSource",
    "SyncPolicy",
    "claude_slug",
    "example_template_text",
    "expand",
    "find_repo_root",
    "get_config",
    "load_config_from",
    "user_config_path",
]
