"""Codex's live model catalogue: ``$CODEX_HOME/models_cache.json``, read as data.

The Codex CLI caches the model list it fetched from the service. cairn reads that
file instead of keeping a static model list in code, so a new model works the day
the CLI learns about it. An empty catalogue means "unknown": the invoke self-check
treats that as a problem (fail loud) rather than guessing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import NamedTuple

# Display order for reasoning efforts (lightest first). Unknown levels sort last.
_EFFORT_ORDER = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")


class CodexModelInfo(NamedTuple):
    slug: str
    efforts: frozenset[str]
    default_effort: str | None
    visible: bool
    fast: bool


def codex_home() -> Path:
    """``$CODEX_HOME`` if set, else ``~/.codex``."""
    home = os.environ.get("CODEX_HOME")
    return Path(home).expanduser() if home else Path.home() / ".codex"


def models_cache_path() -> Path:
    """Where the Codex CLI caches its model catalogue."""
    return codex_home() / "models_cache.json"


def load_codex_model_catalog(path: Path | None = None) -> dict[str, CodexModelInfo]:
    """Parse the CLI's model cache into ``{slug: CodexModelInfo}``.

    Returns ``{}`` when the file is missing, unreadable or not the expected shape;
    callers decide what an unknown catalogue means.
    """
    try:
        raw = json.loads((path or models_cache_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = raw.get("models") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return {}
    catalog: dict[str, CodexModelInfo] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("slug"), str):
            continue
        levels = entry.get("supported_reasoning_levels") or []
        efforts = frozenset(
            level["effort"]
            for level in levels
            if isinstance(level, dict) and isinstance(level.get("effort"), str)
        )
        default_effort = entry.get("default_reasoning_level")
        catalog[entry["slug"]] = CodexModelInfo(
            slug=entry["slug"],
            efforts=efforts,
            default_effort=default_effort if isinstance(default_effort, str) else None,
            visible=entry.get("visibility", "list") != "hide",
            fast="fast" in (entry.get("additional_speed_tiers") or []),
        )
    return catalog


def check_codex_invocation(
    model: str | None, effort: str | None, fast: bool, catalog: dict[str, CodexModelInfo]
) -> list[str]:
    """Problems with a Codex model/effort/fast choice against ``catalog`` (empty = fine).

    ``model=None`` lets the CLI choose, so the model and effort checks are skipped;
    ``fast`` then still needs at least one fast-capable model to exist.
    """
    problems: list[str] = []
    fast_slugs = ", ".join(sorted(s for s, m in catalog.items() if m.visible and m.fast))
    if model is None:
        if fast and not fast_slugs:
            problems.append("fast mode requested but no model in the Codex cache supports it")
        return problems
    info = catalog.get(model)
    if info is None:
        available = ", ".join(sorted(s for s, m in catalog.items() if m.visible))
        problems.append(
            f"model {model!r} is not in this machine's Codex model cache; available: {available}"
        )
        return problems
    if effort and info.efforts and effort not in info.efforts:
        problems.append(
            f"effort {effort!r} is not supported by {model!r}; "
            f"supported: {', '.join(_sorted_efforts(info.efforts))}"
        )
    if fast and not info.fast:
        problems.append(
            f"fast mode is not available for {model!r}; fast-capable: {fast_slugs or '(none)'}"
        )
    return problems


def _sorted_efforts(efforts: frozenset[str]) -> list[str]:
    order = {effort: index for index, effort in enumerate(_EFFORT_ORDER)}
    return sorted(efforts, key=lambda effort: (order.get(effort, len(order)), effort))


def format_models(
    catalog: dict[str, CodexModelInfo], default_model: str | None, as_json: bool
) -> str:
    """Render the catalogue for ``cairn invoke list-models``.

    Text lists visible models only, one per line, tagged ``DEFAULT`` and ``fast``.
    JSON lists every model (hidden ones too, with ``visible: false``).
    """
    if as_json:
        rows = [
            {
                "slug": info.slug,
                "efforts": _sorted_efforts(info.efforts),
                "default_effort": info.default_effort,
                "visible": info.visible,
                "fast": info.fast,
                "default": info.slug == default_model,
            }
            for info in catalog.values()
        ]
        return json.dumps(rows, indent=1)
    lines = []
    for info in catalog.values():
        if not info.visible:
            continue
        efforts = ",".join(_sorted_efforts(info.efforts))
        flags = (("DEFAULT", info.slug == default_model), ("fast", info.fast))
        tags = [tag for tag, on in flags if on]
        tag = f"  [{' '.join(tags)}]" if tags else ""
        lines.append(
            f"{info.slug:<16} efforts={efforts:<38} default_effort={info.default_effort}{tag}"
        )
    return "\n".join(lines)


__all__ = [
    "CodexModelInfo",
    "check_codex_invocation",
    "codex_home",
    "format_models",
    "load_codex_model_catalog",
    "models_cache_path",
]
