"""``cairn recall search``: BM25 per phrasing, one hybrid query when needed, merged.

QMD is a ``#!python`` stub that prints JSON fixtures per subcommand from ``rs.json``
and records every argv. The hybrid timeout test makes the stub start a child and
checks that the process-group kill reached it.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from cairn import config, qmd, recall
from tests.test_shared_store import StoreFixture

STATUS_COMPLETE = "Documents\n  Total:    4 files indexed\n  Vectors:  9 embedded\n"
STATUS_PENDING = STATUS_COMPLETE + "  Pending:  3 need embedding (run 'qmd embed')\n"
STATUS_NO_VECTORS = "Documents\n  Total:    4 files indexed\n  Vectors:  0 embedded\n"

STUB = """
import json, subprocess, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent
spec = json.loads((here / "rs.json").read_text())
argv = sys.argv[1:]
with open(here / "qmd-calls.log", "a") as log:
    log.write(json.dumps(argv) + "\\n")
cmd = argv[0]
if cmd == "collection":
    print("qmd://notes/\\nqmd://claude-code-sessions/")
    sys.exit(0)
if cmd == "--version":
    print("qmd 2.5.3 (179d450)")
    sys.exit(0)
if cmd == "status":
    sys.stdout.write(spec.get("status", ""))
    sys.exit(spec.get("status_rc", 0))
def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None
if cmd == "search":
    s = spec.get("search", {})
    coll = opt("-c")
    if coll in s.get("fail", []) or s.get("rc"):
        sys.exit(1)
    if "out" in s:
        sys.stdout.write(s["out"])
        sys.exit(0)
    hits = s.get(argv[1], s.get("default", []))
    if coll:
        hits = [h for h in hits if h["file"].startswith("qmd://" + coll + "/")]
    print(json.dumps(hits))
    sys.exit(0)
