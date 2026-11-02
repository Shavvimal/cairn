"""``cairn recall search``: BM25 for every phrasing, one hybrid query when needed.

The skills used to run ``qmd search`` per variant themselves and never ``qmd query``
(hybrid: BM25 + vectors + LLM expansion), because a hybrid query takes seconds and
BM25 a fraction of a second. This command keeps that default and adds the hybrid
query as one bounded call: on ``--deep``, or when BM25 finds fewer distinct
documents than ``recall.deep.min_hits``. The hybrid query runs through
:func:`cairn.procs.run_capped`, so its time budget kills the whole process group
(no Node child keeps the GPU busy), and it is skipped with a one-line note when the
index has no vectors or the last embed is older than ``recall.deep.max_vector_age_h``.

qmd facts this relies on (qmd 2.5.3, see docs/nightly-embed-deep-recall-spec.md):
``--format json`` prints a JSON array with ``docid``, ``score``, ``file``
(``qmd://COLLECTION/PATH``), ``line``, ``title`` and ``snippet``, and ``[]`` with
exit 0 for no match; progress lines go to stderr. A repeated ``-c`` searches the
whole index and filters afterwards, which can return fewer than ``-n`` hits, so the
BM25 pass runs one call per phrasing per collection and merges them. qmd ignores
unknown flags, so an older qmd prints text for ``--format json``: that BM25 call
counts as failed, and the hybrid call as ``unsupported``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from . import errors
from .config import DeepRecall, get_config
from .procs import run_capped
from .qmd import index_status, list_collections, parse_iso, qmd_version, read_embed_stamp

BM25_TIMEOUT_S = 15
MAX_WORKERS = 4
MAX_VARIANTS = 6
MAX_N = 20
MAX_RESULTS = 20
RRF_K = 60

HYBRID_STATUSES = (
    "not-needed",
    "disabled",
    "ran",
    "skipped-no-vectors",
    "skipped-stale",
    "skipped-status-unreadable",
    "timeout",
    "failed",
    "unsupported",
)


def _env() -> dict[str, str]:
    return {**os.environ, "NO_COLOR": "1"}


def parse_hits(stdout: str) -> list[dict] | None:
    """The JSON array ``qmd ... --format json`` printed, or ``None`` for anything else."""
    text = (stdout or "").strip()
    if not text.startswith("["):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        return None
    return [item for item in data if isinstance(item.get("file"), str)]


def collection_of(uri: str) -> str:
    """The collection segment of a ``qmd://COLLECTION/PATH`` URI."""
    rest = uri[len("qmd://") :] if uri.startswith("qmd://") else uri
    return rest.split("/", 1)[0]


