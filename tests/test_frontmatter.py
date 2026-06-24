"""Frontmatter parse / emit / mutate correctness - the regression bar for the
hand-rolled YAML handling. Each test pins a bug the prior review found.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import unittest

from cairn.frontmatter import (
    FrontmatterParseError,
    head_close,
    head_open,
    parse_frontmatter,
    preserved_tail,
    rewrite_preserved_tail,
    title_line,
)

CANONICAL = (
    "---\n"
    "type: claude-session\n"
    "date: 2026-06-19\n"
    "session_id: 41b8c5c7-9f09-4f14-b24a-dbff294f7282\n"
    "repo: context\n"
    'title: "ed plan ⎿ /plan to preview"\n'
    "messages: 164\n"
    "last_activity: 2026-06-22T11:29:35.633Z\n"
    "status: active\n"
    "tags: []\n"
    "rating: null\n"
    'comments: ""\n'
    "projects: []\n"
    "---\n"
    "\n"
    "# Title\n"
    "\n"
    "## Conversation\n"
    "### User\n"
    "I said the build status: failed and gave it a rating: 3 here.\n"
)


class TitleRoundTrip(unittest.TestCase):
    def test_quotes_round_trip_without_growing_backslashes(self):
        # B2: escaping must be symmetric so quotes don't accumulate backslashes.
        title = 'say "hi" to the world'
        line = title_line(title)
        parsed = parse_frontmatter(f"---\n{line}\n---\n")
        self.assertEqual(parsed["title"], title)
        # second round must be a fixed point
        self.assertEqual(title_line(parsed["title"]), line)

    def test_backslashes_round_trip(self):
        title = r'a \ b "c"'
        parsed = parse_frontmatter(f"---\n{title_line(title)}\n---\n")
        self.assertEqual(parsed["title"], title)


class PreservedTailEmission(unittest.TestCase):
    def test_canonical_tail_is_stable(self):
        fm = parse_frontmatter(CANONICAL)
        tail = preserved_tail(fm)
        self.assertEqual(
            tail, ["status: active", "tags: []", "rating: null", 'comments: ""', "projects: []"]
        )


class MutationScoping(unittest.TestCase):
    """The headline fixes: mutations touch only the frontmatter tail, never the body."""

    def _body(self, content: str) -> str:
        return content.split("\n---\n", 1)[1]

    def test_set_status_does_not_touch_body(self):
        # B3: 'status: failed' in the body must survive a status change.
        out = rewrite_preserved_tail(CANONICAL, lambda fm: fm.__setitem__("status", "done"))
        assert out is not None  # CANONICAL has frontmatter
        self.assertIn("status: done", out)
        self.assertEqual(self._body(CANONICAL), self._body(out))
        self.assertIn("the build status: failed", out)  # body line untouched

    def test_set_rating_does_not_touch_body(self):
        # B4: 'rating: 3' in the body must survive a rating change.
        out = rewrite_preserved_tail(CANONICAL, lambda fm: fm.__setitem__("rating", 8))
        assert out is not None  # CANONICAL has frontmatter
        self.assertIn("rating: 8", out)
        self.assertIn("gave it a rating: 3 here", out)

    def test_unchanged_mutation_is_byte_identical(self):
        # Re-emitting the same values must reproduce the file exactly (no churn).
        out = rewrite_preserved_tail(CANONICAL, lambda fm: None)
        self.assertEqual(out, CANONICAL)

    def test_tags_cannot_inject_frontmatter_keys(self):
        # B8: a newline in a tag must not create a new top-level key.
        out = rewrite_preserved_tail(
            CANONICAL, lambda fm: fm.__setitem__("tags", ["foo\nstatus: hacked".replace("\n", " ")])
        )
        assert out is not None  # CANONICAL has frontmatter
        reparsed = parse_frontmatter(out)
        self.assertEqual(reparsed["status"], "active")  # not "hacked"
        self.assertEqual(reparsed["tags"], ["foo status: hacked"])

    def test_comment_block_round_trips_through_reparse(self):
        # B5/B6: multi-line comment blocks survive a parse→emit cycle.
        step1 = rewrite_preserved_tail(CANONICAL, lambda fm: fm.__setitem__("comments", "first"))
        assert step1 is not None  # CANONICAL has frontmatter
        step2 = rewrite_preserved_tail(
            step1,
            lambda fm: fm.__setitem__(
                "comments", (parse_frontmatter(step1).get("comments") or "") + "\nsecond"
            ),
        )
        assert step2 is not None
        fm = parse_frontmatter(step2)
        self.assertEqual(fm["comments"], "first\nsecond")
        self.assertEqual(self._body(CANONICAL), self._body(step2))

    def test_no_frontmatter_returns_none(self):
        self.assertIsNone(rewrite_preserved_tail("no frontmatter here", lambda fm: None))


class ParserHardening(unittest.TestCase):
    """The hardened parser fails loudly on YAML it can't faithfully round-trip,
    instead of silently mis-parsing it and corrupting preserved fields."""

    def test_no_block_returns_empty_dict(self):
        # A non-session markdown file has no fields - not an error (listing skips it).
        self.assertEqual(parse_frontmatter("# just a heading\n"), {})

    def test_block_dialect_round_trips(self):
        fm = parse_frontmatter(CANONICAL)
        self.assertEqual(fm["type"], "claude-session")
        self.assertEqual(fm["messages"], "164")
        self.assertEqual(fm["tags"], [])
        self.assertEqual(fm["projects"], [])
        self.assertEqual(fm["comments"], "")

    def test_block_sequence_items_parse(self):
        fm = parse_frontmatter("---\ntags:\n  - a\n  - b\n---\n")
        self.assertEqual(fm["tags"], ["a", "b"])

    def test_block_scalar_parses(self):
        fm = parse_frontmatter("---\ncomments: |\n  one\n  two\n---\n")
        self.assertEqual(fm["comments"], "one\ntwo")

    def test_flow_sequence_raises(self):
        with self.assertRaises(FrontmatterParseError):
            parse_frontmatter("---\ntags: [a, b]\n---\n")

    def test_flow_mapping_raises(self):
        with self.assertRaises(FrontmatterParseError):
            parse_frontmatter("---\nmeta: {x: 1}\n---\n")

    def test_folded_scalar_raises(self):
        with self.assertRaises(FrontmatterParseError):
            parse_frontmatter("---\nsummary: >\n  folded\n---\n")

    def test_nested_mapping_raises(self):
        with self.assertRaises(FrontmatterParseError):
            parse_frontmatter("---\nouter:\n  inner: 1\n---\n")

    def test_orphan_list_item_raises(self):
        with self.assertRaises(FrontmatterParseError):
            parse_frontmatter("---\n  - orphan\n---\n")

    def test_line_without_colon_raises(self):
        with self.assertRaises(FrontmatterParseError):
            parse_frontmatter("---\nnot a mapping line\n---\n")


class HeadHelpers(unittest.TestCase):
    """head_open/head_close emit exactly the lines the per-skill heads used to
    hand-build, so the shared seam is byte-identical to the old inline code."""

    def test_head_open_lines(self):
        self.assertEqual(
            head_open("claude-session", "2026-06-19", "session_id", "abc"),
            ["---", "type: claude-session", "date: 2026-06-19", "session_id: abc"],
        )

    def test_head_close_with_message_count(self):
        self.assertEqual(
            head_close("2026-06-22T11:29:35.633Z", 164),
            ["messages: 164", "last_activity: 2026-06-22T11:29:35.633Z"],
        )

    def test_head_close_without_message_count(self):
        # Meeting transcripts (granola) have no turn count.
        self.assertEqual(
            head_close("2026-06-22T11:29:35.633Z"),
            ["last_activity: 2026-06-22T11:29:35.633Z"],
        )

    def test_head_close_defaults_last_activity_to_now(self):
        lines = head_close(None, 5)
        self.assertEqual(lines[0], "messages: 5")
        self.assertTrue(lines[1].startswith("last_activity: "))
        self.assertGreater(len(lines[1]), len("last_activity: "))


if __name__ == "__main__":
    unittest.main()
