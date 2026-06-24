"""cairn - shared engine for the per-agent session-sync skills.

A faithful local port of the conversation_provenance_service design (SammyClub/api
PR #2199): one normalized schema, one decoupled renderer, per-source adapters, and
a shared CLI. Each source under :mod:`cairn.sources` subclasses
:class:`cairn.sources.base.ConversationSource` and is driven through
:func:`cairn.cli.run`; the ``cairn`` console command dispatches to them.
"""

from __future__ import annotations

__version__ = "2.0.0"

from . import (
    cli,
    frontmatter,
    listing,
    mutations,
    paths,
    projects,
    qmd,
    rendering,
    schema,
    timeutil,
)
from .sources.base import ConversationSource

__all__ = [
    "ConversationSource",
    "__version__",
    "cli",
    "frontmatter",
    "listing",
    "mutations",
    "paths",
    "projects",
    "qmd",
    "rendering",
    "schema",
    "timeutil",
]
