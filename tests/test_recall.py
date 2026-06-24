"""``cairn recall`` - date-range parsing + list/expand over a tmp JSONL fixture."""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from cairn import config, recall
from cairn.timeutil import parse_date_range


class TestParseDateRange(unittest.TestCase):
    def test_accepts_the_full_grammar(self):
        for expr in (
            "today",
            "yesterday",
            "2026-01-15",
            "3 days ago",
            "last 3 days",
            "this week",
            "last week",
            "last monday",
        ):
            start, end = parse_date_range(expr)
            self.assertLess(start, end, expr)
            self.assertEqual(start.tzinfo, UTC)

    def test_single_day_windows_span_one_day(self):
        start, end = parse_date_range("today")
        self.assertEqual(end - start, timedelta(days=1))

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            parse_date_range("whenever")


class _RecallFixture(unittest.TestCase):
    """Points CAIRN_CONFIG at a temp config whose claude store holds one session."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        store = root / "projects"
        (store / "-proj-demo").mkdir(parents=True)
        now = datetime.now(UTC).replace(microsecond=0)
        records = [
            {
                "sessionId": "abc12345-feed-0000-0000-000000000000",
                "timestamp": now.isoformat().replace("+00:00", "Z"),
                "type": "user",
                "message": {"role": "user", "content": "Investigate the widget pipeline bug"},
            },
            {"type": "user", "message": {"role": "user", "content": "Try the second approach"}},
            {
                "type": "user",
                "message": {"role": "user", "content": "Now write the regression test"},
            },
            {
                "type": "assistant",
                "timestamp": now.isoformat().replace("+00:00", "Z"),
                "message": {"role": "assistant", "content": [{"type": "text", "text": "On it."}]},
            },
        ]
        body = "\n".join(json.dumps(r) for r in records) + "\n"
        (store / "-proj-demo" / "abc12345-feed-0000-0000-000000000000.jsonl").write_text(
            body, encoding="utf-8"
        )

        cfg_path = root / "cairn.config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "data_root": str(root / ".context"),
                    "qmd_binary": "qmd",
                    "collections": {"claude-code-sessions": {"store": str(store)}},
                    "project_groups": {},
                    "repo_catalog": {},
                    "project_descriptions": {},
                }
            ),
            encoding="utf-8",
        )
        self._prev = os.environ.get("CAIRN_CONFIG")
        os.environ["CAIRN_CONFIG"] = str(cfg_path)
        config.get_config.cache_clear()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("CAIRN_CONFIG", None)
        else:
            os.environ["CAIRN_CONFIG"] = self._prev
        config.get_config.cache_clear()
        self._tmp.cleanup()

    def _run(self, argv: list[str]) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = recall.main(argv)
        return code, buf.getvalue()


class TestRecallList(_RecallFixture):
    def test_list_today_json_finds_the_session(self):
        code, out = self._run(["list", "today", "--all-projects", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(len(data), 1)
        self.assertTrue(data[0]["session_id"].startswith("abc12345"))
        self.assertEqual(data[0]["msgs"], 3)
        self.assertEqual(data[0]["title"], "Investigate the widget pipeline bug")

    def test_min_msgs_filters_the_session_out(self):
        code, out = self._run(["list", "today", "--all-projects", "--min-msgs", "10", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [])

    def test_bad_date_fails_loud(self):
        code, _ = self._run(["list", "whenever", "--all-projects"])
        self.assertEqual(code, 1)


class TestRecallExpand(_RecallFixture):
    def test_expand_by_prefix(self):
        code, out = self._run(["expand", "abc12345", "--all-projects"])
        self.assertEqual(code, 0)
        self.assertIn("Investigate the widget pipeline bug", out)
        self.assertIn("3 user messages total", out)

    def test_unknown_session_fails_loud(self):
        code, _ = self._run(["expand", "zzzzzzzz", "--all-projects"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