def _score(hit: dict) -> float | None:
    value = hit.get("score")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class Search:
    """One ``cairn recall search`` run; ``run`` returns the exit code."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.config = get_config()
        self.deep: DeepRecall = self.config.recall.deep
        self.qmd = self.config.qmd_binary
        self.query: str = args.query
        self.variants: list[str] = list(args.variant or [])
        self.collections: list[str] = list(args.collection or [])
        self.n: int = args.n
        # file -> merged result
        self.results: dict[str, dict] = {}
        self.bm25 = {"calls": 0, "failed": 0, "documents": 0, "elapsed_s": 0.0}
        self.hybrid: dict = {
            "status": "not-needed",
            "trigger": None,
            "rerank": self.deep.rerank,
            "elapsed_s": None,
            "pending": None,
            "note": None,
        }
        self.binary_missing = False

    # -- BM25 -------------------------------------------------------------------

    def _bm25_one(self, phrase: str, collection: str | None) -> list[dict] | None:
        argv = [self.qmd, "search", phrase, "-n", str(self.n), "--format", "json"]
        if collection:
            argv += ["-c", collection]
        try:
            result = run_capped(argv, timeout=BM25_TIMEOUT_S, env=_env())
        except FileNotFoundError:
            self.binary_missing = True
            return None
        except (subprocess.TimeoutExpired, OSError, RuntimeError):
            return None
        if result.returncode != 0:
            return None
        return parse_hits(result.stdout)

    def run_bm25(self) -> None:
        phrasings = [self.query, *self.variants]
        scopes: list[str | None] = list(self.collections) or [None]
        calls = [(phrase, scope) for phrase in phrasings for scope in scopes]
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            outputs = list(pool.map(lambda call: self._bm25_one(*call), calls))
        self.bm25["elapsed_s"] = round(time.monotonic() - started, 2)
        self.bm25["calls"] = len(calls)
        for (phrase, _scope), hits in zip(calls, outputs, strict=True):
            if hits is None:
                self.bm25["failed"] += 1
                continue
            for rank, hit in enumerate(hits, start=1):
                self._merge(hit, phrase=phrase, bm25_rank=rank)
        self.bm25["documents"] = sum(1 for r in self.results.values() if "bm25" in r["sources"])

    # -- merge ------------------------------------------------------------------

    def _merge(
        self,
        hit: dict,
        *,
        phrase: str | None = None,
        bm25_rank: int | None = None,
        hybrid_rank: int | None = None,
    ) -> None:
        file = hit["file"]
        entry = self.results.get(file)
        if entry is None:
            entry = self.results[file] = {
                "file": file,
                "collection": collection_of(file),
                "docid": hit.get("docid"),
                "title": hit.get("title"),
                "line": hit.get("line"),
                "snippet": None,
                "sources": [],
                "label": "",
                "scores": {"bm25": None, "hybrid": None},
                "variants": [],
                "rank_score": 0.0,
                "_bm25_rrf": 0.0,
                "_hybrid_rrf": 0.0,
            }
        score = _score(hit)
        if bm25_rank is not None:
            if "bm25" not in entry["sources"]:
                entry["sources"].append("bm25")
            if phrase is not None and phrase not in entry["variants"]:
                entry["variants"].append(phrase)
            if score is not None and (
                entry["scores"]["bm25"] is None or score > entry["scores"]["bm25"]
            ):
                entry["scores"]["bm25"] = score
            entry["_bm25_rrf"] = max(entry["_bm25_rrf"], 1 / (RRF_K + bm25_rank))
            if entry["snippet"] is None and hit.get("snippet"):
                entry["snippet"] = hit["snippet"]
        if hybrid_rank is not None:
            if "hybrid" not in entry["sources"]:
                entry["sources"].append("hybrid")
            entry["scores"]["hybrid"] = score
            entry["_hybrid_rrf"] = 1 / (RRF_K + hybrid_rank)
            if entry["snippet"] is None and hit.get("snippet"):
                entry["snippet"] = hit["snippet"]
        if entry["title"] is None and hit.get("title"):
            entry["title"] = hit["title"]

    def ordered(self) -> list[dict]:
        rows = []
        for entry in self.results.values():
            entry["sources"] = [s for s in ("bm25", "hybrid") if s in entry["sources"]]
            entry["label"] = "+".join(entry["sources"])
            entry["rank_score"] = round(entry["_bm25_rrf"] + entry["_hybrid_rrf"], 6)
            rows.append(entry)
        rows.sort(
            key=lambda e: (
                -e["rank_score"],
                -max([v for v in e["scores"].values() if v is not None] or [0.0]),
                e["file"],
            )
        )
        return [{k: v for k, v in e.items() if not k.startswith("_")} for e in rows[:MAX_RESULTS]]

    # -- the hybrid decision ----------------------------------------------------

    def trigger(self) -> str | None:
        if self.args.deep:
            return "flag"
        if self.args.no_deep or not self.deep.auto:
            self.hybrid["status"] = "disabled"
            return None
        if self.deep.min_hits and self.bm25["documents"] < self.deep.min_hits:
            return "few-hits"
        floor = self.deep.min_top_score
        if floor is not None:
            scores = [
                e["scores"]["bm25"]
                for e in self.results.values()
                if e["scores"]["bm25"] is not None
            ]
            if not scores or max(scores) < floor:
                return "low-score"
        return None

    def vectors_usable(self) -> bool:
        """Set a skip status and note when the hybrid query cannot help."""
        try:
            status = index_status(self.qmd)
        except RuntimeError as exc:
            self.hybrid["status"] = "skipped-status-unreadable"
            self.hybrid["note"] = (
                f"Hybrid skipped: could not read 'qmd status' ({exc}); BM25 results only."
            )
            return False
        if status.state in ("empty", "no-vectors"):
            self.hybrid["status"] = "skipped-no-vectors"
            self.hybrid["note"] = (
                "Hybrid skipped: the index has no vectors yet; BM25 results only. "
                "Run 'cairn embed'."
            )
            return False
        stamp = read_embed_stamp()
        limit = self.deep.max_vector_age_h
        last = parse_iso(stamp.get("last_embed_at")) if stamp else None
        if last is not None and limit is not None:
            age_h = (datetime.now(UTC) - last).total_seconds() / 3600
            if age_h > limit:
                self.hybrid["status"] = "skipped-stale"
                self.hybrid["note"] = (
                    f"Hybrid skipped: the last embed finished {int(age_h)} h ago "
                    f"(limit {limit} h); BM25 results only. Run 'cairn embed'."
                )
                return False
        self.hybrid["pending"] = status.pending
        return True

    def run_hybrid(self) -> None:
        argv = [self.qmd, "query", self.query, "-n", str(self.deep.n), "--format", "json"]
        for collection in self.collections:
            argv += ["-c", collection]
        if not self.deep.rerank:
            argv.append("--no-rerank")
        timeout = self.deep.timeout_s
        started = time.monotonic()
        try:
            result = run_capped(argv, timeout=timeout, env=_env())
        except subprocess.TimeoutExpired:
            self.hybrid["elapsed_s"] = round(time.monotonic() - started, 1)
            self.hybrid["status"] = "timeout"
            self.hybrid["note"] = (
                f"Hybrid stopped after {timeout} s (recall.deep.timeout_s); BM25 results only."
            )
            return
        except (OSError, RuntimeError) as exc:
            self.hybrid["elapsed_s"] = round(time.monotonic() - started, 1)
            self.hybrid["status"] = "failed"
            self.hybrid["note"] = f"Hybrid failed ({exc}); BM25 results only."
            return
        self.hybrid["elapsed_s"] = round(time.monotonic() - started, 1)
        if result.returncode != 0:
            self.hybrid["status"] = "failed"
            self.hybrid["note"] = (
                f"Hybrid failed (qmd exit {result.returncode}); BM25 results only."
            )
            return
        hits = parse_hits(result.stdout)
        if hits is None:
            try:
                version = qmd_version(self.qmd)
            except RuntimeError:
                version = "(unknown version)"
            self.hybrid["status"] = "unsupported"
            self.hybrid["note"] = (
                f"Hybrid skipped: {version} printed no JSON for 'qmd query --format json'; "
                "BM25 results only."
            )
            return
        self.hybrid["status"] = "ran"
        for rank, hit in enumerate(hits, start=1):
            self._merge(hit, hybrid_rank=rank)
        if self.hybrid["pending"]:
            self.hybrid["note"] = (
                f"Hybrid ran; {self.hybrid['pending']} documents are not embedded yet, "
                "so only BM25 can find them."
            )

    # -- the run ----------------------------------------------------------------

    def run(self) -> int:
        if len(self.variants) > MAX_VARIANTS:
            print(f"Error: at most {MAX_VARIANTS} -q variants", file=sys.stderr)
            return 2
        if not 1 <= self.n <= MAX_N:
            print(f"Error: -n must be from 1 to {MAX_N}", file=sys.stderr)
            return 2
        if not (shutil.which(self.qmd) or Path(self.qmd).expanduser().is_file()):
            return self._fail("QMD_MISSING", f"qmd not found: {self.qmd}")
        if self.collections:
            try:
                known = sorted(list_collections(self.qmd))
            except (RuntimeError, OSError) as exc:
                return self._fail("RECALL_FAILED", f"could not list the qmd collections: {exc}")
            unknown = [c for c in self.collections if c not in known]
            if unknown:
                print(
                    f"unknown collection {unknown[0]}; known: {', '.join(known) or '(none)'}",
                    file=sys.stderr,
                )
                return 2
        try:
            self.run_bm25()
        except RuntimeError as exc:  # the thread pool could not start
            return self._fail("RECALL_FAILED", f"could not run the BM25 searches: {exc}")
        if self.bm25["failed"] == self.bm25["calls"]:
            if self.binary_missing:
                return self._fail("QMD_MISSING", f"qmd could not start: {self.qmd}")
            return self._fail("RECALL_FAILED", "every BM25 search failed")
        trigger = self.trigger()
        if trigger is not None:
            self.hybrid["trigger"] = trigger
            if self.vectors_usable():
                self.run_hybrid()
        if self.bm25["failed"]:
            partial = (
                f"{self.bm25['failed']} of {self.bm25['calls']} BM25 searches failed; "
                "results are partial."
            )
            self.hybrid["note"] = (
                f"{self.hybrid['note']} {partial}" if self.hybrid["note"] else partial
            )
        self.print_report()
        return 0

    def _fail(self, code: str, message: str) -> int:
        return errors.fail(code, message, exit=1, prefix="Error: ", as_json=bool(self.args.json))

    def print_report(self) -> None:
        results = self.ordered()
        if self.args.json:
            payload = {
                "query": self.query,
                "variants": self.variants,
                "collections": self.collections,
                "bm25": self.bm25,
                "hybrid": self.hybrid,
                "results": results,
            }
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return
        for number, row in enumerate(results, start=1):
            title = f"  {row['title']}" if row.get("title") else ""
            print(f"{number}. [{row['label']}] {row['file']}{title}")
        if not results:
            print("(no results)")
        status = self.hybrid["status"]
        if status == "ran":
            rerank = "rerank" if self.deep.rerank else "no rerank"
            print(f"hybrid: ran ({self.hybrid['trigger']}, {self.hybrid['elapsed_s']} s, {rerank})")
        else:
            print(f"hybrid: {status}")
        if self.hybrid["note"]:
            print(self.hybrid["note"])


def run(args: argparse.Namespace) -> int:
    """Entry point for ``cairn recall search``."""
    return Search(args).run()


__all__ = ["HYBRID_STATUSES", "Search", "collection_of", "parse_hits", "run"]
