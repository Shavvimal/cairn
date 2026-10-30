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
import re
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

# Peers that accept config-defined profiles (``invoke.<peer>.profiles``), and the peers
# a routine may run. Antigravity has neither in v1.
PROFILE_PEERS = ("claude", "codex")
ROUTINE_PEERS = ("claude", "codex")

# A profile or routine name: lower-case, digits and '-', 1 to 64 characters.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
NAME_RULE = "use 1 to 64 characters from a-z, 0-9 and '-', starting with a letter or digit"

# A ``{{NAME}}`` template token (routine prompt files, argv lists and profile args).
TEMPLATE_TOKEN_RE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")


class ConfigError(RuntimeError):
    """Configuration is missing or invalid. Deliberately never swallowed.

    ``code`` names the failure in the error registry (``cairn errors CODE``).
    """

    def __init__(self, message: str = "", code: str = "CONFIG_INVALID") -> None:
        super().__init__(message)
        self.code = code


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


BILLING_ROUTES = ("inherit", "plan", "gcp")


@dataclass(frozen=True)
class BillingPolicy:
    """Which billing route ``cairn invoke antigravity`` runs ``agy`` under (antigravity only).

    ``inherit`` passes the host environment through unchanged: whatever that shell's
    ``agy`` would bill. ``plan`` strips ``AGY_ADC_AUTH`` so the run bills the consumer
    plan quota. ``gcp`` sets ``AGY_ADC_AUTH=true`` (Vertex AI through Application
    Default Credentials, billed to the GCP quota project) and refuses to spawn unless
    the ADC file and a quota project exist. ``project`` and ``location`` are exported
    as ``GOOGLE_CLOUD_QUOTA_PROJECT`` / ``GOOGLE_CLOUD_LOCATION`` when set; ``None``
    leaves the ADC file's ``quota_project_id`` and agy's default location in charge.
    """

    route: str = "inherit"
    project: str | None = None
    location: str | None = None


@dataclass(frozen=True)
class ProfileSpec:
    """A config-defined invoke profile: a built-in ``base`` plus native ``extra_args``.

    ``extra_args`` is an argv list emitted after the base's native flags (the per-peer
    merge and validation rules live in each harness module). It may hold ``{{NAME}}``
    tokens that only ``cairn routine run`` renders.
    """

    name: str
    base: str
    extra_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class PeerPolicy:
    """Defaults for ``cairn invoke <peer>``: one peer agent's model, profile and clocks.

    ``model``/``effort`` ``None`` lets the peer CLI choose. ``profile`` is one of
    :data:`PROFILES`. All durations are in seconds: ``timeout_s`` is the wall clock,
    ``idle_timeout_s`` kills a run with no activity, and ``quiet_after_s`` /
    ``stall_after_s`` are the activity thresholds ``cairn invoke status`` reports.
    ``billing`` is set for antigravity only (:class:`BillingPolicy`); ``None`` means the
    peer has no billing route to choose. ``profiles`` holds the config-defined profiles
    (claude and codex only); ``profile`` may name a built-in or one of them.
    """

    model: str | None = None
    effort: str | None = None
    profile: str = "review"
    timeout_s: int = 1800
    idle_timeout_s: int = 900
    quiet_after_s: int = 120
    stall_after_s: int = 600
    billing: BillingPolicy | None = None
    profiles: dict[str, ProfileSpec] = field(default_factory=dict)


@dataclass(frozen=True)
class InvokePolicy:
    """The ``invoke`` block: one :class:`PeerPolicy` per supported peer."""

    codex: PeerPolicy = PeerPolicy()
    claude: PeerPolicy = PeerPolicy()
    antigravity: PeerPolicy = PeerPolicy(billing=BillingPolicy())

    def for_peer(self, peer: str) -> PeerPolicy:
        """The policy for ``peer``, failing loudly for a peer cairn cannot invoke."""
        if peer == "codex":
            return self.codex
        if peer == "claude":
            return self.claude
        if peer == "antigravity":
            return self.antigravity
        raise ConfigError(f"No invoke policy for peer {peer!r}. Known: {', '.join(INVOKE_PEERS)}")


