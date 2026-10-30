"""Withdrawal: ``forget`` strikes a decision row and records its fingerprint; ``decide``
refuses a withdrawn claim; readers hide withdrawn rows.

Every test runs in a temp home (``StoreFixture``). Documents are built with the real
renderer, as in ``tests.test_decisions``.
"""

from __future__ import annotations

import gc
import importlib
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from typing import Any
from unittest import mock

from cairn import cli, config, decisions, recall, withdrawals
from cairn.decisions import HEADER, SEPARATOR
from cairn.withdrawals import fingerprint, normalise_claim
from tests.test_decisions import SID, doc
from tests.test_recall_digest import document, msg
from tests.test_shared_store import StoreFixture

SOURCES = {
    "claude": ("claude-code-sessions", "ClaudeSource"),
    "codex": ("codex-sessions", "CodexSource"),
}
TS = "2026-10-30T14:02:11Z"
ROWS = (
    "| 1 | Use Postgres 16 for the queue. | decision | high | 2026-10-28 |  |  |\n"
    "| 2 | The digest is byte-identical | fact | high | 2026-10-28 |  |  |\n"
    "| 3 | Keep three words | decision | medium | 2026-10-29 | 2026-10-30 |  |\n"
    "| 4 | ~~Old plan~~ superseded by #5 | decision | low | 2026-10-29 |  |  |\n"
    "| 5 | New plan | decision | low | 2026-10-29 |  | supersedes #4 |"
)
FINGERPRINTS = (
    (
        "Use Postgres 16 for the queue.",
        "use postgres 16 for the queue",
        "36e5348b0c921d29e9974b20311175686f38dba3b3bc0890f976264f2a20ece5",
    ),
    (
        "  use  POSTGRES 16, for the queue!! ",
        "use postgres 16 for the queue",
        "36e5348b0c921d29e9974b20311175686f38dba3b3bc0890f976264f2a20ece5",
    ),
    (
        "The API's rate-limit is 100 req/s",
        "the apis ratelimit is 100 reqs",
        "e84d7d5a98c5d452bdd0e621c85c5c8f93205a375b7679cb12f5337aa51ff067",
    ),
    (
        "`cairn sync` runs every 15 minutes",
        "cairn sync runs every 15 minutes",
        "185bfafd13790869e0095dd0a230e4dfbe847eb098c44a50c4a22cd4509d44a1",
    ),
)


class TestFingerprint(unittest.TestCase):
    def test_normalise_claim_examples(self):
        for claim, norm, digest in FINGERPRINTS:
            with self.subTest(claim=claim):
                self.assertEqual(normalise_claim(claim), norm)
                self.assertEqual(fingerprint(claim), digest)
        # Full-width "Use" folds to "use" under NFKC.
        self.assertEqual(normalise_claim("\uff35\uff53\uff45 Postgres"), "use postgres")

    def test_normalise_keeps_symbols(self):
        self.assertNotEqual(fingerprint("Use C++"), fingerprint("Use C"))
        # Math symbols stay; % is Unicode punctuation (Po), so it goes like a comma.
        self.assertEqual(normalise_claim("a < b | 5% ~~x~~"), "a < b | 5 x")

    def test_fingerprint_rejects_empty_claim(self):
        with self.assertRaises(ValueError):
            fingerprint("!!! ...")


