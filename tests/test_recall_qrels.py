"""Retrieval quality of cairn's deterministic recall paths, against fixed relevance data.

The corpus (``tests/fixtures/recall-corpus/``, built by :mod:`tests.recall_corpus`) is an
invented exported store of 33 documents. ``tests/fixtures/recall-qrels.json`` lists
queries in three families with the documents each must return:

* ``temporal``: ``cairn recall list DATE`` in process, with the clock pinned to the
  file's ``anchor_now`` through ``cairn.timeutil._utcnow``;
* ``exact`` and ``title-bare``: the ripgrep list pass exactly as the ``/search`` and
  ``/recall`` skills print it (the flags are parsed from the two SKILL.md files).

Each family has floors for recall@5 and expected-top-1 (and, for ``temporal``, the
whole ordered list). A failing floor test prints one table with its families and every
miss: the temporal test for ``temporal``, the rg test for ``exact`` and ``title-bare``.
See docs/recall-qrels-spec.md, including how to add a row when a retrieval bug is fixed.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, ClassVar
from unittest.mock import patch

from cairn import config, recall, timeutil
from cairn.frontmatter import parse_frontmatter
from cairn.store import record_from_frontmatter
from tests import recall_corpus
from tests.test_shared_store import HOST_IDENTITY_HINTS

ROOT = Path(__file__).resolve().parent.parent
QRELS = recall_corpus.FIXTURES / "recall-qrels.json"
CORPUS = recall_corpus.CORPUS
SKILLS = {
    "search": ROOT / "skills" / "search" / "SKILL.md",
    "recall": ROOT / "skills" / "recall" / "SKILL.md",
}
FAMILIES = ("temporal", "exact", "title-bare")
# Raised to the observed rate, never lowered without a reason in the commit message.
# title-bare top-1 is 7 of 9: rows b01 and b07 are deliberate misses (a newer session
# quotes an older title, and the rg pass ranks by mtime).
FLOORS: dict[str, dict[str, float]] = {
    "temporal": {"exact": 1.0, "recall": 1.0, "top1": 1.0},
    "exact": {"recall": 1.0, "top1": 1.0},
    "title-bare": {"recall": 1.0, "top1": 0.77},
}
RG_CAP = 20  # the /search head -n cap; /recall keeps 10
RG_SKIP = "rg not on PATH; the exact and title-bare families need ripgrep"
PLACEHOLDERS = ("QUERY", "TERM")
DECOY = "decoy-not-markdown.txt"


# ---------------------------------------------------------------------------
# The qrels file
# ---------------------------------------------------------------------------


def corpus_files(root: Path = CORPUS) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix()
        for name in recall_corpus.COLLECTIONS.values()
        for p in (root / name).rglob("*")
        if p.is_file()
    )


def load_qrels(data: dict[str, Any] | None = None, known: set[str] | None = None) -> dict:
    """Validate the qrels document; ``AssertionError`` names the first bad row."""
    data = json.loads(QRELS.read_text(encoding="utf-8")) if data is None else data
    known = set(corpus_files()) if known is None else known
    prefix = "recall-qrels.json:"
    version = data.get("schema_version")
    if version != 1:
        raise AssertionError(f"{prefix} schema_version must be 1, got {version!r}")
    seen: set[str] = set()
    for row in data["queries"]:
        rid = row["id"]
        if rid in seen:
            raise AssertionError(f"{prefix} duplicate id {rid!r}")
        seen.add(rid)
        if row["family"] not in FAMILIES:
            raise AssertionError(
                f"{prefix} row {rid}: unknown family {row['family']!r}; "
                "use temporal|exact|title-bare"
            )
        top = row.get("expected_top1")
        for path in [*row["relevant"], *([top] if top else [])]:
            if path not in known:
                raise AssertionError(
                    f"{prefix} row {rid}: {path} is not in tests/fixtures/recall-corpus"
                )
        if (top is None and row["relevant"]) or (top is not None and top not in row["relevant"]):
            raise AssertionError(
                f"{prefix} row {rid}: expected_top1 must be relevant[0] or in relevant"
            )
        args = row.get("args", [])
        if row["family"] == "temporal" and not {"--all-projects", "--project"} & set(args):
            raise AssertionError(
                f"{prefix} row {rid}: a temporal row needs --all-projects or --project in args"
            )
    return data


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def recall_at(got: list[str], relevant: list[str], k: int) -> float:
    """Share of the relevant documents in the first ``k``, out of at most ``k``.

    The denominator is ``min(len(relevant), k)``: a window with eight sessions can
    put only five in the first five, and that is full recall at five.
    """
    if not relevant:
        return 1.0 if not got else 0.0
    return len(set(got[:k]) & set(relevant)) / min(len(relevant), k)


def top1(got: list[str], expected: str | None) -> float:
    if expected is None:
        return 1.0 if not got else 0.0
    return 1.0 if got[:1] == [expected] else 0.0


def exact(got: list[str], relevant: list[str]) -> float:
    return 1.0 if got == relevant else 0.0


@dataclass
class FamilyResult:
    family: str
    rows: int = 0
    recall: float = 0.0
    top1: float = 0.0
    exact: float | None = None
    misses: list[str] = field(default_factory=list)

    def failed(self) -> list[str]:
        values = {"recall": self.recall, "top1": self.top1, "exact": self.exact}
        return [
            name
            for name, floor in FLOORS[self.family].items()
            if (values[name] or 0.0) + 1e-9 < floor
        ]


def score(family: str, rows: list[dict], results: dict[str, tuple[list[str], str]], k: int):
    """Mean recall@k, top-1 and (temporal) exact order; misses for the table."""
    out = FamilyResult(family, rows=len(rows))
    exacts = []
    for row in rows:
        got, error = results[row["id"]]
        if error:  # an rg failure is a miss even when the gold list is empty
            r = t = e = 0.0
        else:
            r = recall_at(got, row["relevant"], k)
            t = top1(got, row["expected_top1"])
            e = exact(got, row["relevant"])
        out.recall += r / len(rows)
        out.top1 += t / len(rows)
        exacts.append(e)
        if r < 1 or t < 1 or (family == "temporal" and e < 1):
            want = row["relevant"] if family == "temporal" else row["expected_top1"]
            shown = got if family == "temporal" else got[:1]
            out.misses += [
                f"  {row['id']} {family}  want {want}",
                f"  {' ' * len(row['id'])} {' ' * len(family)}  got  {shown}",
                f"  {' ' * len(row['id'])} {' ' * len(family)}  note: {row['note']}",
            ]
            if error:
                out.misses.append(f"  {' ' * len(row['id'])} {' ' * len(family)}  rg: {error}")
    if family == "temporal":
        out.exact = sum(exacts) / len(exacts)
    return out


def table(results: list[FamilyResult]) -> str:
    lines = [f"{'family':<11} rows recall@5 top1  exact {'floors':<34} status"]
    for res in results:
        floors = " ".join(f"{n}>={v:.2f}" for n, v in FLOORS[res.family].items())
        ex = f"{res.exact:.2f}" if res.exact is not None else " - "
        status = "FAIL" if res.failed() else "ok"
        lines.append(
            f"{res.family:<11} {res.rows:>4}   {res.recall:.2f}   {res.top1:.2f}  {ex:<5} "
            f"{floors:<34} {status}"
        )
    misses = [line for res in results for line in res.misses]
    if misses:
        lines += ["misses:", *misses]
    return "\n".join(lines)


def rows_of(family: str) -> list[dict]:
    return [row for row in load_qrels()["queries"] if row["family"] == family]


# ---------------------------------------------------------------------------
# The skills' rg list pass
# ---------------------------------------------------------------------------


def skill_rg_line(path: Path, prefix: str = "rg -l ") -> str:
    """The one fenced line of a skill that starts with ``prefix`` (the list pass)."""
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith(prefix)
    ]
    if len(lines) != 1:
        raise AssertionError(f"{path}: expected one {prefix.strip()!r} line, found {len(lines)}")
    return lines[0]


def skill_rg_argv(path: Path) -> tuple[list[str], int]:
    """The list-pass argv with the placeholder as ``QUERY`` and paths as ``PATHS``."""
    line = skill_rg_line(path)
    command, _, tail = line.partition("|")
    argv: list[str] = []
    for token in shlex.split(command):
        if token in PLACEHOLDERS:
            argv.append("QUERY")
        elif token == "..." or (token.startswith("PATH") and token[4:].isdigit()):
            if "PATHS" not in argv:
                argv.append("PATHS")
        else:
            argv.append(token)
    head = shlex.split(tail)
    return argv, int(head[head.index("-n") + 1])


def rg_outcome(returncode: int, stdout: str, stderr: str) -> tuple[list[str], str]:
    """The listed paths and an error: exit 1 is no match, any other non-zero is an error."""
    if returncode == 1:
        return [], ""
    if returncode:
        first = (stderr.strip().splitlines() or [f"exit {returncode}"])[0]
        return [], f"exit {returncode}: {first}"
    return stdout.splitlines()[:RG_CAP], ""


# ---------------------------------------------------------------------------
# The temp store
# ---------------------------------------------------------------------------


class CorpusStore(unittest.TestCase):
    """The committed corpus copied into a temp home, with mtimes set from activity."""

    root: ClassVar[Path]
    data: ClassVar[Path]
    _tmp: ClassVar[TemporaryDirectory]
    _env: ClassVar[Any]

    @classmethod
    def setUpClass(cls):
        cls._tmp = TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.root = Path(cls._tmp.name).resolve()
        cls.data = cls.root / "data"
        cfg = cls.root / "config.json"
        cfg.write_text(
            json.dumps(
                {
                    "data_root": str(cls.data),
                    "qmd_binary": "/missing/qmd",
                    "collections": {
                        name: {"store": str(cls.root / "stores" / name)}
                        for name in recall_corpus.COLLECTIONS.values()
                    },
                    "project_groups": {},
                    "repo_catalog": {},
                    "project_descriptions": {},
                }
            ),
            encoding="utf-8",
        )
        env = dict(
            {k: v for k, v in os.environ.items() if k not in HOST_IDENTITY_HINTS},
            HOME=str(cls.root / "home"),
            CAIRN_CONFIG=str(cfg),
            XDG_CACHE_HOME=str(cls.root / "cache"),
            XDG_CONFIG_HOME=str(cls.root / "xdg"),
            XDG_STATE_HOME=str(cls.root / "state"),
        )
        env.pop("RIPGREP_CONFIG_PATH", None)  # a user's rg defaults must not change the run
        cls._env = patch.dict(os.environ, env, clear=True)
        cls._env.start()
        # Class cleanups run even when setUpClass raises; tearDownClass does not.
        cls.addClassCleanup(config.get_config.cache_clear)
        cls.addClassCleanup(cls._env.stop)
        config.get_config.cache_clear()
        for name in recall_corpus.COLLECTIONS.values():
            (cls.root / "stores" / name).mkdir(parents=True)
            shutil.copytree(CORPUS / name, cls.data / name)
        # The file changes when its session does (skills/search/SKILL.md), so the mtime
        # is the last activity; one second per file keeps the rg order total.
        for index, rel in enumerate(corpus_files(cls.data)):
            path = cls.data / rel
            fm = parse_frontmatter(path.read_text(encoding="utf-8"))
            stamp = timeutil.parse_iso(fm.get("activity_at") or fm.get("last_activity"))
            assert stamp is not None, rel
            when = stamp.timestamp() + index
            os.utime(path, (when, when))
        # A non-Markdown file that holds every rg query and is the newest file in the
        # store: if a skill drops --type md, it takes top-1 on every rg row.
        decoy = cls.data / recall_corpus.COLLECTIONS["claude"] / DECOY
        queries = [row["query"] for row in load_qrels()["queries"] if row["family"] != "temporal"]
        decoy.write_text("\n".join(queries) + "\n", encoding="utf-8")
        newest = max(p.stat().st_mtime for p in cls.data.rglob("*.md")) + 60
        os.utime(decoy, (newest, newest))

    def relative(self, path: str) -> str:
        return Path(path).resolve().relative_to(self.data).as_posix()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class ScoringTest(unittest.TestCase):
    def test_metrics(self):
        self.assertEqual(recall_at([], [], 5), 1.0)
        self.assertEqual(recall_at(["a"], [], 5), 0.0)
        self.assertEqual(recall_at(["x", "a", "y", "z", "w", "b"], ["a", "b"], 5), 0.5)
        self.assertEqual(recall_at(list("abcdefgh"), list("abcdefgh"), 5), 1.0)
        self.assertEqual(recall_at(list("abcdxfgh"), list("abcdefgh"), 5), 0.8)
        self.assertEqual(top1([], None), 1.0)
        self.assertEqual(top1(["a"], None), 0.0)
        self.assertEqual(top1(["a", "b"], "a"), 1.0)
        self.assertEqual(top1([], "a"), 0.0)
        self.assertEqual(exact(["a", "b"], ["a", "b"]), 1.0)
        self.assertEqual(exact(["b", "a"], ["a", "b"]), 0.0)
        self.assertEqual(exact([], []), 1.0)

    def test_an_rg_error_scores_zero_and_is_shown(self):
        row: dict[str, Any] = {
            "id": "e99",
            "relevant": [],
            "expected_top1": None,
            "note": "empty gold",
        }
        clean = score("exact", [row], {"e99": ([], "")}, 5)
        self.assertEqual((clean.recall, clean.top1, clean.misses), (1.0, 1.0, []))
        broken = score("exact", [row], {"e99": ([], "exit 2: rg: unrecognized flag --x")}, 5)
        self.assertEqual((broken.recall, broken.top1), (0.0, 0.0))
        self.assertEqual(broken.failed(), ["recall", "top1"])
        self.assertIn("rg: exit 2: rg: unrecognized flag --x", "\n".join(broken.misses))

    def test_rg_outcome(self):
        self.assertEqual(rg_outcome(0, "a.md\nb.md\n", ""), (["a.md", "b.md"], ""))
        self.assertEqual(rg_outcome(0, "".join(f"{i}.md\n" for i in range(30)), "")[0][-1], "19.md")
        self.assertEqual(rg_outcome(1, "", ""), ([], ""))
        self.assertEqual(
            rg_outcome(2, "", "rg: unrecognized flag --x\nmore\n"),
            ([], "exit 2: rg: unrecognized flag --x"),
        )
        self.assertEqual(rg_outcome(2, "", ""), ([], "exit 2: exit 2"))


class QrelsFileTest(unittest.TestCase):
    def test_schema_and_ids(self):
        data = load_qrels()
        ids = [row["id"] for row in data["queries"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual({row["family"] for row in data["queries"]}, set(FAMILIES))
        self.assertEqual(data["k"], 5)

    def test_every_path_is_in_the_corpus(self):
        known = set(corpus_files())
        for row in load_qrels()["queries"]:
            for path in [*row["relevant"], row["expected_top1"]]:
                if path is not None:
                    self.assertIn(path, known, row["id"])

    def test_family_sizes(self):
        self.assertGreaterEqual(len(rows_of("temporal")), 12)
        self.assertGreaterEqual(len(rows_of("exact")), 10)
        self.assertGreaterEqual(len(rows_of("title-bare")), 8)

    def test_loader_rejects_bad_rows(self):
        known = {"a.md", "b.md"}

        def doc(*rows: dict, version: object = 1) -> dict:
            return {"schema_version": version, "queries": list(rows)}

        good = {"id": "e01", "family": "exact", "query": "x", "relevant": ["a.md"]}
        good["expected_top1"] = "a.md"
        cases = [
            (doc(good, version=2), "recall-qrels.json: schema_version must be 1, got 2"),
            (
                doc({**good, "family": "title-in-question"}),
                "recall-qrels.json: row e01: unknown family 'title-in-question'; "
                "use temporal|exact|title-bare",
            ),
            (
                doc({**good, "relevant": ["c.md"], "expected_top1": "c.md"}),
                "recall-qrels.json: row e01: c.md is not in tests/fixtures/recall-corpus",
            ),
            (
                doc({**good, "expected_top1": "b.md"}),
                "recall-qrels.json: row e01: expected_top1 must be relevant[0] or in relevant",
            ),
            (
                doc({**good, "family": "temporal", "args": ["--min-msgs", "1"]}),
                "recall-qrels.json: row e01: a temporal row needs --all-projects or "
                "--project in args",
            ),
            (doc(good, good), "recall-qrels.json: duplicate id 'e01'"),
        ]
        for data, message in cases:
            with self.subTest(message=message), self.assertRaises(AssertionError) as caught:
                load_qrels(data, known)
            self.assertEqual(str(caught.exception), message)


class CorpusTest(unittest.TestCase):
    def test_corpus_matches_builder(self):
        built = recall_corpus.build()
        committed = {rel: (CORPUS / rel).read_bytes() for rel in corpus_files()}
        self.assertEqual(sorted(built), sorted(committed), "run python -m tests.recall_corpus")
        for rel, content in built.items():
            self.assertEqual(
                content,
                committed[rel],
                f"{rel} differs from the builder; run 'uv run python -m tests.recall_corpus "
                "--write' and review the diff",
            )

    def test_every_document_parses(self):
        records = {}
        for rel in corpus_files():
            fm = parse_frontmatter((CORPUS / rel).read_text(encoding="utf-8"))
            source = next(s for s, c in recall_corpus.COLLECTIONS.items() if rel.startswith(c))
            id_field = "composer_id" if source == "cursor" else "session_id"
            types = (f"{source}-session", f"{source}-subagent")
            records[rel] = record_from_frontmatter(CORPUS / rel, fm, source, id_field, types)
        sessions = [rel for rel, record in records.items() if record is not None]
        others = [rel for rel, record in records.items() if record is None]
        self.assertEqual(len(sessions), 32)
        self.assertEqual(len(others), 1)
        self.assertIn("/exchange-codex-", others[0])

    def test_span_and_spread(self):
        sources: set[str] = set()
        projects: set[str] = set()
        days: set[str] = set()
        for rel in corpus_files():
            fm = parse_frontmatter((CORPUS / rel).read_text(encoding="utf-8"))
            if fm.get("type") == "agent-exchange":
                continue
            sources.add(rel.split("/", 1)[0])
            projects.add(rel.split("/")[1])
            days.update(fm.get("activity_dates") or [])
        self.assertEqual(len(sources), 3)
        self.assertEqual(projects, {"harbour", "lantern", "quarry", "fieldnotes"})
        self.assertEqual(len(days), 14)


class SkillRgTest(unittest.TestCase):
    def test_skill_rg_lines_agree(self):
        search, search_cap = skill_rg_argv(SKILLS["search"])
        recall_argv, recall_cap = skill_rg_argv(SKILLS["recall"])
        self.assertEqual(
            search,
            recall_argv,
            f"the rg list pass differs:\n  search: {skill_rg_line(SKILLS['search'])}\n"
            f"  recall: {skill_rg_line(SKILLS['recall'])}",
        )
        self.assertEqual((search_cap, recall_cap), (RG_CAP, 10))

    def test_query_is_passed_with_e(self):
        for name, path in SKILLS.items():
            argv, _ = skill_rg_argv(path)
            with self.subTest(skill=name):
                self.assertEqual(argv[argv.index("QUERY") - 1], "-e")

    def test_search_second_pass_passes_the_query_with_e(self):
        tokens = shlex.split(skill_rg_line(SKILLS["search"], prefix="rg -n "))
        self.assertEqual(tokens[tokens.index("QUERY") - 1], "-e")


class CorpusStoreSetupTest(unittest.TestCase):
    def test_failed_setup_restores_the_environment(self):
        class Probe(CorpusStore):
            pass

        before = dict(os.environ)
        with (
            patch("shutil.copytree", side_effect=OSError("copy failed")),
            self.assertRaises(OSError),
        ):
            Probe.setUpClass()
        self.assertNotEqual(os.environ.get("HOME"), before.get("HOME"))
        Probe.doClassCleanups()
        self.assertEqual(dict(os.environ), before)
        self.assertEqual(config.get_config.cache_info().currsize, 0)
        self.assertFalse(Probe.root.exists())

    def test_the_store_holds_a_non_markdown_decoy(self):
        class Probe(CorpusStore):
            pass

        Probe.setUpClass()
        try:
            decoy = Probe.data / recall_corpus.COLLECTIONS["claude"] / DECOY
            newest = max(p.stat().st_mtime for p in Probe.data.rglob("*.md"))
            self.assertGreater(decoy.stat().st_mtime, newest)
            self.assertIn("--sortr modified", decoy.read_text(encoding="utf-8"))
        finally:
            Probe.doClassCleanups()


class TemporalQrelsTest(CorpusStore):
    def anchor(self) -> datetime:
        stamp = timeutil.parse_iso(load_qrels()["anchor_now"])
        assert stamp is not None
        return stamp

    def listed(self, row: dict) -> list[str]:
        out, err = io.StringIO(), io.StringIO()
        argv = ["list", row["query"], *row["args"], "--json"]
        with (
            patch("cairn.timeutil._utcnow", return_value=self.anchor()),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = recall.main(argv)
        self.assertEqual(code, 0, f"{row['id']}: {err.getvalue()}")
        return [self.relative(item["path"]) for item in json.loads(out.getvalue())]

    def test_floors(self):
        rows = rows_of("temporal")
        results = {row["id"]: (self.listed(row), "") for row in rows}
        res = score("temporal", rows, results, load_qrels()["k"])
        if res.failed():
            self.fail("\n" + table([res]))

    def test_clock_seam_is_used(self):
        with patch("cairn.timeutil._utcnow", return_value=self.anchor()):
            start, end = timeutil.parse_date_range("yesterday")
        self.assertEqual(
            (start.date().isoformat(), end.date().isoformat()), ("2026-03-15", "2026-03-16")
        )


class ExactQrelsTest(CorpusStore):
    def setUp(self):
        self.rg = shutil.which("rg")
        if self.rg is None:
            self.skipTest(RG_SKIP)

    def run_rg(self, query: str) -> tuple[list[str], str]:
        template, _ = skill_rg_argv(SKILLS["search"])
        dirs = sorted(str(self.data / name) for name in recall_corpus.COLLECTIONS.values())
        argv: list[str] = []
        for token in template:
            if token == "QUERY":
                argv.append(query)
            elif token == "PATHS":
                argv.extend(dirs)
            else:
                argv.append(token)
        argv[0] = str(self.rg)
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        lines, error = rg_outcome(result.returncode, result.stdout, result.stderr)
        return [self.relative(line) for line in lines], error

    def test_floors(self):
        k = load_qrels()["k"]
        scored = []
        for family in ("exact", "title-bare"):
            rows = rows_of(family)
            results = {row["id"]: self.run_rg(row["query"]) for row in rows}
            scored.append(score(family, rows, results, k))
        if any(res.failed() for res in scored):
            self.fail("\n" + table(scored))


if __name__ == "__main__":
    unittest.main()
