"""QMD diagnostics shared by sync and doctor: version, status and embed-output parsing.

Fixtures are sanitised copies of real QMD output (1.1.5 and 2.5.3). Runtime probes
use ``#!python`` stub binaries; no real qmd, model or index is touched.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from cairn import qmd

# `qmd status` from QMD 2.5.3 with colour codes; Pending is printed only when > 0.
STATUS_COMPLETE = (
    "\x1b[1mQMD Status\x1b[0m\n\nIndex: /tmp/index.sqlite\nSize:  1.2 GB\n\n"
    "\x1b[1mDocuments\x1b[0m\n  Total:    12,481 files indexed\n"
    "  Vectors:  301,557 embedded\n  Updated:  2m ago\n"
)
STATUS_PENDING = (
    "QMD Status\n\nDocuments\n  Total:    12481 files indexed\n  Vectors:  301557 embedded\n"
    "  \x1b[33mPending:  611 need embedding\x1b[0m (run 'qmd embed')\n"
)
STATUS_EMPTY = "QMD Status\n\nDocuments\n  Total:    0 files indexed\n  Vectors:  0 embedded\n"

# QMD 1.1.5: exit 0 after its 30-minute session expired mid-run.
EMBED_115_FAILED = (
    "Embedding 1604 documents (131291 chunks, 256.5 MB)\n"
    "✓ Done! Embedded 83072 chunks from 1604 documents in 30m 1s (145.9 KB/s)\n"
    "⚠ 48219 chunks failed\n"
    '⚠ Error embedding "notes/a.md" chunk 7: SessionReleasedError: LLM session has been '
    "released or aborted\n"
)
# QMD 2.5.3 failure summary (formatCount adds thousands separators).
EMBED_253_FAILED = (
    "\x1b[32m✓ Done!\x1b[0m Embedded 53,742 chunks from 622 documents in 30m 0s\n"
    "\x1b[33m⚠ 1,204 chunks still failed after retries\x1b[0m\n"
    "  notes/a.md#3 (3 attempts): LLM session expired before embedding chunk\n"
)
EMBED_253_OK = (
    "Model: embeddinggemma-300M-Q8_0.gguf\n\n✓ Done! Embedded 53742 chunks from 622 documents "
    "in 21m 19s\n[node-llama-cpp] ggml_metal_library_init_from_source: error compiling source\n"
)


def _status(text: str) -> qmd.IndexStatus:
    parsed = qmd.parse_status(text)
    assert parsed is not None, text
    return parsed


def _version(text: str) -> tuple[int, int, int]:
    parsed = qmd.parse_version(text)
    assert parsed is not None, text
    return parsed


class TestParseVersion(unittest.TestCase):
    def test_versions(self):
        cases = {
            "qmd 2.5.3 (179d450)": (2, 5, 3),
            "qmd 1.1.5": (1, 1, 5),
            "\x1b[1mqmd 3.0.0\x1b[0m": (3, 0, 0),
            "qmd 2.6.0-beta.1": None,
            "qmd version unknown": None,
            "": None,
        }
        for text, expected in cases.items():
            self.assertEqual(qmd.parse_version(text), expected, text)

    def test_ordering_against_tested_baseline(self):
        self.assertLess(_version("qmd 1.1.5"), qmd.TESTED_QMD_VERSION)
        self.assertEqual(_version("qmd 2.5.3 (x)"), qmd.TESTED_QMD_VERSION)
        self.assertGreater(_version("qmd 2.8.3"), qmd.TESTED_QMD_VERSION)


class TestParseStatus(unittest.TestCase):
    def test_ansi_and_commas_with_omitted_pending_line(self):
        status = _status(STATUS_COMPLETE)
        self.assertEqual(status, qmd.IndexStatus(12481, 301557, 0))
        self.assertEqual(status.state, "complete")

    def test_pending(self):
        status = _status(STATUS_PENDING)
        self.assertEqual(status, qmd.IndexStatus(12481, 301557, 611))
        self.assertEqual(status.state, "pending")

    def test_empty_and_no_vectors(self):
        self.assertEqual(_status(STATUS_EMPTY).state, "empty")
        no_vectors = STATUS_EMPTY.replace("Total:    0", "Total:    5")
        self.assertEqual(_status(no_vectors).state, "no-vectors")

    def test_pending_with_thousands_separator(self):
        text = STATUS_COMPLETE + "  Pending:  1,204 need embedding (run 'qmd embed')\n"
        self.assertEqual(_status(text).pending, 1204)

    def test_malformed_pending_line_is_unknown_not_zero(self):
        for line in (
            "  Pending:  unknown need embedding\n",
            "  Pending:  8 documents need embedding\n",
        ):
            self.assertIsNone(qmd.parse_status(STATUS_COMPLETE + line), line)

    def test_unrecognised_output_is_unknown_not_zero(self):
        for text in (
            "",
            "Documents\n  Total:    5 files indexed\n",  # no Vectors line
            "  Total:    5 files indexed\n  Vectors:  5 embedded\n",  # no Documents heading
            "Documents\n  Total:    many files indexed\n  Vectors:  5 embedded\n",
        ):
            self.assertIsNone(qmd.parse_status(text), repr(text))


# An invented `qmd collection list` in QMD 2.x's block format (with colour codes).
COLLECTION_LIST = (
    "Collections (3):\n\n"
    "\x1b[1mnotes\x1b[0m (qmd://notes/)\n  Pattern:  **/*.md\n  Files:    1,429\n"
    "  Updated:  3h ago\n\n"
    "claude-code-sessions (qmd://claude-code-sessions/)\n  Pattern:  **/*.md\n"
    "  Files:    120\n  Updated:  9m ago\n\n"
    "cursor-sessions (qmd://cursor-sessions/)\n  Pattern:  **/*.md\n  Files:    0\n"
    "  Updated:  11d ago\n"
)


class TestParseCollectionList(unittest.TestCase):
    def test_files_and_relative_updated(self):
        found = qmd.parse_collection_list(COLLECTION_LIST)
        self.assertEqual(list(found), ["notes", "claude-code-sessions", "cursor-sessions"])
        self.assertEqual(found["notes"], qmd.CollectionInfo(1429, 3 * 3600, 3600))
        self.assertEqual(found["claude-code-sessions"], qmd.CollectionInfo(120, 9 * 60, 60))
        self.assertEqual(found["cursor-sessions"], qmd.CollectionInfo(0, 11 * 86400, 86400))

    def test_unknown_unit_is_none(self):
        text = "notes (qmd://notes/)\n  Files:    4\n  Updated:  2mo ago\n"
        self.assertEqual(qmd.parse_collection_list(text)["notes"], qmd.CollectionInfo(4))

    def test_block_without_files_line(self):
        text = "qmd://codex-sessions/\nqmd://claude-code-sessions/\n  Updated:  0s ago\n"
        found = qmd.parse_collection_list(text)
        self.assertEqual(found["codex-sessions"], qmd.CollectionInfo())
        self.assertEqual(found["claude-code-sessions"], qmd.CollectionInfo(None, 0, 1))


class TestEmbedFailures(unittest.TestCase):
    def test_qmd_115_failed_chunks_and_session_release(self):
        problems = qmd.embed_failures(EMBED_115_FAILED)
        self.assertEqual(problems[0], "48219 chunks failed")
        self.assertTrue(any("chunk error line" in p for p in problems))
        self.assertTrue(any("session expired" in p for p in problems))

    def test_qmd_253_failure_summary_with_commas(self):
        self.assertIn("1,204 chunks failed", qmd.embed_failures(EMBED_253_FAILED))

    def test_zero_failed_chunks_and_benign_stderr_are_not_failures(self):
        self.assertEqual(qmd.embed_failures(EMBED_253_OK), [])
        self.assertEqual(qmd.embed_failures("✓ Done!\n⚠ 0 chunks failed\n"), [])

    def test_session_expired_and_error_rate_warnings(self):
        out = "⚠ Session expired — skipping 12 remaining chunks\n⚠ Error rate too high (9/10)"
        problems = qmd.embed_failures(out)
        self.assertEqual(len(problems), 2)


class TestRuntimeProbes(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def stub(self, body: str) -> str:
        path = self.root / "qmd"
        path.write_text(f"#!{sys.executable}\nimport os, sys, time\n{body}\n")
        path.chmod(0o755)
        return str(path)

    def test_status_parses_from_the_configured_binary(self):
        binary = self.stub(f"print({STATUS_PENDING!r}); print('Metal warning', file=sys.stderr)")
        self.assertEqual(qmd.index_status(binary).pending, 611)

    def test_version_succeeds_but_status_crashes(self):
        binary = self.stub(
            "if sys.argv[1] == '--version': print('qmd 2.8.3 (abc)')\n"
            "else: os.kill(os.getpid(), 11)"
        )
        self.assertEqual(qmd.qmd_version(binary), "qmd 2.8.3 (abc)")
        with self.assertRaisesRegex(RuntimeError, "killed by signal 11"):
            qmd.index_status(binary)

    def test_nonzero_exit_and_missing_binary(self):
        with self.assertRaisesRegex(RuntimeError, "exit 3"):
            qmd.index_status(self.stub("print('db locked', file=sys.stderr); sys.exit(3)"))
        with self.assertRaisesRegex(RuntimeError, "could not start"):
            qmd.index_status(str(self.root / "missing"))
        with self.assertRaisesRegex(RuntimeError, "could not start"):
            qmd.qmd_version(str(self.root / "missing"))

    def test_unrecognised_status_is_a_format_error(self):
        with self.assertRaises(qmd.StatusFormatError):
            qmd.index_status(self.stub("print('something new')"))

    def test_status_timeout(self):
        with (
            mock.patch.object(qmd, "STATUS_TIMEOUT", 0.5),
            self.assertRaisesRegex(RuntimeError, "timed out"),
        ):
            qmd.index_status(self.stub("time.sleep(30)"))

    def test_effective_environment_is_inherited(self):
        binary = self.stub(
            "print('Documents\\n  Total:    1 files indexed\\n  Vectors:  1 embedded')\n"
            "assert os.environ['INDEX_PATH'] == '/pinned/index.sqlite'"
        )
        with mock.patch.dict("os.environ", {"INDEX_PATH": "/pinned/index.sqlite"}):
            self.assertEqual(qmd.index_status(binary).state, "complete")


if __name__ == "__main__":
    unittest.main()