class WithdrawalFixture(StoreFixture):
    def setUp(self):
        super().setUp()
        self.data = self.root / "data"
        self.ledger = self.data / "withdrawn.jsonl"
        self.patch_now = mock.patch.object(withdrawals, "_now", return_value=TS)
        self.patch_now.start()
        self.addCleanup(self.patch_now.stop)

    def write_doc(self, name: str, text: str, source: str = "claude") -> Path:
        folder = self.data / SOURCES[source][0]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(text.encode("utf-8"))
        return path

    def run_verb(self, *argv: str, source: str = "claude") -> tuple[int, str, str]:
        """Run ``cairn <source> <argv>`` in process, ``since`` pinned to 2026-10-30."""
        module = importlib.reload(importlib.import_module(f"cairn.sources.{source}"))
        instance = getattr(module, SOURCES[source][1])()
        gc.collect()  # a finaliser warning from an earlier test must not land in ``err``
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(decisions, "today", return_value=date(2026, 10, 30)),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            try:
                args = cli.build_parser(instance).parse_args(list(argv))
                code = args.func(args)
            except SystemExit as exc:
                code = exc.code
            finally:
                instance.flush_catalog()
        return code, out.getvalue(), err.getvalue()

    def recall(self, *argv: str) -> tuple[int, Any, str]:
        gc.collect()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = recall.main(list(argv))
        text = out.getvalue()
        return code, (json.loads(text) if "--json" in argv and text else text), err.getvalue()

    def records(self) -> list[dict]:
        return [json.loads(line) for line in self.ledger.read_text().splitlines()]

    def seed(self, *entries: dict) -> None:
        self.data.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a") as handle:
            for entry in entries:
                handle.write(json.dumps(entry) + "\n")

    def entry(self, claim: str, event: str = "withdrawn", reason: str | None = "stale") -> dict:
        return withdrawals.record(
            event,
            fingerprint=fingerprint(claim),
            source="claude",
            session_id=SID,
            row=1,
            reason=reason,
        )