if cmd == "query":
    q = spec.get("query", {})
    if q.get("sleep"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        (here / "child.pid").write_text(str(child.pid))
        time.sleep(q["sleep"])
    if "out" in q:
        sys.stdout.write(q["out"])
        sys.exit(q.get("rc", 0))
    print(json.dumps(q.get("hits", [])))
    sys.exit(q.get("rc", 0))
sys.exit(0)
"""


def hit(file: str, score: float, title: str = "T", docid: str = "#abc") -> dict:
    return {
        "docid": docid,
        "score": score,
        "file": file,
        "line": 1,
        "title": title,
        "snippet": f"snippet of {file}",
    }


FIVE = [hit(f"qmd://notes/doc{i}.md", 0.9 - i / 100, title=f"Doc {i}") for i in range(5)]


class SearchFixture(StoreFixture):
    def setUp(self):
        super().setUp()
        self.fake_qmd(STUB)
        self.spec: dict = {"search": {"default": FIVE}, "status": STATUS_COMPLETE}

    def deep(self, **values) -> None:
        data = json.loads(self.config.read_text())
        data["recall"] = {"deep": values}
        self.config.write_text(json.dumps(data))
        config.get_config.cache_clear()

    def cli(self, *args: str) -> tuple[int, str, str]:
        (self.root / "rs.json").write_text(json.dumps(self.spec))
        (self.root / "qmd-calls.log").unlink(missing_ok=True)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = recall.main(["search", *args])
        return code, out.getvalue(), err.getvalue()

    def run_json(self, *args: str) -> tuple[int, dict]:
        code, out, err = self.cli(*args, "--json")
        self.assertEqual(code, 0, err)
        return code, json.loads(out)

    def calls(self, command: str | None = None) -> list[list[str]]:
        path = self.root / "qmd-calls.log"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [r for r in rows if command is None or r[0] == command]

    def stamp(self, hours_ago: float) -> None:
        payload = qmd.record_embed("complete", "nightly", 1, qmd.IndexStatus(4, 9, 0))
        when = (datetime.now(UTC) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload["last_embed_at"] = when
        qmd.embed_stamp_path().write_text(json.dumps(payload))


class TestTrigger(SearchFixture):
    def test_enough_bm25_hits_runs_no_hybrid(self):
        _code, data = self.run_json("launchd", "-q", "launch agent")
        self.assertEqual(self.calls("query"), [])
        self.assertEqual(data["hybrid"]["status"], "not-needed")
        self.assertIsNone(data["hybrid"]["trigger"])
        self.assertEqual(data["bm25"], {**data["bm25"], "calls": 2, "failed": 0, "documents": 5})
        self.assertEqual(len(data["results"]), 5)
        self.assertEqual({r["label"] for r in data["results"]}, {"bm25"})

    def test_deep_flag_runs_exactly_one_hybrid_with_same_collections(self):
        _code, data = self.run_json(
            "renewal", "-q", "contract", "-c", "notes", "-c", "claude-code-sessions", "--deep"
        )
        queries = self.calls("query")
        self.assertEqual(len(queries), 1)
        argv = queries[0]
        self.assertEqual(argv[:2], ["query", "renewal"])
        self.assertIn("--format", argv)
        self.assertEqual(argv[argv.index("--format") + 1], "json")
        self.assertEqual(argv[argv.index("-n") + 1], "10")
        self.assertEqual(
            [argv[i + 1] for i, a in enumerate(argv) if a == "-c"],
            ["notes", "claude-code-sessions"],
        )
        self.assertIn("--no-rerank", argv)
        # BM25: one call per phrasing per collection, each with a single -c.
        searches = self.calls("search")
        self.assertEqual(len(searches), 4)
        self.assertTrue(all(s.count("-c") == 1 for s in searches))
        self.assertEqual(data["hybrid"]["trigger"], "flag")
        self.assertEqual(data["hybrid"]["status"], "ran")
        self.assertIs(data["hybrid"]["rerank"], False)

    def test_rerank_true_omits_no_rerank(self):
        self.deep(rerank=True)
        _code, data = self.run_json("x", "--deep")
        self.assertNotIn("--no-rerank", self.calls("query")[0])
        self.assertIs(data["hybrid"]["rerank"], True)

    def test_few_hits_trigger(self):
        two = [hit("qmd://notes/a.md", 0.9), hit("qmd://notes/b.md", 0.8)]
        self.spec["search"] = {"default": two}
        _code, data = self.run_json("x", "-q", "y", "-q", "z")
        self.assertEqual(data["bm25"]["calls"], 3)
        self.assertEqual(data["bm25"]["documents"], 2)
        self.assertEqual(data["hybrid"]["trigger"], "few-hits")
        self.assertEqual(len(self.calls("query")), 1)

    def test_min_hits_zero_disables_count_trigger(self):
        self.deep(min_hits=0)
        self.spec["search"] = {"default": []}
        _code, data = self.run_json("x")
        self.assertEqual(data["hybrid"]["status"], "not-needed")
        self.assertEqual(self.calls("query"), [])

    def test_low_score_trigger_when_configured(self):
        self.deep(min_top_score=0.95)
        _code, data = self.run_json("x")
        self.assertEqual(data["hybrid"]["trigger"], "low-score")
        self.deep(min_top_score=0.5)
        _code, data = self.run_json("x")
        self.assertEqual(data["hybrid"]["status"], "not-needed")

    def test_no_deep_and_auto_false(self):
        self.spec["search"] = {"default": []}
        _code, data = self.run_json("x", "--no-deep")
        self.assertEqual(data["hybrid"]["status"], "disabled")
        self.assertEqual(self.calls("query"), [])
        self.deep(auto=False)
        _code, data = self.run_json("x")
        self.assertEqual(data["hybrid"]["status"], "disabled")
        _code, data = self.run_json("x", "--deep")
        self.assertEqual(data["hybrid"]["status"], "ran")


class TestMerge(SearchFixture):
    def test_merge_labels_and_dedup(self):
        self.spec["search"] = {"default": [hit("qmd://notes/a.md", 0.82, title="A")]}
        self.spec["query"] = {
            "hits": [
                hit("qmd://notes/a.md", 0.5, title="A"),
                hit("qmd://notes/h.md", 0.4, title="H"),
            ]
        }
        _code, data = self.run_json("x", "--deep")
        by_file = {r["file"]: r for r in data["results"]}
        self.assertEqual(set(by_file), {"qmd://notes/a.md", "qmd://notes/h.md"})
        a = by_file["qmd://notes/a.md"]
        self.assertEqual(a["label"], "bm25+hybrid")
        self.assertEqual(a["sources"], ["bm25", "hybrid"])
        self.assertEqual(a["scores"], {"bm25": 0.82, "hybrid": 0.5})
        self.assertEqual(a["collection"], "notes")
        self.assertEqual(a["variants"], ["x"])
        h = by_file["qmd://notes/h.md"]
        self.assertEqual(h["label"], "hybrid")
        self.assertEqual(h["scores"], {"bm25": None, "hybrid": 0.4})
        self.assertEqual(data["results"][0]["file"], "qmd://notes/a.md")

    def test_rrf_order(self):
        # Raw scores favour c (0.99); RRF favours a and b, found by both searches.
        self.spec["search"] = {
            "x": [hit("qmd://notes/a.md", 0.5), hit("qmd://notes/b.md", 0.4)],
            "y": [hit("qmd://notes/c.md", 0.99)],
        }
        self.spec["query"] = {"hits": [hit("qmd://notes/a.md", 0.8), hit("qmd://notes/b.md", 0.9)]}
        _code, data = self.run_json("x", "-q", "y", "--deep")
        order = [r["file"] for r in data["results"]]
        self.assertEqual(order[:2], ["qmd://notes/a.md", "qmd://notes/b.md"])
        self.assertEqual(order[2], "qmd://notes/c.md")
        scores = [r["rank_score"] for r in data["results"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(data["results"][0]["variants"], ["x"])

    def test_rrf_takes_the_best_phrasing_not_the_sum(self):
        # Four variants that all find r would drown h, found by one phrasing and by
        # the hybrid query, if BM25 ranks were summed instead of maxed.
        repeated = [hit("qmd://notes/r.md", 0.9)]
        self.spec["search"] = {
            "x": [hit("qmd://notes/h.md", 0.9), *repeated],
            "v1": repeated,
            "v2": repeated,
            "v3": repeated,
        }
        self.spec["query"] = {"hits": [hit("qmd://notes/h.md", 0.8)]}
        _code, data = self.run_json("x", "-q", "v1", "-q", "v2", "-q", "v3", "--deep")
        order = [r["file"] for r in data["results"]]
        self.assertEqual(order, ["qmd://notes/h.md", "qmd://notes/r.md"])
        self.assertEqual(data["bm25"]["calls"], 4)

    def test_scoreless_hybrid_hit_does_not_break_the_command(self):
        self.spec["search"] = {"default": [hit("qmd://notes/a.md", 0.82)]}
        self.spec["query"] = {"hits": [{"file": "qmd://notes/h.md", "title": "no score"}]}
        code, data = self.run_json("x", "--deep")
        self.assertEqual(code, 0)
        files = [r["file"] for r in data["results"]]
        self.assertEqual(files, ["qmd://notes/a.md", "qmd://notes/h.md"])
        self.assertEqual(data["results"][1]["scores"], {"bm25": None, "hybrid": None})

    def test_best_bm25_score_and_variants_are_kept(self):
        self.spec["search"] = {
            "x": [hit("qmd://notes/a.md", 0.5)],
            "y": [hit("qmd://notes/a.md", 0.7)],
        }
        _code, data = self.run_json("x", "-q", "y", "--no-deep")
        self.assertEqual(data["results"][0]["scores"]["bm25"], 0.7)
        self.assertEqual(data["results"][0]["variants"], ["x", "y"])
        self.assertEqual(data["bm25"]["documents"], 1)


class TestVectors(SearchFixture):
    def test_no_vectors_skips_hybrid_with_note(self):
        self.spec["status"] = STATUS_NO_VECTORS
        _code, data = self.run_json("x", "--deep")
        self.assertEqual(self.calls("query"), [])
        self.assertEqual(data["hybrid"]["status"], "skipped-no-vectors")
        self.assertEqual(
            data["hybrid"]["note"],
            "Hybrid skipped: the index has no vectors yet; BM25 results only. Run 'cairn embed'.",
        )
        self.assertEqual(len(data["results"]), 5)

    def test_stale_stamp_skips_hybrid(self):
        self.stamp(hours_ago=100)
        _code, data = self.run_json("x", "--deep")
        self.assertEqual(data["hybrid"]["status"], "skipped-stale")
        self.assertRegex(
            data["hybrid"]["note"],
            r"^Hybrid skipped: the last embed finished (99|100) h ago \(limit 72 h\); "
            r"BM25 results only\. Run 'cairn embed'\.$",
        )
        self.assertEqual(self.calls("query"), [])
        self.deep(max_vector_age_h=None)
        _code, data = self.run_json("x", "--deep")
        self.assertEqual(data["hybrid"]["status"], "ran")

    def test_missing_stamp_runs_hybrid(self):
        self.assertIsNone(qmd.read_embed_stamp())
        _code, data = self.run_json("x", "--deep")
        self.assertEqual(data["hybrid"]["status"], "ran")
        self.assertIsNone(data["hybrid"]["note"])
        self.assertEqual(data["hybrid"]["pending"], 0)

    def test_pending_runs_hybrid_with_note(self):
        self.spec["status"] = STATUS_PENDING
        _code, data = self.run_json("x", "--deep")
        self.assertEqual(data["hybrid"]["status"], "ran")
        self.assertEqual(data["hybrid"]["pending"], 3)
        self.assertEqual(
            data["hybrid"]["note"],
            "Hybrid ran; 3 documents are not embedded yet, so only BM25 can find them.",
        )

    def test_unreadable_status_skips_with_reason(self):
        self.spec["status"] = "garbage"
        _code, data = self.run_json("x", "--deep")
        self.assertEqual(data["hybrid"]["status"], "skipped-status-unreadable")
        self.assertTrue(
            data["hybrid"]["note"].startswith("Hybrid skipped: could not read 'qmd status' (")
        )
        self.assertTrue(data["hybrid"]["note"].endswith("); BM25 results only."))


class TestHybridFailures(SearchFixture):
    def test_hybrid_timeout_returns_bm25_and_kills_group(self):
        self.deep(timeout_s=1)
        self.spec["query"] = {"sleep": 5}
        started = time.monotonic()
        _code, data = self.run_json("x", "--deep")
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(data["hybrid"]["status"], "timeout")
        self.assertEqual(
            data["hybrid"]["note"],
            "Hybrid stopped after 1 s (recall.deep.timeout_s); BM25 results only.",
        )
        self.assertEqual(len(data["results"]), 5)
        pid = int((self.root / "child.pid").read_text())
        deadline = time.monotonic() + 3
        alive = True
        while alive and time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
                time.sleep(0.05)
            except ProcessLookupError:
                alive = False
        self.assertFalse(alive, f"child {pid} of the killed qmd query survived")

    def test_hybrid_non_json_is_unsupported(self):
        self.spec["query"] = {"out": "1. notes/a.md  (0.9)\n"}
        _code, data = self.run_json("x", "--deep")
        self.assertEqual(data["hybrid"]["status"], "unsupported")
        self.assertEqual(
            data["hybrid"]["note"],
            "Hybrid skipped: qmd 2.5.3 (179d450) printed no JSON for 'qmd query --format json'; "
            "BM25 results only.",
        )

    def test_hybrid_exit_code_is_failed_and_exit_stays_0(self):
        self.spec["query"] = {"out": "", "rc": 3}
        code, data = self.run_json("x", "--deep")
        self.assertEqual(code, 0)
        self.assertEqual(data["hybrid"]["status"], "failed")
        self.assertEqual(data["hybrid"]["note"], "Hybrid failed (qmd exit 3); BM25 results only.")


class TestBm25Failures(SearchFixture):
    def test_all_bm25_failed_exits_1_with_code(self):
        self.spec["search"] = {"rc": 1}
        code, out, err = self.cli("x", "--json")
        self.assertEqual(code, 1)
        self.assertIn("code: RECALL_FAILED", err)
        self.assertEqual(json.loads(out)["code"], "RECALL_FAILED")
        self.assertEqual(self.calls("query"), [])

    def test_non_json_bm25_output_counts_as_failed(self):
        self.spec["search"] = {"out": "1. notes/a.md\n"}
        code, _out, err = self.cli("x")
        self.assertEqual(code, 1)
        self.assertIn("every BM25 search failed", err)

    def test_partial_bm25_failure_note(self):
        self.spec["search"] = {"default": FIVE, "fail": ["claude-code-sessions"]}
        _code, data = self.run_json("x", "-c", "notes", "-c", "claude-code-sessions", "--no-deep")
        self.assertEqual(data["bm25"]["failed"], 1)
        self.assertEqual(
            data["hybrid"]["note"], "1 of 2 BM25 searches failed; results are partial."
        )
        self.assertEqual(len(data["results"]), 5)

    def test_collection_listing_failure_is_recall_failed(self):
        self.spec["collection_rc"] = 1
        stub = (
            (self.root / "qmd")
            .read_text()
            .replace(
                'if cmd == "collection":\n',
                'if cmd == "collection":\n    sys.exit(spec.get("collection_rc", 0))\n',
            )
        )
        (self.root / "qmd").write_text(stub)
        code, out, err = self.cli("x", "-c", "notes", "--json")
        self.assertEqual(code, 1)
        self.assertIn("code: RECALL_FAILED", err)
        self.assertEqual(json.loads(out)["code"], "RECALL_FAILED")
        self.assertIn("could not list the qmd collections", err)

    def test_unknown_collection_exits_2(self):
        code, _out, err = self.cli("x", "-c", "nope")
        self.assertEqual(code, 2)
        self.assertEqual(err.strip(), "unknown collection nope; known: claude-code-sessions, notes")
        self.assertEqual(self.calls("search"), [])

    def test_qmd_missing_exits_1_qmd_missing(self):
        data = json.loads(self.config.read_text())
        data["qmd_binary"] = str(self.root / "no-such-qmd")
        self.config.write_text(json.dumps(data))
        config.get_config.cache_clear()
        code, out, err = self.cli("x", "--json")
        self.assertEqual(code, 1)
        self.assertIn("code: QMD_MISSING", err)
        self.assertEqual(json.loads(out)["code"], "QMD_MISSING")

    def test_usage_limits(self):
        variants = [flag for i in range(7) for flag in ("-q", f"v{i}")]
        code, _out, err = self.cli("x", *variants)
        self.assertEqual(code, 2)
        self.assertIn("at most 6 -q variants", err)
        code, _out, err = self.cli("x", "-n", "21")
        self.assertEqual(code, 2)
        self.assertIn("-n must be from 1 to 20", err)


class TestTextOutput(SearchFixture):
    def test_text_output_shape(self):
        self.spec["search"] = {"default": [hit("qmd://notes/a.md", 0.82, title="Renewal terms")]}
        self.spec["query"] = {
            "hits": [
                hit("qmd://notes/a.md", 0.5),
                hit("qmd://notes/h.md", 0.4, title="Contract notes"),
            ]
        }
        self.spec["status"] = STATUS_PENDING
        code, out, _err = self.cli("x", "--deep")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "1. [bm25+hybrid] qmd://notes/a.md  Renewal terms")
        self.assertEqual(lines[1], "2. [hybrid] qmd://notes/h.md  Contract notes")
        self.assertRegex(lines[2], r"^hybrid: ran \(flag, [0-9.]+ s, no rerank\)$")
        self.assertEqual(
            lines[3], "Hybrid ran; 3 documents are not embedded yet, so only BM25 can find them."
        )

    def test_text_output_without_hybrid(self):
        code, out, _err = self.cli("launchd")
        self.assertEqual(code, 0)
        self.assertTrue(out.rstrip().endswith("hybrid: not-needed"), out)

    def test_parser_rejects_deep_with_no_deep(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            recall.main(["search", "x", "--deep", "--no-deep"])
        self.assertEqual(caught.exception.code, 2)

    def test_no_results_prints_a_marker(self):
        self.spec["search"] = {"default": []}
        self.spec["query"] = {"hits": []}
        code, out, _err = self.cli("zzqqxx")
        self.assertEqual(code, 0)
        self.assertIn("(no results)", out)
        self.assertRegex(out, r"hybrid: ran \(few-hits")


if __name__ == "__main__":
    sys.exit(0)
