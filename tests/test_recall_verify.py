"""``cairn recall verify``: citation grammar, checks, report, ``--json`` and ``--fix``.

The digest comes from invented exports built with the real renderer
(``tests/test_recall_digest.py``). The session follows the worked example of the
journal-verify spec (4.8): 14 frames, frames 8 to 13 on the digest day.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from cairn import digest
from cairn.frames import locate
from cairn.verify import CITATION_RE, NEAR_CITATION_RE, REASONS, normalise
from tests.test_recall_digest import DAY, DigestFixture, document, fixture_document, msg

SESSION = "3f2a9c1e-7b4d-4e2a-9c1e-000000000001"
REF = f"claude:{SESSION}"
SIDECAR = "claude-3f2a9c1e7b4d.frames.jsonl"
F8 = "The retry test still fails on CI. Find out why and fix it."
F11 = "Found it: the backoff cap ran before the jitter.\nI moved the cap after the jitter."
VERIFIED = '- Fixed the backoff cap: "I moved the cap after the jitter" [claude:3f2a9c1e f11]'
BAD_ORDINAL = "- The full suite went green [claude:3f2a9c1e f42]"
OPEN, CLOSE = chr(0x201C), chr(0x201D)  # curly double quotes
NOT_FOUND = '- The user asked to "fix the retry test before the release" [claude:3f2a9c1e f8]'


def example_messages() -> list[dict]:
    """Eight frames on the day before, then the six frames of the worked example."""
    before = [
        msg("user" if n % 2 == 0 else "assistant", f"2026-03-27T1{n}:00:00Z", f"Earlier step {n}.")
        for n in range(8)
    ]
    today = [
        msg("user", "2026-03-28T09:00:00Z", F8),
        msg("assistant", "2026-03-28T09:01:00Z", "Reading the retry module."),
        msg("assistant", "2026-03-28T09:02:00Z", "The cap is applied first."),
        msg("assistant", "2026-03-28T09:03:00Z", F11),
        msg("user", "2026-03-28T09:04:00Z", "Good, run the suite."),
        msg("assistant", "2026-03-28T09:05:00Z", "The full suite passes."),
    ]
    return before + today


def journal(*lines: str) -> str:
    """A journal with front matter and a heading, so body lines start at line 5."""
    return "\n".join(["---", "date: 2026-03-28", "---", "# Journal", *lines]) + "\n"


class VerifyFixture(DigestFixture):
    def setUp(self):
        super().setUp()
        self.doc = self.write_doc("example.md", document(SESSION, example_messages()))
        code, _, _, self.out = self.digest()
        self.assertEqual(code, 0)
        self.journal = self.root / "journal" / f"{DAY}.md"
        self.journal.parent.mkdir()

    def verify(self, text: str | None, *args: str) -> tuple[int, str, str]:
        if text is not None:
            self.journal.write_bytes(text.encode("utf-8"))
        return self.cli("verify", str(self.journal), "--digest", str(self.out), *args)

    def report(self, text: str) -> dict:
        code, stdout, _ = self.verify(text, "--json")
        data = json.loads(stdout)
        data["exit"] = code
        return data

    def reasons(self, text: str) -> list[str]:
        return [f["reason"] for f in self.report(text)["failures"]]


class TestGrammar(unittest.TestCase):
    def test_grammar_matches_examples(self):
        for line, ordinal in ((VERIFIED, "11"), (BAD_ORDINAL, "42"), (NOT_FOUND, "8")):
            with self.subTest(line=line):
                match = CITATION_RE.search(line)
                assert match is not None
                self.assertEqual(
                    (match["source"], match["id"], match["ordinal"]),
                    ("claude", "3f2a9c1e", ordinal),
                )
        self.assertIsNone(CITATION_RE.search("see [claude:3f2a9c1e f1](https://x.y/z)"))
        self.assertIsNone(CITATION_RE.search("[claude:3f2a f12]"))
        self.assertIsNotNone(CITATION_RE.search(f"[{REF} f0]"))

    def test_near_citation_shapes(self):
        for text in ("[f12]", "[claude:3f2a f12]", "[claude:3f2a9c1e f12 f13]"):
            with self.subTest(text=text):
                self.assertIsNotNone(NEAR_CITATION_RE.search(text))
                self.assertIsNone(CITATION_RE.search(text))

    def test_normalise(self):
        text = f"  {OPEN}A{CLOSE}  b\n\tc {chr(0x2019)}d{chr(0x2018)} "
        self.assertEqual(normalise(text), "\"A\" b c 'd'")

    def test_reason_vocabulary_is_fixed(self):
        self.assertEqual(
            REASONS,
            (
                "malformed-citation",
                "session-not-in-digest",
                "ambiguous-session",
                "sidecar-missing",
                "bad-ordinal",
                "frame-not-in-digest",
                "export-unreadable",
                "hash-mismatch",
                "quote-not-found",
                "quote-uncited",
            ),
        )


class TestChecks(VerifyFixture):
    def test_verified_journal_exits_0(self):
        code, stdout, stderr = self.verify(journal(VERIFIED))
        self.assertEqual(code, 0)
        self.assertEqual(
            stdout, "verified 1/1 citations, 1/1 quotes (0 short quotes not checked); 0 failures\n"
        )
        self.assertEqual(stderr, "")

    def test_bad_ordinal_line_and_exit_4(self):
        code, stdout, _ = self.verify(journal(BAD_ORDINAL))
        self.assertEqual(code, 4)
        self.assertEqual(
            stdout.splitlines()[0],
            f"{self.journal}:5: bad-ordinal: ordinal 42 not in {SIDECAR} (14 frames)",
        )
        self.assertEqual(
            stdout.splitlines()[1],
            "verified 0/1 citations, 0/0 quotes (0 short quotes not checked); 1 failures",
        )

    def test_quote_not_found(self):
        code, stdout, _ = self.verify(journal(NOT_FOUND))
        self.assertEqual(code, 4)
        self.assertEqual(
            stdout.splitlines()[0],
            f'{self.journal}:5: quote-not-found: "fix the retry test before the release" '
            f"is not in f8 of {REF}",
        )

    def test_malformed_citation_reported(self):
        code, stdout, _ = self.verify(journal("- A claim [f12]", "- Another [claude:3f2a f12]"))
        self.assertEqual(code, 4)
        lines = stdout.splitlines()
        self.assertTrue(lines[0].startswith(f"{self.journal}:5: malformed-citation: [f12] "))
        self.assertTrue(
            lines[1].startswith(f"{self.journal}:6: malformed-citation: [claude:3f2a f12] ")
        )

    def test_markdown_link_text_is_not_a_citation(self):
        data = self.report(
            journal("- See [claude:3f2a9c1e f8](https://x.y/z) and [claude:3f2a9c1e f8]")
        )
        self.assertEqual(data["citations"], {"total": 1, "verified": 1, "failed": 0})

    def test_frame_not_kept_fails(self):
        rows = digest.load_sidecar(self.out / SIDECAR)
        self.assertFalse(rows[0]["kept"])
        self.assertEqual(
            self.reasons(journal("- Started [claude:3f2a9c1e f0]")), ["frame-not-in-digest"]
        )

    def test_session_not_in_digest(self):
        self.assertEqual(
            self.reasons(
                journal("- Elsewhere [claude:99999999 f8]", "- Codex [codex:3f2a9c1e f8]")
            ),
            ["session-not-in-digest", "session-not-in-digest"],
        )

    def test_ambiguous_prefix(self):
        twin = SESSION[:-1] + "2"
        self.write_doc("twin.md", document(twin, example_messages()))
        code, _, _, self.out = self.digest()
        self.assertEqual(code, 0)
        data = self.report(journal("- Either [claude:3f2a9c1e f8]", f"- Full [{REF} f8]"))
        self.assertEqual([f["reason"] for f in data["failures"]], ["ambiguous-session"])
        self.assertEqual(data["citations"]["verified"], 1)

    def test_sidecar_missing(self):
        (self.out / SIDECAR).unlink()
        self.assertEqual(self.reasons(journal("- Gone [claude:3f2a9c1e f8]")), ["sidecar-missing"])

    def test_export_unreadable(self):
        self.doc.unlink()
        self.assertEqual(
            self.reasons(journal("- Gone [claude:3f2a9c1e f8]")), ["export-unreadable"]
        )

    def test_hash_mismatch_after_rerender(self):
        text = self.doc.read_text(encoding="utf-8")
        anchor, frames = locate(text)
        old = frames[11]
        start = anchor + old.offset
        marker_line = f"<!-- cairn:turn {old.length} -->\n"
        self.assertTrue(text.startswith(marker_line, start))
        body = old.body.replace("Found it", "Found it!", 1)
        new = f"<!-- cairn:turn {old.length + 1} -->\n" + body
        end = start + len(marker_line) + len(old.body)
        self.doc.write_bytes((text[:start] + new + text[end:]).encode("utf-8"))
        code, stdout, _ = self.verify(journal(VERIFIED))
        self.assertEqual(code, 4)
        self.assertIn(
            f"{self.journal}:5: hash-mismatch: {REF} frame 11 changed since the digest", stdout
        )
        data = self.report(journal(VERIFIED))
        self.assertEqual(data["quotes"]["unchecked"], 1)

    def test_quote_normalisation(self):
        text = journal(
            f"- Curly {OPEN}I moved the  cap\nafter the jitter{CLOSE} [claude:3f2a9c1e f11]",
        )
        data = self.report(text)
        self.assertEqual((data["exit"], data["quotes"]["verified"]), (0, 1))

    def test_quote_is_case_sensitive(self):
        self.assertEqual(
            self.reasons(journal('- "i moved the cap after the jitter" [claude:3f2a9c1e f11]')),
            ["quote-not-found"],
        )

    def test_short_quote_not_checked(self):
        data = self.report(journal('- The "short one" fix [claude:3f2a9c1e f11]'))
        self.assertEqual(data["exit"], 0)
        self.assertEqual(
            data["quotes"],
            {"total": 0, "verified": 0, "failed": 0, "unchecked": 0, "skipped_short": 1},
        )

    def test_blockquote_takes_previous_citation(self):
        text = journal(
            "- The fix [claude:3f2a9c1e f11]", "", "> I moved the cap", "> after the jitter."
        )
        data = self.report(text)
        self.assertEqual((data["exit"], data["quotes"]["verified"]), (0, 1))
        wrong = journal("- The ask [claude:3f2a9c1e f8]", "> I moved the cap after the jitter.")
        self.assertEqual(self.reasons(wrong), ["quote-not-found"])

    def test_uncited_quote_fails(self):
        self.assertEqual(
            self.reasons(journal('- The user said "fix the retry test before the release"')),
            ["quote-uncited"],
        )
        far = journal(
            "- The fix [claude:3f2a9c1e f11]", "", "", "> I moved the cap after the jitter."
        )
        self.assertEqual(self.reasons(far), ["quote-uncited"])

    def test_code_is_ignored(self):
        text = journal(
            "```",
            '- [claude:3f2a9c1e f42] and "a quote that is not in any frame"',
            "```",
            '- Ran `echo "a quote that is not in any frame"` [claude:3f2a9c1e f8]',
        )
        data = self.report(text)
        self.assertEqual(data["exit"], 0)
        self.assertEqual(data["citations"]["total"], 1)
        self.assertEqual(data["quotes"]["total"], 0)

    def test_unverified_section_ignored(self):
        data = self.report(journal(VERIFIED, "", "## Unverified", "", BAD_ORDINAL))
        self.assertEqual((data["exit"], data["citations"]["total"]), (0, 1))

    def test_no_citations_warns_and_exits_0(self):
        code, stdout, stderr = self.verify(journal("Nothing happened."))
        self.assertEqual(code, 0)
        self.assertEqual(stderr, f"Warning: {self.journal} has no citations\n")
        self.assertIn("verified 0/0 citations", stdout)

    def test_json_shape_and_reason_vocabulary(self):
        data = self.report(journal(VERIFIED, BAD_ORDINAL, NOT_FOUND))
        self.assertEqual(
            sorted(data),
            sorted(
                [
                    "schema_version",
                    "file",
                    "digest",
                    "ok",
                    "citations",
                    "quotes",
                    "failures",
                    "moved",
                    "exit",
                ]
            ),
        )
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["file"], str(self.journal.absolute()))
        self.assertEqual(data["digest"], str(self.out.absolute()))
        self.assertFalse(data["ok"])
        self.assertEqual(data["citations"], {"total": 3, "verified": 2, "failed": 1})
        self.assertEqual(
            data["quotes"],
            {"total": 2, "verified": 1, "failed": 1, "unchecked": 0, "skipped_short": 0},
        )
        self.assertEqual(
            data["failures"][0],
            {
                "line": 6,
                "block": [6, 6],
                "kind": "citation",
                "reason": "bad-ordinal",
                "text": "[claude:3f2a9c1e f42]",
                "ref": REF,
                "ordinal": 42,
                "detail": f"ordinal 42 not in {SIDECAR} (14 frames)",
            },
        )
        self.assertEqual(data["failures"][1]["kind"], "quote")
        self.assertEqual(data["moved"], 0)
        self.assertTrue({f["reason"] for f in data["failures"]} <= set(REASONS))

    def test_not_a_digest_dir_exits_1(self):
        self.journal.write_text(journal(VERIFIED))
        code, _, stderr = self.cli("verify", str(self.journal), "--digest", str(self.root))
        self.assertEqual(code, 1)
        self.assertEqual(
            stderr, f"Error: not a digest directory: {self.root} has no .cairn-digest\n"
        )
        code, _, stderr = self.cli("verify", str(self.root / "none.md"), "--digest", str(self.out))
        self.assertEqual(code, 1)
        self.assertEqual(stderr, f"Error: no such file: {self.root / 'none.md'}\n")

    def test_exit_code_never_3(self):
        texts = [
            journal(VERIFIED),
            journal(BAD_ORDINAL),
            journal("Nothing happened."),
            journal("- [f1]", '- "an uncited quote of some length"'),
        ]
        for text in texts:
            for args in ((), ("--fix",), ("--json",)):
                with self.subTest(text=text, args=args):
                    self.assertIn(self.verify(text, *args)[0], (0, 4))


class TestFix(VerifyFixture):
    def test_fix_moves_only_failing_blocks(self):
        original = journal(VERIFIED, "", NOT_FOUND, "", "Closing line.")
        code, stdout, _ = self.verify(original, "--fix")
        self.assertEqual(code, 0)
        self.assertIn(f"moved 1 blocks to ## Unverified in {self.journal}", stdout)
        self.assertEqual(
            self.journal.read_text(encoding="utf-8"),
            journal(
                VERIFIED,
                "",
                "Closing line.",
                "",
                "## Unverified",
                "",
                "<!-- cairn:unverified line=7 reason=quote-not-found -->",
                NOT_FOUND,
                "",
            ),
        )

    def test_fix_moves_attached_blockquote(self):
        original = journal(BAD_ORDINAL, "> I moved the cap after the jitter.", "", VERIFIED)
        code, _, _ = self.verify(original, "--fix")
        self.assertEqual(code, 0)
        self.assertEqual(
            self.journal.read_text(encoding="utf-8"),
            journal(
                VERIFIED,
                "",
                "## Unverified",
                "",
                "<!-- cairn:unverified line=5 reason=bad-ordinal -->",
                BAD_ORDINAL,
                "",
                "<!-- cairn:unverified line=6 reason=bad-ordinal -->",
                "> I moved the cap after the jitter.",
                "",
            ),
        )

    def test_fix_is_idempotent(self):
        self.verify(journal(VERIFIED, "", BAD_ORDINAL), "--fix")
        fixed = self.journal.read_bytes()
        stamp = 1_000_000_000
        os.utime(self.journal, (stamp, stamp))
        code, stdout, _ = self.verify(None, "--fix")
        self.assertEqual(code, 0)
        self.assertNotIn("moved", stdout)
        self.assertEqual(self.journal.read_bytes(), fixed)
        self.assertEqual(self.journal.stat().st_mtime, stamp)

    def test_fix_appends_to_existing_section(self):
        original = journal(NOT_FOUND, "", "## Unverified", "", "- older entry")
        self.verify(original, "--fix")
        self.assertEqual(
            self.journal.read_text(encoding="utf-8"),
            journal(
                "## Unverified",
                "",
                "- older entry",
                "",
                "<!-- cairn:unverified line=5 reason=quote-not-found -->",
                NOT_FOUND,
                "",
            ),
        )

    def test_fix_keeps_crlf(self):
        original = journal(VERIFIED, "", BAD_ORDINAL).replace("\n", "\r\n")
        self.verify(original, "--fix")
        fixed = self.journal.read_bytes().decode("utf-8")
        self.assertNotIn("\n", fixed.replace("\r\n", ""))
        self.assertIn(
            f"\r\n## Unverified\r\n\r\n<!-- cairn:unverified line=7 reason=bad-ordinal -->\r\n{BAD_ORDINAL}\r\n",
            fixed,
        )

    def test_fix_on_read_only_file_exits_1(self):
        original = journal(BAD_ORDINAL)
        self.journal.write_text(original)
        self.journal.chmod(0o444)
        self.addCleanup(self.journal.chmod, 0o644)
        if os.access(self.journal, os.W_OK):
            self.skipTest("running as a user that can write read-only files")
        code, _, stderr = self.verify(None, "--fix")
        self.assertEqual(code, 1)
        self.assertIn("Error: cannot write", stderr)
        self.assertEqual(self.journal.read_text(), original)


class TestFrameOutputUnchanged(DigestFixture):
    """``recall frame`` shares its hash check with verify; its output is pinned here."""

    def test_recall_frame_output_unchanged(self):
        doc = self.write_doc("fixture.md", fixture_document())
        _, _, _, out = self.digest()
        sidecar = out / "claude-fixture0001.frames.jsonl"
        row = digest.load_sidecar(sidecar)[4]
        text = doc.read_text(encoding="utf-8")
        anchor, frames = locate(text)
        old = frames[4]
        start = anchor + old.offset
        body = old.body.replace("line 0000", "line 00000", 1)
        new = f"<!-- cairn:turn {old.length + 1} -->\n" + body
        end = start + len(f"<!-- cairn:turn {old.length} -->\n") + len(old.body)
        doc.write_bytes((text[:start] + new + text[end:]).encode("utf-8"))
        current = locate(doc.read_text(encoding="utf-8"))[1][4]
        code, stdout, stderr = self.cli("frame", "fixture-0001", "4", "--frames", str(sidecar))
        self.assertEqual(code, 0)
        self.assertEqual(stdout, current.body + "\n")
        self.assertEqual(
            stderr,
            f"Warning: claude:fixture-0001 frame 4 changed since the digest "
            f"(stored sha1_8 {row['sha1_8']}, offset {row['offset']}; "
            f"now sha1_8 {digest.frame_sha1_8(current.body)}, offset {current.offset}); "
            "the export was re-rendered\n",
        )
        code, stdout, stderr = self.cli("frame", "fixture-0001", "4")
        self.assertEqual((code, stdout), (0, current.body + "\n"))
        self.assertEqual(stderr, f"sha1_8 {digest.frame_sha1_8(current.body)}\n")
        unchanged = digest.frame_unchanged(text, anchor, frames[4], row)
        self.assertTrue(unchanged)


if __name__ == "__main__":
    unittest.main()