class TestForget(WithdrawalFixture):
    def test_forget_strikes_row_and_appends_one_record(self):
        path = self.write_doc("a.md", doc(rows=ROWS))
        before = path.read_text()
        code, out, err = self.run_verb("forget", "1", "--reason", "stale", "--session-id", SID)
        self.assertEqual(
            (code, out, err), (0, "Withdrew row #1 in claude:3f2a9c1e (reason: stale)\n", "")
        )
        old = ROWS.splitlines()[0]
        new = "| 1 | ~~Use Postgres 16 for the queue.~~ withdrawn: stale | decision | high | 2026-10-28 |  |  |"
        self.assertEqual(path.read_text(), before.replace(old, new, 1))
        self.assertEqual(
            self.records(),
            [
                {
                    "v": 1,
                    "ts": TS,
                    "event": "withdrawn",
                    "source": "claude",
                    "session_id": SID,
                    "row": 1,
                    "fingerprint": FINGERPRINTS[0][2],
                    "norm": 1,
                    "reason": "stale",
                }
            ],
        )
        self.assertEqual(list(json.loads(self.ledger.read_text())), list(withdrawals.FIELDS))
        row = decisions.parse(path.read_text())[0]
        self.assertEqual((row.state, row.withdrawn_reason), ("struck", "stale"))

    def test_ledger_record_has_no_claim_text(self):
        self.write_doc("a.md", doc(rows=ROWS))
        self.run_verb("forget", "2", "--reason", "private", "--session-id", SID)
        data = self.ledger.read_bytes()
        for text in (b"digest", b"byte-identical", b"The digest is byte-identical", b"title", b"/"):
            self.assertNotIn(text, data)

    def test_ledger_mode_is_0600(self):
        self.write_doc("a.md", doc(rows=ROWS))
        self.run_verb("forget", "2", "--reason", "stale", "--session-id", SID)
        self.assertEqual(self.ledger.stat().st_mode & 0o777, 0o600)

    def test_forget_twice_appends_once(self):
        path = self.write_doc("a.md", doc(rows=ROWS))
        self.run_verb("forget", "2", "--reason", "wrong_source", "--session-id", SID)
        after = path.read_bytes()
        code, out, err = self.run_verb("forget", "2", "--reason", "stale", "--session-id", SID)
        self.assertEqual(
            (code, out, err),
            (0, "Row #2 in claude:3f2a9c1e is already withdrawn (reason: wrong_source)\n", ""),
        )
        self.assertEqual(path.read_bytes(), after)
        self.assertEqual(len(self.records()), 1)

    def test_forget_repairs_unstruck_row_after_crash(self):
        path = self.write_doc("a.md", doc(rows=ROWS))
        self.seed(self.entry("Use Postgres 16 for the queue.", reason="hallucination"))
        code, _, _ = self.run_verb("forget", "1", "--reason", "hallucination", "--session-id", SID)
        self.assertEqual(code, 0)
        self.assertIn(
            "~~Use Postgres 16 for the queue.~~ withdrawn: hallucination", path.read_text()
        )
        self.assertEqual(len(self.records()), 1)

    def test_forget_superseded_row_keeps_supersede_suffix(self):
        path = self.write_doc("a.md", doc(rows=ROWS))
        code, _, _ = self.run_verb("forget", "4", "--reason", "wrong_source", "--session-id", SID)
        self.assertEqual(code, 0)
        self.assertIn(
            "| 4 | ~~Old plan~~ superseded by #5, withdrawn: wrong_source | decision |",
            path.read_text(),
        )
        row = decisions.parse(path.read_text())[3]
        self.assertEqual((row.state, row.withdrawn_reason), ("struck", "wrong_source"))

    def test_forget_round_trips_claims_at_markdown_boundaries(self):
        rows = (
            "| 1 | Store files in ~ | decision | high | 2026-10-28 |  |  |\n"
            "| 2 | ~/cache is the root | decision | high | 2026-10-28 |  |  |\n"
            "| 3 | Pipe a \\| b ends in \\\\ | decision | high | 2026-10-28 |  |  |\n"
            "| 4 | ~~Paths under ~~~ superseded by #1 | decision | low | 2026-10-28 |  |  |"
        )
        path = self.write_doc("a.md", doc(rows=rows))
        before = decisions.parse(path.read_text())
        self.assertEqual(
            [r.claim for r in before],
            ["Store files in ~", "~/cache is the root", "Pipe a | b ends in \\", "Paths under ~"],
        )
        for number in ("1", "2", "3", "4"):
            with self.subTest(row=number):
                code, _, err = self.run_verb(
                    "forget", number, "--reason", "stale", "--session-id", SID
                )
                self.assertEqual((code, err), (0, ""))
        self.assertIn("| 1 | ~~Store files in ~~~ withdrawn: stale |", path.read_text())
        after = decisions.parse(path.read_text())
        self.assertEqual([r.claim for r in after], [r.claim for r in before])
        self.assertEqual([r.withdrawn_reason for r in after], ["stale"] * 4)
        self.assertEqual(after[3].suffix, "superseded by #1, withdrawn: stale")
        # The table still takes writes, and the struck claim is refused.
        self.assertEqual(self.run_verb("decide", "Store files in ~", "--session-id", SID)[0], 3)
        self.assertEqual(self.run_verb("decide", "Next", "--session-id", SID)[0], 0)

    def test_forget_refuses_a_strike_that_does_not_read_back(self):
        path = self.write_doc("a.md", doc(rows=ROWS))
        before = path.read_bytes()
        with mock.patch.object(
            decisions, "strike_withdrawn", return_value=decisions.DecisionRow(1, "x~~y")
        ):
            code, _, err = self.run_verb("forget", "1", "--reason", "stale", "--session-id", SID)
        self.assertEqual(
            (code, err),
            (1, "Error: row #1 cannot be struck in a form that reads back; edit it by hand\n"),
        )
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.ledger.exists())

    def test_forget_unknown_row_exits_1(self):
        path = self.write_doc("a.md", doc(rows=ROWS))
        before = path.read_bytes()
        code, _, err = self.run_verb("forget", "9", "--reason", "stale", "--session-id", SID)
        self.assertEqual(
            (code, err), (1, "Error: no decision row #9 in claude:3f2a9c1e (rows: 1-5)\n")
        )
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.ledger.exists())
        self.write_doc("b.md", doc("bbbbbbbb-0002", rows=None))
        code, _, err = self.run_verb("forget", "1", "--reason", "stale", "--session-id", "bbbbbbbb")
        self.assertEqual((code, err), (1, "Error: no decisions table in claude:bbbbbbbb\n"))

    def test_forget_row_zero_exits_2(self):
        self.write_doc("a.md", doc(rows=ROWS))
        code, _, err = self.run_verb("forget", "0", "--reason", "stale", "--session-id", SID)
        self.assertEqual((code, err), (2, "Error: row number must be 1 or greater, got 0\n"))

    def test_forget_reason_outside_vocabulary_exits_2(self):
        self.write_doc("a.md", doc(rows=ROWS))
        for argv in (("--reason", "because"), ()):
            with self.subTest(argv=argv):
                code, _, err = self.run_verb("forget", "1", *argv, "--session-id", SID)
                self.assertEqual(code, 2)
                self.assertIn("--reason", err)
        self.assertFalse(self.ledger.exists())

    def test_forget_private_prints_not_erased_note(self):
        path = self.write_doc("a.md", doc(rows=ROWS))
        code, _, err = self.run_verb("forget", "2", "--reason", "private", "--session-id", SID)
        self.assertEqual(code, 0)
        self.assertEqual(
            err,
            f"Note: withdrawn, not erased: the claim text stays in {path} "
            "and in the QMD index until you edit the file.\n",
        )

    def test_forget_empty_claim_after_normalisation_exits_1(self):
        rows = "| 1 | ... !!! | decision | high | 2026-10-28 |  |  |"
        self.write_doc("a.md", doc(rows=rows))
        code, _, err = self.run_verb("forget", "1", "--reason", "other", "--session-id", SID)
        self.assertEqual(
            (code, err),
            (1, "Error: row #1 has an empty claim after normalisation; edit it by hand\n"),
        )

    def test_forget_json(self):
        path = self.write_doc("a.md", doc(rows=ROWS))
        code, out, _ = self.run_verb(
            "forget", "1", "--reason", "stale", "--json", "--session-id", SID
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(out),
            {
                "ref": f"claude:{SID}",
                "row": 1,
                "reason": "stale",
                "fingerprint": FINGERPRINTS[0][2],
                "path": str(path),
                "ledger": str(self.ledger),
                "already_withdrawn": False,
                "appended": True,
            },
        )

    def test_unwritable_data_root_leaves_document_unchanged(self):
        path = self.write_doc("a.md", doc(rows=ROWS))
        before = path.read_bytes()
        with mock.patch.object(withdrawals, "_append", side_effect=PermissionError("denied")):
            code, _, err = self.run_verb("forget", "1", "--reason", "stale", "--session-id", SID)
        self.assertEqual((code, err), (1, "Error: denied\n"))
        self.assertEqual(path.read_bytes(), before)

    def test_short_write_fails_and_leaves_document_unchanged(self):
        path = self.write_doc("a.md", doc(rows=ROWS))
        before = path.read_bytes()
        with mock.patch.object(withdrawals.os, "write", return_value=3):
            code, _, err = self.run_verb("forget", "1", "--reason", "stale", "--session-id", SID)
        self.assertEqual(code, 1)
        self.assertRegex(err, r"^Error: short write to .*withdrawn\.jsonl: 3 of [0-9]+ bytes\n$")
        self.assertEqual(path.read_bytes(), before)

    def test_append_fsyncs_file_then_directory_on_create(self):
        calls: list[str] = []
        real_fsync, real_write = os.fsync, os.write

        def fsync(fd: int) -> None:
            calls.append("dir" if os.path.isdir(f"/dev/fd/{fd}") else "file")
            real_fsync(fd)

        def write(fd: int, data: bytes) -> int:
            calls.append("write")
            return real_write(fd, data)

        entry = self.entry("Use Postgres 16 for the queue.")
        self.data.mkdir(parents=True, exist_ok=True)
        with (
            mock.patch.object(withdrawals.os, "fsync", side_effect=fsync),
            mock.patch.object(withdrawals.os, "write", side_effect=write),
        ):
            withdrawals._append(self.ledger, entry)
            self.assertEqual(calls, ["write", "file", "dir"])
            calls.clear()
            withdrawals._append(self.ledger, entry)
            self.assertEqual(calls, ["write", "file"])
        self.assertEqual(len(self.records()), 2)