@dataclass(frozen=True)
class RoutineSpec:
    """One ``routines.<name>`` entry: a scheduled peer run (see docs/routine-spec.md).

    ``profile`` is the resolved profile name (the routine's own, else the peer's
    default). ``prompt_file``, ``cd`` and ``outputs`` hold expanded absolute paths.
    ``prepare`` and ``validate`` hold the raw template strings, rendered per day.
    ``raw`` keeps the fields as written in the config file, for ``config show``.
    """

    name: str
    peer: str
    prompt_file: Path
    cd: Path
    profile: str
    model: str | None
    at: str
    day: str
    sources: tuple[str, ...]
    catch_up_days: int
    timeout_s: int | None
    budget_usd_per_day: float | None
    outputs: dict[str, Path]
    prepare: tuple[str, ...] | None
    validate: tuple[str, ...]
    terminal_hook: tuple[str, ...] | None = None
    stale_sources: str = "warn"
    raw: dict = field(default_factory=dict, compare=False, repr=False)


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
    routines: dict[str, RoutineSpec] = field(default_factory=dict)

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
            raise ConfigError(
                f"{ENV_OVERRIDE}={override!r} is set but no such file exists.",
                code="CONFIG_NOT_FOUND",
            )
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
        f"Run 'cairn config init' to create {user}.",
        code="CONFIG_NOT_FOUND",
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
            f"Allowed keys: {', '.join(sorted(_COLLECTION_KEYS))}.",
            code="CONFIG_UNKNOWN_KEY",
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


INVOKE_PEERS = ("codex", "claude", "antigravity")
BILLING_PEERS = ("antigravity",)
_PEER_FIELDS = tuple(f.name for f in fields(PeerPolicy))
_PEER_NULLABLE = frozenset({"model", "effort"})
_PEER_INT_FIELDS = frozenset({"timeout_s", "idle_timeout_s", "quiet_after_s", "stall_after_s"})
_BILLING_FIELDS = tuple(f.name for f in fields(BillingPolicy))
_BILLING_NULLABLE = frozenset({"project", "location"})


def _parse_billing(peer: str, spec: object) -> BillingPolicy:
    if not isinstance(spec, dict):
        raise ConfigError(f"invoke.{peer}.billing must be an object, got {type(spec).__name__}")
    values = {k: v for k, v in spec.items() if not k.startswith("//")}
    unknown = set(values) - set(_BILLING_FIELDS)
    if unknown:
        raise ConfigError(
            f"invoke.{peer}.billing has unknown keys: {', '.join(sorted(unknown))}. "
            f"Allowed keys: {', '.join(_BILLING_FIELDS)}.",
            code="CONFIG_UNKNOWN_KEY",
        )
    route = values.get("route", "inherit")
    if route not in BILLING_ROUTES:
        raise ConfigError(
            f"invoke.{peer}.billing.route must be one of {', '.join(BILLING_ROUTES)}, got {route!r}"
        )
    for key in _BILLING_NULLABLE & set(values):
        value = values[key]
        if value is not None and not (isinstance(value, str) and value.strip()):
            raise ConfigError(f"invoke.{peer}.billing.{key} must be a non-empty string or null")
    return BillingPolicy(**values)


