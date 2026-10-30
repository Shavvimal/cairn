"""``cairn doctor`` exporter lines: spec targets, open parser drift and gaps, warn-only."""

from __future__ import annotations

import io
import json
import os
import time
from contextlib import redirect_stdout
from unittest import mock

from cairn import admin
from cairn.sources.spec_targets import SPEC_TARGETS
from tests.test_shared_store import StoreFixture

CODEX_ID = "019e3000-0000-7000-8000-0000000000c1"
CLAUDE_ID = "c1a0de00-0000-4000-8000-0000000000d1"


class TestDoctorExporter(StoreFixture):
    def test_doctor_prints_spec_targets_drift_and_gaps_warn_only(self):
        drifted = self.codex / f"rollout-2026-10-01T09-00-00-{CODEX_ID}.jsonl"
        drifted.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": CODEX_ID}})
            + "\n"
            + json.dumps({"type": "conversation_item", "payload": {"text": "Orchard"}})
            + "\n"
        )
        self.assertEqual(self.run_cli("codex", "export", "--all", check=False).returncode, 1)
        missing = self.native("claude", CLAUDE_ID)
        stamp = time.time() - 2 * 86400
        for path in (drifted, missing):
            os.utime(path, (stamp, stamp))

        out = io.StringIO()
        with mock.patch.object(admin, "_current_crontab", return_value=[]), redirect_stdout(out):
            admin.doctor_main([])
        text = out.getvalue()

        claude = SPEC_TARGETS["claude"]
        self.assertIn(
            f"  ok   exporter [claude] - adapter 2.0.0, spec target {claude.checked} "
            f"(Claude Code {claude.harness_version})",
            text,
        )
        self.assertNotIn("exporter [cursor]", text)  # cursor is not configured here
        self.assertIn(f"  warn parser drift - codex:{CODEX_ID[:8]} zero-messages: rendered 0", text)
        self.assertIn("; first seen 20", text)
        self.assertIn(
            "  warn gap check [claude-code-sessions] - 1 of 1 sessions in the last 30 days "
            f"have no document: claude:{CLAUDE_ID[:8]}; run 'cairn sync'",
            text,
        )
        self.assertIn(
            "  ok   gap check [codex-sessions] - 1 sessions on disk in the last 30 days, "
            "no unexplained gaps; 1 held by parser drift",
            text,
        )
        for line in text.splitlines():
            if any(label in line for label in ("exporter [", "parser drift", "gap check")):
                self.assertNotIn("FAIL", line)