class TestGuard(WithdrawalFixture):
    def forget_first_row(self, reason: str = "hallucination") -> Path:
        path = self.write_doc("a.md", doc(rows=ROWS))
        self.assertEqual(
            self.run_verb("forget", "1", "--reason", reason, "--session-id", SID)[0], 0
        )
        return path

    def test_decide_refuses_withdrawn_claim_exit_3(self):
        path = self.forget_first_row()
        before = path.read_bytes()
        code, out, err = self.run_verb(
            "decide", "Use Postgres 16 for the queue.", "--session-id", SID
        )
        self.assertEqual(
            (code, out, err),
            (
                3,
                "",
                "Error: claim withdrawn on 2026-10-30 (reason: hallucination, claude:3f2a9c1e "
                "row #1); pass --force to add it again\n",
            ),
        )
        self.assertEqual(path.read_bytes(), before)
        # supersede refuses a withdrawn replacement claim and strikes nothing.
        code, _, _ = self.run_verb(
            "supersede", "2", "use postgres 16 for the queue", "--session-id", SID
        )
        self.assertEqual(code, 3)
        self.assertEqual(path.read_bytes(), before)

    def test_decide_refuses_case_and_punctuation_variant(self):
        self.forget_first_row()
        code, _, _ = self.run_verb(
            "decide", "  use  POSTGRES 16,", "for the queue!!", "--session-id", SID
        )
        self.assertEqual(code, 3)
        self.assertEqual(self.run_verb("decide", "Use Postgres 17", "--session-id", SID)[0], 0)

    def test_decide_refuses_across_sources(self):
        self.forget_first_row()
        codex_id = "019a0000-0000-7000-8000-00000000c0de"
        text = document(codex_id, [msg("user", "2026-10-29T10:00:00Z", "hi")], source="codex")
        self.write_doc("c.md", text, source="codex")
        code, _, err = self.run_verb(
            "decide", "Use Postgres 16 for the queue", "--session-id", codex_id, source="codex"
        )
        self.assertEqual(code, 3, err)
        self.assertIn("claude:3f2a9c1e row #1", err)

    def test_decide_force_appends_unwithdrawn_and_adds_row(self):
        path = self.forget_first_row()
        code, out, err = self.run_verb(
            "decide", "Use Postgres 16 for the queue", "--force", "--session-id", SID
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "Added #6 to claude:3f2a9c1e: Use Postgres 16 for the queue\n")
        self.assertEqual(
            err,
            "Warning: adding a withdrawn claim (reason was: hallucination); recorded as unwithdrawn\n",
        )
        last = self.records()[-1]
        self.assertEqual(
            (last["event"], last["reason"], last["row"], last["session_id"]),
            ("unwithdrawn", None, 6, SID),
        )
        self.assertEqual(
            decisions.parse(path.read_text())[-1].claim, "Use Postgres 16 for the queue"
        )
        # The claim is no longer withdrawn; --force on a free claim appends nothing.
        self.assertEqual(
            self.run_verb("decide", "Use Postgres 16 for the queue", "--session-id", SID)[0], 0
        )
        self.assertEqual(
            self.run_verb("decide", "Free claim", "--force", "--session-id", SID)[0], 0
        )
        self.assertEqual(len(self.records()), 2)

    def test_failed_forced_write_leaves_claim_withdrawn(self):
        path = self.forget_first_row()
        before = path.read_bytes()
        with mock.patch.object(decisions, "atomic_write", side_effect=PermissionError("denied")):
            code, out, err = self.run_verb(
                "decide", "Use Postgres 16 for the queue", "--force", "--session-id", SID
            )
        self.assertEqual((code, out, err), (1, "", "Error: denied\n"))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual([r["event"] for r in self.records()], ["withdrawn"])
        self.assertEqual(
            self.run_verb("decide", "Use Postgres 16 for the queue", "--session-id", SID)[0], 3
        )

    def test_failed_unwithdrawn_append_keeps_claim_withdrawn(self):
        path = self.forget_first_row()
        with mock.patch.object(withdrawals, "_append", side_effect=PermissionError("denied")):
            code, out, err = self.run_verb(
                "decide", "Use Postgres 16 for the queue", "--force", "--session-id", SID
            )
        self.assertEqual(
            (code, out, err),
            (
                1,
                "",
                "Error: added #6 but could not record it as unwithdrawn: denied; "
                "the claim stays withdrawn\n",
            ),
        )
        self.assertEqual(decisions.parse(path.read_text())[-1].number, 6)
        self.assertEqual([r["event"] for r in self.records()], ["withdrawn"])
        # The new row is hidden through ledger evidence.
        _, data, _ = self.recall("decisions", "--all-projects", "--json")
        self.assertNotIn(f"claude:{SID}#6", [r["ref"] for r in data["decisions"]])

    def test_forced_guard_holds_ledger_lock_until_commit(self):
        self.forget_first_row()
        seen: list[bool] = []
        real_write = decisions.atomic_write

        def write(path: Path, text: str) -> None:
            lock = withdrawals.ledger_lock(self.ledger)
            probe = subprocess.run(
                [sys.executable, "-c", PROBE, str(lock)], capture_output=True, text=True
            )
            seen.append(probe.stdout.strip() == "busy")
            real_write(path, text)

        with mock.patch.object(decisions, "atomic_write", side_effect=write):
            code, _, _ = self.run_verb(
                "decide", "Use Postgres 16 for the queue", "--force", "--session-id", SID
            )
        self.assertEqual((code, seen), (0, [True]))
        self.assertEqual([r["event"] for r in self.records()], ["withdrawn", "unwithdrawn"])

    def test_withdraw_after_unwithdraw_refuses_again(self):
        self.forget_first_row()
        self.run_verb("decide", "Use Postgres 16 for the queue", "--force", "--session-id", SID)
        self.assertEqual(
            self.run_verb("forget", "6", "--reason", "stale", "--session-id", SID)[0], 0
        )
        self.assertEqual(
            [r["event"] for r in self.records()], ["withdrawn", "unwithdrawn", "withdrawn"]
        )
        code, _, err = self.run_verb("decide", "use postgres 16 for the queue", "--session-id", SID)
        self.assertEqual(code, 3)
        self.assertIn("(reason: stale, claude:3f2a9c1e row #6)", err)