def _parse_peer(peer: str, spec: object) -> PeerPolicy:
    if not isinstance(spec, dict):
        raise ConfigError(f"invoke.{peer} must be an object, got {type(spec).__name__}")
    values = {k: v for k, v in spec.items() if not k.startswith("//")}
    if "billing" in values:
        if peer not in BILLING_PEERS:
            raise ConfigError(
                f"invoke.{peer}.billing is not supported; only "
                f"{', '.join(BILLING_PEERS)} selects a billing route"
            )
        values["billing"] = _parse_billing(peer, values["billing"])
    elif peer in BILLING_PEERS:
        values["billing"] = BillingPolicy()
    unknown = set(values) - set(_PEER_FIELDS)
    if unknown:
        raise ConfigError(
            f"invoke.{peer} has unknown keys: {', '.join(sorted(unknown))}. "
            f"Allowed keys: {', '.join(_PEER_FIELDS)}.",
            code="CONFIG_UNKNOWN_KEY",
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
    if "profiles" in values:
        values["profiles"] = _parse_profiles(peer, values["profiles"])
    policy = PeerPolicy(**values)
    try:
        resolve_profile(policy, policy.profile, peer=peer)
    except ConfigError:
        raise ConfigError(
            f"invoke.{peer}.profile must be one of {', '.join(profile_names(policy))}, "
            f"got {policy.profile!r}"
        ) from None
    return policy


# ---------------------------------------------------------------------------
# Config-defined invoke profiles
# ---------------------------------------------------------------------------

_PROFILE_KEYS = ("base", "extra_args")


def check_profile_args(peer: str, base: str, args: tuple[str, ...]) -> None:
    """Validate a profile's ``extra_args`` against ``peer``'s rules for ``base``.

    Raises ``ValueError`` with the reason text when a rule is broken.
    """
    if peer == "claude":
        from .harnesses.claude import check_profile_args as check
    elif peer == "codex":
        from .harnesses.codex import check_profile_args as check
    else:
        raise ValueError(f"{peer} takes no config profiles")
    check(base, args)


def _parse_profile(peer: str, name: str, spec: object) -> ProfileSpec:
    where = f"invoke.{peer}.profiles.{name}"
    if not isinstance(spec, dict):
        raise ConfigError(f"{where} must be an object, got {type(spec).__name__}")
    values = {k: v for k, v in spec.items() if not k.startswith("//")}
    unknown = set(values) - set(_PROFILE_KEYS)
    if unknown:
        raise ConfigError(
            f"{where} has unknown keys: {', '.join(sorted(unknown))}. "
            f"Allowed keys: {', '.join(_PROFILE_KEYS)}.",
            code="CONFIG_UNKNOWN_KEY",
        )
    base = values.get("base")
    if base not in PROFILES:
        raise ConfigError(f"{where}.base must be one of {', '.join(PROFILES)}, got {base!r}")
    extra = values.get("extra_args", [])
    if not isinstance(extra, list) or not all(isinstance(arg, str) for arg in extra):
        raise ConfigError(
            f"{where}.extra_args must be an array of strings (an argv list, never a string), "
            f"got {_describe(extra)}"
        )
    try:
        check_profile_args(peer, str(base), tuple(extra))
    except ValueError as exc:
        raise ConfigError(f"{where}.extra_args: {exc}") from None
    return ProfileSpec(name=name, base=str(base), extra_args=tuple(extra))


def _parse_profiles(peer: str, spec: object) -> dict[str, ProfileSpec]:
    if peer not in PROFILE_PEERS:
        raise ConfigError(
            f"invoke.{peer}.profiles is not supported; only "
            f"{', '.join(PROFILE_PEERS)} take config profiles"
        )
    if not isinstance(spec, dict):
        raise ConfigError(
            f"invoke.{peer}.profiles must be an object of named profiles, got {type(spec).__name__}"
        )
    profiles: dict[str, ProfileSpec] = {}
    for name, value in spec.items():
        if name.startswith("//"):
            continue
        if not NAME_RE.match(name):
            raise ConfigError(f"invoke.{peer}.profiles name {name!r} is invalid: {NAME_RULE}")
        if name in PROFILES:
            raise ConfigError(
                f"invoke.{peer}.profiles.{name} shadows the built-in profile {name!r}; "
                "pick another name"
            )
        profiles[name] = _parse_profile(peer, name, value)
    return profiles


def profile_names(policy: PeerPolicy) -> tuple[str, ...]:
    """Every profile name ``policy`` accepts: the built-ins, then its config profiles."""
    return (*PROFILES, *sorted(policy.profiles))


def resolve_profile(policy: PeerPolicy, name: str, *, peer: str) -> tuple[str, tuple[str, ...]]:
    """``(base, extra_args)`` for profile ``name``: a built-in has no extra args.

    Raises :class:`ConfigError` naming the choices when ``name`` is neither a built-in
    nor a key of ``policy.profiles``.
    """
    if name in PROFILES:
        return name, ()
    spec = policy.profiles.get(name) if isinstance(name, str) else None
    if spec is None:
        raise ConfigError(
            f"unknown profile {name!r} for {peer}; choose {', '.join(profile_names(policy))}"
        )
    return spec.base, spec.extra_args


def _describe(value: object) -> str:
    """A short type description for an error message (``list of int``, ``str``)."""
    if isinstance(value, list):
        if not value:
            return "an empty array"
        kinds = sorted({type(item).__name__ for item in value if not isinstance(item, str)})
        return f"an array holding {', '.join(kinds)}" if kinds else "list"
    return type(value).__name__


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


# ---------------------------------------------------------------------------
# Routines
# ---------------------------------------------------------------------------

_ROUTINE_FIELDS = (
    "at",
    "budget_usd_per_day",
    "catch_up_days",
    "cd",
    "day",
    "model",
    "outputs",
    "peer",
    "prepare",
    "profile",
    "prompt_file",
    "sources",
    "stale_sources",
    "terminal_hook",
    "timeout_s",
    "validate",
)
ROUTINE_REQUIRED = ("peer", "prompt_file", "cd", "at", "validate")
ROUTINE_DAYS = ("yesterday-utc",)
ROUTINE_STALE_SOURCES = ("warn", "refuse", "ignore")
ROUTINE_BUILTIN_VARIABLES = ("DAY", "DATA_ROOT", "ATTEMPT_DIR", "DIGEST_DIR")
DEFAULT_ROUTINE_OUTPUTS = {"journal_dir": "{{DATA_ROOT}}/journal"}
_AT_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
_OUTPUT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
ROUTINE_LABEL_PREFIX = "dev.cairn.routine."


def _windowed_sources() -> tuple[str, ...]:
    from .registry import SOURCES

    return tuple(sorted(s.source for s in SOURCES if s.session_type and s.windowed))


def expand_home(value: str) -> str:
    """Expand a bare ``~`` or a leading ``~/`` to the home directory, as a shell would."""
    if value == "~" or value.startswith("~/"):
        return str(Path.home()) + value[1:]
    return value


def expand_routine_path(value: str, data_root: Path) -> str:
    """Expand a routine path field: a leading ``~`` and every ``{{DATA_ROOT}}``.

    Nothing else: no ``$VARS`` (a launchd run has another environment than the shell
    that wrote the config), so a ``$`` stays literal.
    """
    return expand_home(value).replace("{{DATA_ROOT}}", str(data_root))


def _routine_path(where: str, value: object, data_root: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} must be a non-empty string, got {value!r}")
    for token in TEMPLATE_TOKEN_RE.findall(value):
        if token != "DATA_ROOT":
            raise ConfigError(f"{where} may only use {{{{DATA_ROOT}}}}, found {{{{{token}}}}}")
    expanded = expand_routine_path(value, data_root)
    if not Path(expanded).is_absolute():
        raise ConfigError(
            f"{where} must be absolute after expanding ~ and {{{{DATA_ROOT}}}}, got {expanded!r}"
        )
    return Path(expanded)


def _routine_argv(where: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(arg, str) for arg in value):
        raise ConfigError(
            f"{where} must be a non-empty array of strings (an argv list, never a shell "
            f"string), got {_describe(value)}"
        )
    return tuple(value)


def _terminal_hook(where: str, value: object) -> tuple[str, ...] | None:
    """``null``, or an argv list without template variables whose argv[0] is absolute.

    launchd runs the hook with the recorded ``PATH``, and a failed hook is only logged,
    so a bare command name would fail silently: argv[0] must be an absolute path.
    """
    if value is None:
        return None
    argv = _routine_argv(where, value)
    for arg in argv:
        for token in TEMPLATE_TOKEN_RE.findall(arg):
            raise ConfigError(
                f"{where} takes no template variables (the hook reads CAIRN_ROUTINE_* from "
                f"its environment), found {{{{{token}}}}}"
            )
    expanded = tuple(expand_home(arg) for arg in argv)
    if not Path(expanded[0]).is_absolute():
        raise ConfigError(
            f"{where}[0] must be an absolute path after expanding ~, got {expanded[0]!r}"
        )
    return expanded


def _routine_sources(
    where: str, value: object, collections: dict[str, Collection]
) -> tuple[str, ...]:
    from .registry import BY_NAME

    if not isinstance(value, list):
        raise ConfigError(f"{where} must be an array of source names, got {type(value).__name__}")
    windowed = _windowed_sources()
    for source in value:
        if source not in windowed:
            raise ConfigError(
                f"{where} names {source!r}, which is not a windowed session source; "
                f"choose {', '.join(windowed)}"
            )
        collection = BY_NAME[source].collection
        if collection not in collections:
            raise ConfigError(f"{where} names {source!r}, but collections has no {collection!r}")
        if not collections[collection].sync.enabled:
            raise ConfigError(
                f"{where} names {source!r}, whose collection {collection!r} is disabled "
                "(sync.enabled=false)"
            )
    return tuple(value)


def _routine_outputs(where: str, value: object, data_root: Path) -> dict[str, Path]:
    if not isinstance(value, dict):
        raise ConfigError(
            f"{where} must be an object of name -> directory, got {type(value).__name__}"
        )
    items = {k: v for k, v in value.items() if not k.startswith("//")}
    for key, default in DEFAULT_ROUTINE_OUTPUTS.items():
        items.setdefault(key, default)
    outputs: dict[str, Path] = {}
    for key, path in items.items():
        if not _OUTPUT_KEY_RE.match(key):
            raise ConfigError(
                f"{where} key {key!r} is invalid: use a-z, 0-9 and '_', starting with a letter"
            )
        if key.upper() in ROUTINE_BUILTIN_VARIABLES:
            raise ConfigError(
                f"{where} key {key!r} renders as {{{{{key.upper()}}}}}, which is a built-in "
                "variable"
            )
        outputs[key] = _routine_path(f"{where}.{key}", path, data_root)
    return outputs


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _parse_routine(
    name: str, spec: object, data_root: Path, invoke: InvokePolicy, collections: dict
) -> RoutineSpec:
    where = f"routines.{name}"
    if not isinstance(spec, dict):
        raise ConfigError(f"{where} must be an object, got {type(spec).__name__}")
    values = {k: v for k, v in spec.items() if not k.startswith("//")}
    unknown = set(values) - set(_ROUTINE_FIELDS)
    if unknown:
        raise ConfigError(
            f"{where} has unknown keys: {', '.join(sorted(unknown))}. "
            f"Allowed keys: {', '.join(_ROUTINE_FIELDS)}.",
            code="CONFIG_UNKNOWN_KEY",
        )
    missing = [key for key in ROUTINE_REQUIRED if key not in values]
    if missing:
        raise ConfigError(f"{where} is missing required keys: {', '.join(missing)}")
    peer = values["peer"]
    if peer not in ROUTINE_PEERS:
        raise ConfigError(f"{where}.peer must be one of {', '.join(ROUTINE_PEERS)}, got {peer!r}")
    policy = invoke.for_peer(peer)
    prompt_file = _routine_path(f"{where}.prompt_file", values["prompt_file"], data_root)
    cd = _routine_path(f"{where}.cd", values["cd"], data_root)

    profile = values.get("profile")
    if profile is None:
        profile = policy.profile
    try:
        resolve_profile(policy, profile, peer=peer)
    except ConfigError:
        raise ConfigError(
            f"{where}.profile {profile!r} is not a profile of {peer}; "
            f"choose {', '.join(profile_names(policy))}"
        ) from None

    model = values.get("model")
    if model is not None and not (isinstance(model, str) and model.strip()):
        raise ConfigError(f"{where}.model must be a non-empty string, got {model!r}")
    at = values["at"]
    if not isinstance(at, str) or not _AT_RE.match(at):
        raise ConfigError(f"{where}.at must be HH:MM in 24-hour local time, got {at!r}")
    day = values.get("day", ROUTINE_DAYS[0])
    if day not in ROUTINE_DAYS:
        raise ConfigError(f'{where}.day must be "yesterday-utc", got {day!r}')
    sources = _routine_sources(f"{where}.sources", values.get("sources", []), collections)
    catch_up = values.get("catch_up_days", 3)
    if not _positive_int(catch_up) or catch_up > 30:
        raise ConfigError(
            f"{where}.catch_up_days must be an integer from 1 to 30, got {catch_up!r}"
        )
    timeout = values.get("timeout_s")
    if timeout is not None and not _positive_int(timeout):
        raise ConfigError(f"{where}.timeout_s must be a positive integer or null, got {timeout!r}")
    budget = values.get("budget_usd_per_day")
    if budget is not None:
        if isinstance(budget, bool) or not isinstance(budget, int | float) or budget <= 0:
            raise ConfigError(
                f"{where}.budget_usd_per_day must be a positive number or null, got {budget!r}"
            )
        if peer != "claude":
            raise ConfigError(
                f"{where}.budget_usd_per_day is Claude-only (codex reports no cost); "
                "remove it or set it to null"
            )
    outputs = _routine_outputs(
        f"{where}.outputs", values.get("outputs", dict(DEFAULT_ROUTINE_OUTPUTS)), data_root
    )
    prepare = values.get("prepare")
    stale_sources = values.get("stale_sources", ROUTINE_STALE_SOURCES[0])
    if stale_sources not in ROUTINE_STALE_SOURCES:
        raise ConfigError(
            f"{where}.stale_sources must be one of {', '.join(ROUTINE_STALE_SOURCES)}, "
            f"got {stale_sources!r}"
        )
    return RoutineSpec(
        name=name,
        peer=peer,
        prompt_file=prompt_file,
        cd=cd,
        profile=profile,
        model=model,
        at=at,
        day=day,
        sources=sources,
        catch_up_days=catch_up,
        timeout_s=timeout,
        budget_usd_per_day=float(budget) if budget is not None else None,
        outputs=outputs,
        prepare=None if prepare is None else _routine_argv(f"{where}.prepare", prepare),
        validate=_routine_argv(f"{where}.validate", values["validate"]),
        terminal_hook=_terminal_hook(f"{where}.terminal_hook", values.get("terminal_hook")),
        stale_sources=stale_sources,
        raw=values,
    )


def _parse_routines(
    spec: object, data_root: Path, invoke: InvokePolicy, collections: dict
) -> dict[str, RoutineSpec]:
    """Parse the optional ``routines`` block (after ``invoke`` and ``collections``)."""
    if spec is None:
        return {}
    if not isinstance(spec, dict):
        raise ConfigError(
            f"'routines' must be an object of named routines, got {type(spec).__name__}"
        )
    routines: dict[str, RoutineSpec] = {}
    for name, value in spec.items():
        if name.startswith("//"):
            continue
        if not NAME_RE.match(name):
            raise ConfigError(f"routine name {name!r} is invalid: {NAME_RULE}")
        routines[name] = _parse_routine(name, value, data_root, invoke, collections)
    return routines


def state_home() -> Path:
    """``$XDG_STATE_HOME`` if set and non-empty, else ``~/.local/state``."""
    raw = os.environ.get("XDG_STATE_HOME")
    return Path(raw) if raw else Path.home() / ".local" / "state"


def routine_paths(config: CairnConfig, name: str) -> dict[str, Path | str]:
    """The fixed locations of routine ``name``: state dir, log, launchd plist and label."""
    label = f"{ROUTINE_LABEL_PREFIX}{name}"
    return {
        "state_dir": state_home() / "cairn" / "routines" / name,
        "log": config.data_root / "logs" / f"routine-{name}.log",
        "plist": Path.home() / "Library" / "LaunchAgents" / f"{label}.plist",
        "label": label,
    }


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
    data_root = expand(data["data_root"])
    routines = _parse_routines(data.get("routines"), data_root, invoke, collections)

    return CairnConfig(
        repo_root=repo_root,
        config_path=config_path,
        data_root=data_root,
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
        routines=routines,
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
    "BILLING_PEERS",
    "BILLING_ROUTES",
    "CONFIG_FILENAME",
    "DEFAULT_CRON_SCHEDULE",
    "ENV_OVERRIDE",
    "EXAMPLE_FILENAME",
    "INVOKE_PEERS",
    "NAME_RE",
    "PROFILES",
    "PROFILE_PEERS",
    "ROUTINE_PEERS",
    "TEMPLATE_TOKEN_RE",
    "BillingPolicy",
    "CairnConfig",
    "Collection",
    "ConfigError",
    "InvokePolicy",
    "PeerPolicy",
    "ProfileSpec",
    "RoutineSpec",
    "ServiceSource",
    "SyncPolicy",
    "check_profile_args",
    "claude_slug",
    "example_template_text",
    "expand",
    "expand_home",
    "expand_routine_path",
    "find_repo_root",
    "get_config",
    "load_config_from",
    "profile_names",
    "resolve_profile",
    "routine_paths",
    "state_home",
    "user_config_path",
]
