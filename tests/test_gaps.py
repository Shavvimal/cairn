"""The trailing-window gap check in ``cairn sync`` (and its read-only scan).

Stores, outputs and the QMD stub live under a temp root; transcripts are invented.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest import mock

from cairn import config, gaps, sync
from cairn.sources.codex_lineage import rollout_id
from cairn.sources.codex_replay import LIVE_CHILD_WINDOW_S
from tests.test_sync import SyncFixture

DAY = 86400
CODEX_ID = "019e2000-0000-7000-8000-0000000000a1"
CLAUDE_ID = "c1a0de00-0000-4000-8000-0000000000b1"


def _age(path: Path, seconds: float) -> Path:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))
    return path


class GapFixture(SyncFixture):
    def window(self) -> tuple[float, float]:
        now = time.time()
        return now - gaps.GAP_WINDOW_DAYS * DAY, now - gaps.GAP_SETTLE_S

    def codex_file(self, sid: str, meta_id: str | None = None, text: str = "Orchard") -> Path:
        """A rollout named for ``sid`` whose ``session_meta`` names ``meta_id``."""
        path = self.native("codex", meta_id or sid, text=text)
        return path.rename(self.codex / f"rollout-2026-10-01T09-00-00-{sid}.jsonl")


class TestEnumeration(GapFixture):
    def test_enumerates_claude_parents_and_children_skips_legacy_agent_files(self):
        parent = _age(self.native("claude", CLAUDE_ID), DAY)
        child = parent.with_suffix("") / "subagents" / "agent-a1b2.jsonl"
        child.parent.mkdir(parents=True)
        child.write_text('{"type":"user"}\n')
        _age(child, DAY)
        _age(self.native("claude", "agent-legacy"), DAY)
        found = gaps.claude_sessions(self.claude, *self.window())
        self.assertEqual(set(found), {CLAUDE_ID, "agent-a1b2"})

    def test_codex_id_from_filename(self):
        self.assertEqual(
            rollout_id(Path(f"rollout-2026-10-01T09-00-00-{CODEX_ID}.jsonl")), CODEX_ID
        )
        self.assertEqual(rollout_id(Path("rollout-plain.jsonl")), "rollout-plain")
        _age(self.codex_file(CODEX_ID), DAY)
        self.assertEqual(set(gaps.codex_sessions(self.codex, *self.window())), {CODEX_ID})

    def test_settle_window_excludes_live_files(self):
        self.assertEqual(gaps.GAP_SETTLE_S, LIVE_CHILD_WINDOW_S)
        live = self.codex_file(CODEX_ID)  # written just now
        old = _age(self.codex_file(CODEX_ID.replace("a1", "a2")), 31 * DAY)
        settled = _age(self.codex_file(CODEX_ID.replace("a1", "a3")), DAY)
        found = gaps.codex_sessions(self.codex, *self.window())
        self.assertEqual(set(found.values()), {settled})
        self.assertNotIn(live, found.values())
        self.assertNotIn(old, found.values())


class TestGapHeal(GapFixture):
    def test_missing_session_is_healed_and_second_pass_is_clean(self):
        # Two days old: outside the default 1d sync window, so only the heal sees it.
        _age(self.codex_file(CODEX_ID), 2 * DAY)
        _age(self.native("claude", CLAUDE_ID), 2 * DAY)
        result = self.sync_cron()
        log = self.log_text
        self.assertEqual(result.returncode, 0, log)
        self.assertIn("[gap-check] codex-sessions: 1 on disk in 30d, 1 missing, 0 held", log)
        self.assertIn("[gap-heal:codex-sessions] ok", log)
        self.assertIn("[gap-check] codex-sessions: after heal 0 missing", log)
        self.assertIn("[gap-check] claude-code-sessions: after heal 0 missing", log)
        self.assertLess(log.index("[gap-check]"), log.index("[qmd-collections]"))
        self.assertEqual(gaps.scan(config.get_config(), "codex").missing, [])
        # The next run finds nothing to heal.
        self.sync_cron()
        self.assertEqual(self.log_text.count("[gap-heal:codex-sessions]"), 1)

    def test_still_missing_fails_the_named_step(self):
        # The filename names one thread, session_meta another: heal cannot close it.
        _age(self.codex_file(CODEX_ID, meta_id="other-thread"), 2 * DAY)
        result = self.sync_cron()
        log = self.log_text
        self.assertEqual(result.returncode, 1, log)
        self.assertIn(f"after heal 1 missing: codex:{CODEX_ID[:8]}", log)
        self.assertIn("failed steps: gap-check:codex-sessions", log)
        self.assertIn("=== FAILED ===", log)

    def test_drift_held_ids_are_not_missing(self):
        path = self.codex / f"rollout-2026-10-01T09-00-00-{CODEX_ID}.jsonl"
        path.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": CODEX_ID}})
            + "\n"
            + json.dumps({"type": "conversation_item", "payload": {"text": "Orchard"}})
            + "\n"
        )
        _age(path, 2 * DAY)
        # Outside the 1d sync window the drift is reported, but fails no step.
        self.assertEqual(self.sync_cron().returncode, 0, self.log_text)
        log = self.log_text
        self.assertIn("[gap-check] codex-sessions: 1 on disk in 30d, 0 missing, 1 held", log)
        self.assertNotIn("[gap-heal:codex-sessions]", log)
        self.assertIn(f"[exporter-drift] 1 open findings: codex:{CODEX_ID[:8]} zero-messages", log)

    def test_agy_scan_uses_the_index_time_the_heal_filters_on(self):
        def stamp(days: float) -> str:
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * DAY))

        def age_logs(sid: str, seconds: float) -> None:
            for transcript in (self.antigravity / "brain" / sid).rglob("transcript*.jsonl"):
                _age(transcript, seconds)

        # Last turn 40 days ago, transcript touched 2 days ago: the --since 30d heal
        # filters on the index time and skips it, so the scan must not count it.
        stale = "a9e00000-0000-4000-8000-0000000000c1"
        self.native("antigravity", stale, stamps=[stamp(40)])
        age_logs(stale, 2 * DAY)
        # Last turn 2 days ago, transcript mtime 40 days old: missing, and healed.
        fresh = "a9e00000-0000-4000-8000-0000000000c2"
        self.native("antigravity", fresh, stamps=[stamp(2)])
        age_logs(fresh, 40 * DAY)
        self.assertEqual(gaps.scan(config.get_config(), "antigravity").missing, [fresh])
        self.run_cli("antigravity", "export", "--since", "30d", "-q")
        self.assertEqual(gaps.scan(config.get_config(), "antigravity").missing, [])

    def test_hook_mode_skips_gap_check(self):
        _age(self.codex_file(CODEX_ID), 2 * DAY)
        self.assertEqual(self.run_cli("sync", "--hook", check=False).returncode, 0, self.log_text)
        self.assertNotIn("[gap-check]", self.log_text)
        self.assertNotIn("[exporter-drift]", self.log_text)

    def test_no_heal_subprocess_when_nothing_missing(self):
        _age(self.codex_file(CODEX_ID), 2 * DAY)
        self.run_cli("codex", "export", "--all")
        failed: list[str] = []

        def record(name: str, ok: bool) -> None:
            if not ok:
                failed.append(name)

        with mock.patch.object(sync, "run_step") as run_step:
            sync.gap_check(config.get_config(), "codex", record)
        run_step.assert_not_called()
        self.assertEqual(failed, [])
        self.assertIn("[gap-check] codex-sessions: 1 on disk in 30d, 0 missing", self.log_text)
