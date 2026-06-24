"""Shared renderer helpers: artifact blocks and UTF-8-safe output."""

from __future__ import annotations

import unittest

from cairn.rendering import render_artifacts, render_conversation, strip_lone_surrogates


class TestRenderArtifacts(unittest.TestCase):
    def test_empty_when_no_files(self):
        self.assertEqual(render_artifacts([], []), [])

    def test_created_kept_in_order_modified_sorted(self):
        lines = render_artifacts(["b.py", "a.py"], ["z.py", "m.py"])
        self.assertEqual(
            lines,
            [
                "## Artifacts",
                "",
                "**Created:**",
                "- `b.py`",  # created keeps given order
                "- `a.py`",
                "",
                "**Modified:**",
                "- `m.py`",  # modified is sorted
                "- `z.py`",
                "",
            ],
        )

    def test_only_created(self):
        self.assertEqual(
            render_artifacts(["a.py"], []),
            ["## Artifacts", "", "**Created:**", "- `a.py`", ""],
        )


class TestStripLoneSurrogates(unittest.TestCase):
    def test_removes_lone_surrogate(self):
        self.assertEqual(strip_lone_surrogates("Trophy \ud83c done"), "Trophy  done")

    def test_paired_emoji_untouched(self):
        # A real emoji is a single non-surrogate code point in a Python str.
        text = "clean \U0001f3c6 text"
        self.assertEqual(strip_lone_surrogates(text), text)

    def test_result_is_utf8_encodable(self):
        cleaned = strip_lone_surrogates("a\ud800b\udfffc")
        cleaned.encode("utf-8")  # must not raise
        self.assertEqual(cleaned, "abc")

    def test_noop_on_plain_text(self):
        self.assertEqual(strip_lone_surrogates("nothing special"), "nothing special")


class TestRenderConversation(unittest.TestCase):
    def test_unrecoverable_reasoning_is_marked_not_dropped(self):
        lines = render_conversation(
            [{"role": "assistant", "text": "ok", "reasoning_recoverable": False}]
        )
        body = "\n".join(lines)
        self.assertIn("[reasoning: unrecoverable]", body)
        self.assertIn("ok", body)


if __name__ == "__main__":
    unittest.main()