class TestReading(WithdrawalFixture):
    def setUp(self):
        super().setUp()
        self.path = self.write_doc("a.md", doc(rows=ROWS))
        self.run_verb("forget", "2", "--reason", "wrong_source", "--session-id", SID)

    def decisions(self, *argv: str) -> tuple[int, Any, str]:
        return self.recall("decisions", "--all-projects", "--as-of", "2026-10-31", *argv)

    def test_recall_decisions_hides_withdrawn_by_default(self):
        _, data, _ = self.decisions("--json")
        self.assertEqual([r["number"] for r in data["decisions"]], [1, 3, 5])

    def test_recall_decisions_include_withdrawn_marks_rows(self):
        _, text, _ = self.decisions("--include-struck")
        self.assertIn(
            "claude:3f2a9c1e#2  fact  high  2026-10-28  revisit -  "
            "~~The digest is byte-identical~~ [withdrawn: wrong_source]\n",
            text,
        )
        self.assertTrue(text.endswith("5 decisions\n"))

    def test_recall_decisions_json_fields(self):
        _, data, _ = self.decisions("--json", "--include-struck")
        fields = {
            r["number"]: (r["withdrawn"], r["withdrawn_reason"], r["withdrawn_via"])
            for r in data["decisions"]
        }
        self.assertEqual(fields[1], (False, None, None))
        self.assertEqual(fields[2], (True, "wrong_source", "row"))
        self.assertEqual(fields[4], (False, None, None))  # superseded, not withdrawn

    def test_unstruck_row_hidden_by_ledger_evidence(self):
        # The same claim, written in another session before the withdrawal.
        other = "dddddddd-0004"
        rows = "| 1 | the Digest is byte-identical. | fact | high | 2026-10-20 | 2026-10-21 |  |"
        self.write_doc("d.md", doc(other, rows=rows))
        _, data, _ = self.decisions("--json")
        self.assertNotIn(f"claude:{other}#1", [r["ref"] for r in data["decisions"]])
        _, data, _ = self.decisions("--json", "--include-struck")
        row = next(r for r in data["decisions"] if r["ref"] == f"claude:{other}#1")
        self.assertEqual(
            (row["state"], row["withdrawn"], row["withdrawn_reason"], row["withdrawn_via"]),
            ("active", True, "wrong_source", "ledger"),
        )
        self.assertFalse(row["overdue"])
        _, text, _ = self.decisions("--include-struck")
        self.assertIn("the Digest is byte-identical.  [withdrawn: wrong_source]\n", text)
        _, data, _ = self.decisions("--json", "--overdue", "--include-struck")
        self.assertNotIn(f"claude:{other}#1", [r["ref"] for r in data["decisions"]])

    def test_digest_hides_withdrawn_rows_by_default(self):
        other = "eeeeeeee-0005"
        rows = (
            "| 1 | the Digest is byte-identical. | fact | high | 2026-10-20 | 2026-10-21 |  |\n"
            "| 2 | Still due | fact | high | 2026-10-20 | 2026-10-21 |  |"
        )
        messages = [msg("user", "2026-10-30T10:00:00Z", "x"), msg("assistant", None, "y")]
        self.write_doc("e.md", document(other, messages, notes=_notes(rows)))
        out = self.root / "out"
        code, data, _ = self.recall(
            "digest", "2026-10-30", "--all-projects", "--json", "--out", str(out)
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            [r["ref"] for r in data["overdue"]], [f"claude:{other}#2", f"claude:{SID}#3"]
        )
        self.assertTrue(all(r["withdrawn"] is False for r in data["overdue"]))

    def test_bad_ledger_line_fails_loud_with_line_number(self):
        with self.ledger.open("a") as handle:
            handle.write('{"v": 1}\n')
        code, _, err = self.decisions("--json")
        self.assertEqual(code, 1)
        self.assertEqual(
            err,
            f"Error: {self.ledger} line 2 is not a valid withdrawal record: missing ts, event, "
            "source, session_id, row, fingerprint, norm, reason; fix or remove the line\n",
        )
        code, _, err = self.run_verb("decide", "Anything", "--session-id", SID)
        self.assertEqual(code, 1)
        self.assertIn("line 2 is not a valid withdrawal record", err)
        good = json.dumps(self.records()[0])
        for line, problem in (
            ("[1]", "not a JSON object"),
            ("not json", "not valid JSON"),
            (good.replace('"withdrawn"', '"hidden"'), "unknown event 'hidden'"),
            (good.replace('"wrong_source"', '"because"'), "unknown reason 'because'"),
            (_with(good, fingerprint=FP + "\n"), "fingerprint must be 64 lowercase hex"),
            (_with(good, ts=TS + "\n"), "ts must be YYYY-MM-DDTHH:MM:SSZ"),
            (_with(good, ts="2026-99-99T99:99:99Z"), "ts must be YYYY-MM-DDTHH:MM:SSZ"),
            (_with(good, v=1.0), "unknown record version 1.0"),
            (_with(good, v=True), "unknown record version True"),
            (_with(good, norm=1.0), "unknown normalisation version 1.0"),
            (
                _with(good, event="unwithdrawn"),
                "reason must be null on an unwithdrawn record, got 'wrong_source'",
            ),
        ):
            with self.subTest(line=line):
                self.ledger.write_text(good + "\n" + line + "\n")
                code, _, err = self.decisions("--json")
                self.assertEqual(code, 1)
                self.assertIn(f"line 2 is not a valid withdrawal record: {problem}", err)

    def test_torn_tail_is_ignored_then_truncated_on_append(self):
        whole = self.ledger.read_bytes()
        self.ledger.write_bytes(whole + b'{"v": 1, "ts": "2026')
        code, data, err = self.decisions("--json")
        self.assertEqual(code, 0)
        self.assertEqual(err, f"Warning: {self.ledger} ends with an unterminated line; ignored\n")
        self.assertEqual([r["number"] for r in data["decisions"]], [1, 3, 5])
        code, _, err = self.run_verb("forget", "3", "--reason", "stale", "--session-id", SID)
        self.assertEqual(code, 0)
        self.assertIn("unterminated line", err)
        lines = self.ledger.read_bytes().split(b"\n")
        self.assertEqual(lines[-1], b"")
        self.assertEqual(b"\n".join(lines[:1]) + b"\n", whole)
        self.assertEqual([r["row"] for r in self.records()], [2, 3])


