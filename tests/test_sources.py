"""Smoke tests for the moved sources, CLI parity, and the lazy dispatcher.

Confirms each source imports + exposes ``main`` + (conversation sources) a
ConversationSource subclass; that every source exposes the canonical verb set
with the shared flags; and that the dispatcher routes/fails as designed.
"""

from __future__ import annotations

import importlib
import sys
import unittest
from unittest import mock

from cairn import cli
from cairn.sources.base import ConversationSource

CONVERSATION_SOURCES = {
    "claude": "ClaudeSource",
    "codex": "CodexSource",
    "cursor": "CursorSource",
    "granola": "GranolaSource",
}
CANONICAL_VERBS = {"export", "list", "context", "note", "close", "log"}


def _source(mod: str, cls: str) -> ConversationSource:
    return getattr(importlib.import_module(f"cairn.sources.{mod}"), cls)()


class TestSourceModules(unittest.TestCase):
    def test_conversation_sources_expose_main_and_subclass(self):
        for mod, cls in CONVERSATION_SOURCES.items():
            with self.subTest(source=mod):
                m = importlib.import_module(f"cairn.sources.{mod}")
                self.assertTrue(callable(getattr(m, "main", None)))
                self.assertTrue(issubclass(getattr(m, cls), ConversationSource))

    def test_service_docs_exposes_main(self):
        m = importlib.import_module("cairn.sources.service_docs")
        self.assertTrue(callable(getattr(m, "main", None)))


class TestCliParity(unittest.TestCase):
    def _subcommands(self, parser):
        action = next(a for a in parser._actions if getattr(a, "choices", None))
        return set(action.choices)

    def test_every_source_has_the_canonical_verbs(self):
        for mod, cls in CONVERSATION_SOURCES.items():
            with self.subTest(source=mod):
                cmds = self._subcommands(cli.build_parser(_source(mod, cls)))
                self.assertTrue(CANONICAL_VERBS.issubset(cmds), f"{mod}: {cmds}")

    def test_export_has_shared_flags(self):
        parser = cli.build_parser(_source("codex", "CodexSource"))
        a = parser.parse_args(["export", "--since", "3d"])
        self.assertFalse(a.all)
        self.assertIsInstance(a.since, float)
        a = parser.parse_args(["export", "--all", "--json", "--quiet"])
        self.assertTrue(a.all and a.json and a.quiet)

    def test_since_and_all_are_mutually_exclusive(self):
        parser = cli.build_parser(_source("codex", "CodexSource"))
        with self.assertRaises(SystemExit):
            parser.parse_args(["export", "--since", "1d", "--all"])


class TestDispatcher(unittest.TestCase):
    def _run(self, argv):
        from cairn import __main__ as dispatcher

        with mock.patch.object(sys, "argv", argv), self.assertRaises(SystemExit) as ctx:
            dispatcher.main()
        return ctx.exception.code

    def test_unknown_command_exits_2(self):
        self.assertEqual(self._run(["cairn", "bogus"]), 2)

    def test_help_exits_0(self):
        self.assertEqual(self._run(["cairn", "help"]), 0)

    def test_version_exits_0(self):
        self.assertEqual(self._run(["cairn", "--version"]), 0)

    def test_route_rewrites_argv(self):
        from cairn import __main__ as dispatcher

        seen = {}

        def fake_main():
            seen["argv"] = list(sys.argv)

        with (
            mock.patch.object(dispatcher, "_resolve", return_value=fake_main),
            mock.patch.object(sys, "argv", ["cairn", "codex", "export", "--all"]),
            self.assertRaises(SystemExit),
        ):
            dispatcher.main()
        self.assertEqual(seen["argv"], ["cairn codex", "export", "--all"])


if __name__ == "__main__":
    unittest.main()
