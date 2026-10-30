"""The ``provenance`` key: the value an export writes and the value a reader gets."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr

from cairn import provenance
from cairn.frontmatter import parse_frontmatter
from cairn.invoke import preamble
from cairn.schema import Message

CALLER = "claude:abc12345-feed-0000-0000-000000000000"


def _user(text: str) -> Message:
    return {"role": "user", "text": text, "timestamp": "2026-03-28T05:20:11Z"}


def _assistant(text: str) -> Message:
    return {"role": "assistant", "text": text, "timestamp": "2026-03-28T05:20:12Z"}


class AtExport(unittest.TestCase):
    def test_plain_transcript_is_tool(self):
        messages = [_user("Investigate the widget bug"), _assistant("Found it")]
        self.assertEqual(provenance.at_export("claude", "claude-session", messages), "tool")

    def test_invoke_preamble_is_agent(self):
        prompt = preamble("codex", CALLER, 1, 600) + "\n\nReview the diff"
        messages = [_user(""), _user(prompt), _assistant("VERDICT: accept - fine")]
        self.assertEqual(provenance.at_export("codex", "codex-session", messages), "agent")

    def test_mode_prefixed_preamble_is_agent(self):
        prompt = "/plan " + preamble("antigravity", None, 1, 600) + "\n\nPlan it"
        self.assertEqual(
            provenance.at_export("antigravity", "antigravity-session", [_user(prompt)]), "agent"
        )

    def test_owner_form_preamble_is_agent(self):
        prompt = preamble("codex", None, 1, 600, owner="routine:example-daily")
        self.assertIn("being invoked by routine:example-daily", prompt)
        self.assertEqual(provenance.at_export("codex", "codex-session", [_user(prompt)]), "agent")

    def test_preamble_in_later_message_is_tool(self):
        messages = [_user("Look at this"), _user(preamble("codex", CALLER, 1, 600))]
        self.assertEqual(provenance.at_export("codex", "codex-session", messages), "tool")

    def test_subagent_type_is_agent(self):
        for source in ("claude", "codex", "antigravity"):
            with self.subTest(source=source):
                messages = [_user("Survey the widget tests")]
                value = provenance.at_export(source, f"{source}-subagent", messages)
                self.assertEqual(value, "agent")

    def test_granola_is_imported(self):
        self.assertEqual(provenance.at_export("granola", "granola-meeting", []), "imported")
        self.assertEqual(provenance.derive({"type": "granola-meeting"}, "granola"), "imported")


class OnRead(unittest.TestCase):
    def test_stored_value_wins(self):
        fm = {"type": "claude-session", "provenance": "imported", "owner": "routine:x"}
        self.assertEqual(provenance.effective(fm, "claude", "/tmp/x.md"), "imported")

    def test_absent_key_derives_from_owner_and_parent_link(self):
        base = {"type": "codex-session"}
        cases: list[tuple[dict, str]] = [
            ({}, "tool"),
            ({"owner": "routine:example-daily"}, "agent"),
            ({"links": [f"parent {CALLER}"]}, "agent"),
            ({"links": [f'"parent {CALLER}"']}, "agent"),
            ({"links": [f"peer {CALLER}"]}, "tool"),
            ({"links": [f"child {CALLER}"]}, "tool"),
            ({"type": "codex-subagent"}, "agent"),
        ]
        for extra, expected in cases:
            with self.subTest(extra=extra):
                fm = {**base, **extra}
                self.assertEqual(provenance.effective(fm, "codex", "/tmp/x.md"), expected)

    def test_invalid_stored_value_warns_and_derives(self):
        fm = {"type": "claude-session", "provenance": "robot", "owner": "routine:x"}
        err = io.StringIO()
        with redirect_stderr(err):
            value = provenance.effective(fm, "claude", "/tmp/demo/x.md")
        self.assertEqual(value, "agent")
        self.assertEqual(
            err.getvalue(),
            "Warning: /tmp/demo/x.md: provenance 'robot' is not one of user, agent, tool, "
            "imported; reading it as agent\n",
        )

    def test_empty_stored_value_warns(self):
        for raw in ("provenance:", 'provenance: ""'):
            with self.subTest(raw=raw):
                fm = parse_frontmatter(f"---\ntype: claude-session\n{raw}\n---\n")
                err = io.StringIO()
                with redirect_stderr(err):
                    value = provenance.effective(fm, "claude", "/tmp/demo/x.md")
                self.assertEqual(value, "tool")
                self.assertEqual(
                    err.getvalue(),
                    "Warning: /tmp/demo/x.md: provenance '' is not one of user, agent, tool, "
                    "imported; reading it as tool\n",
                )

    def test_absent_key_is_silent(self):
        err = io.StringIO()
        with redirect_stderr(err):
            value = provenance.effective({"type": "claude-session"}, "claude", "/tmp/x.md")
        self.assertEqual((value, err.getvalue()), ("tool", ""))


if __name__ == "__main__":
    unittest.main()