FP = FINGERPRINTS[0][2]
# Exits 0 printing "busy" when another process holds the flock on argv[1].
PROBE = (
    "import fcntl, sys\n"
    "h = open(sys.argv[1], 'a')\n"
    "try:\n"
    "    fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    "    print('free')\n"
    "except BlockingIOError:\n"
    "    print('busy')\n"
)


def _with(line: str, **changes: object) -> str:
    return json.dumps({**json.loads(line), **changes})


def _section(text: str) -> str:
    """The ``## Decisions`` section: from its heading to the next heading."""
    start = text.index("## Decisions\n")
    end = text.find("\n## ", start + 1)
    return text[start:end]


def _notes(rows: str) -> str:
    return f"## My Notes\n\nmine\n\n## Decisions\n\n{HEADER}\n{SEPARATOR}\n{rows}"


class TestProcesses(StoreFixture):
    """Real ``python -m cairn`` processes against the temp home."""

    def export_with_table(self, sid: str) -> None:
        self.native("codex", sid)
        self.run_cli("codex", "export", "--all")
        self.run_cli("codex", "decide", f"Claim number {sid}", "--session-id", sid)

    def test_strike_survives_reexport(self):
        self.export_with_table("reexport")
        self.run_cli("codex", "forget", "1", "--reason", "stale", "--session-id", "reexport")
        path = next((self.root / "data" / "codex-sessions").rglob("*.md"))
        struck = path.read_text()
        self.run_cli("codex", "export", "--all")
        self.assertIn(
            "| 1 | ~~Claim number reexport~~ withdrawn: stale | decision |", path.read_text()
        )
        self.assertEqual(_section(path.read_text()), _section(struck))
        refused = self.run_cli(
            "codex", "decide", "claim number REEXPORT", "--session-id", "reexport", check=False
        )
        self.assertEqual(refused.returncode, 3, refused.stderr)

    def spawn(self, *argv: str) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-m", "cairn", "codex", *argv],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def test_concurrent_forgets_of_one_claim_append_once(self):
        sids = [f"same-{n}" for n in range(6)]
        for sid in sids:
            self.native("codex", sid)
        self.run_cli("codex", "export", "--all")
        for sid in sids:
            self.run_cli("codex", "decide", "One shared claim", "--session-id", sid)
        processes = [
            self.spawn("forget", "1", "--reason", "stale", "--session-id", sid) for sid in sids
        ]
        for process in processes:
            _out, err = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, err)
        ledger = config.get_config().data_root / "withdrawn.jsonl"
        self.assertEqual(len(ledger.read_text().splitlines()), 1)
        for path in (self.root / "data" / "codex-sessions").rglob("*.md"):
            self.assertIn("~~One shared claim~~ withdrawn: stale", path.read_text())

    def test_concurrent_forgets_in_one_document_keep_both_strikes(self):
        self.native("codex", "one-doc")
        self.run_cli("codex", "export", "--all")
        for n in range(1, 7):
            self.run_cli("codex", "decide", f"Claim {n}", "--session-id", "one-doc")
        processes = [
            self.spawn("forget", str(n), "--reason", "other", "--session-id", "one-doc")
            for n in range(1, 7)
        ]
        for process in processes:
            _out, err = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, err)
        path = next((self.root / "data" / "codex-sessions").rglob("*.md"))
        rows = decisions.parse(path.read_text())
        self.assertEqual([r.withdrawn_reason for r in rows], ["other"] * 6)
        ledger = config.get_config().data_root / "withdrawn.jsonl"
        self.assertEqual(len(ledger.read_text().splitlines()), 6)

    def test_concurrent_forgets_write_whole_lines(self):
        sids = [f"race-{n}" for n in range(8)]
        for sid in sids:
            self.export_with_table(sid)
        processes = [
            subprocess.Popen(
                [sys.executable, "-m", "cairn", "codex", "forget", "1", "--reason", "other",
                 "--session-id", sid],
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for sid in sids
        ]  # fmt: skip
        for process in processes:
            _out, err = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, err)
        ledger = config.get_config().data_root / "withdrawn.jsonl"
        lines = ledger.read_text().splitlines()
        self.assertEqual(len(lines), 8)
        records = [json.loads(line) for line in lines]
        self.assertEqual(sorted(r["session_id"] for r in records), sorted(sids))
        self.assertEqual(
            {r["fingerprint"] for r in records},
            {fingerprint(f"Claim number {sid}") for sid in sids},
        )
        self.assertEqual(os.stat(ledger).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
