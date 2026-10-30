"""``cairn claude memory import``: the memory reader, discovery, mapping and writes.

Every memory is invented (``tests/fixtures/claude_memory/``, or written by a test).
The Claude config directory, the data root and HOME live in a temp directory
(:class:`tests.test_shared_store.StoreFixture`), so the real memory directories are
never read.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import shutil
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from cairn import claude_memory, cli, config, decisions, recall, withdrawals
from cairn.claude_memory import Memory, MemoryProblem, read_memory
from cairn.harnesses.claude import claude_project_dir_name
from tests.test_decisions import SID, DecisionsFixture, doc

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "claude_memory"
ENCODED = "-tmp-example-app"
UNEXPORTED = "9b1d07aa-0000-4000-8000-000000000002"


def memory_text(
    description: str = "Keep each layer small",
    *,
    type_: str | None = "project",
    origin: str | None = None,
    modified: str | None = "2026-10-29T09:00:00Z",
    flat: bool = False,
    metadata_line: str = "metadata:",
) -> str:
    keys = []
    if type_ is not None:
        keys.append(f"type: {type_}")
    if origin is not None:
        keys.append(f"originSessionId: {origin}")
    if modified is not None:
        keys.append(f"modified: {modified}")
    lines = ["---", "name: a note", f"description: {json.dumps(description)}"]
    lines += keys if flat else [metadata_line, *(f"  {k}" for k in keys)]
    return "\n".join([*lines, "---", "", "Body."]) + "\n"


def _home(path: Path) -> str:
    return "~/" + path.resolve().relative_to(Path.home().resolve()).as_posix()


class MemoryFixture(DecisionsFixture):
    def setUp(self):
        super().setUp()
        data = json.loads(self.config.read_text())
        data["path_resolution"] = {"home_slug_prefixes": ["-tmp-"]}
        self.config.write_text(json.dumps(data))
        config.get_config.cache_clear()
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.root / "home" / ".claude")
        self.projects = self.root / "home" / ".claude" / "projects"
        self.memory_dir_value = f"~/.claude/projects/{ENCODED}/memory"
        self.memory = self.projects / ENCODED / "memory"
        shutil.copytree(FIXTURES, self.memory)
        self.session = self.write_doc("2026-10-30-3f2a9c1e.md", doc(rows=None))
        self.project_doc = self.root / "data" / "decisions" / "example-app.md"

    def chdir(self, path: Path) -> None:
        previous = Path.cwd()
        os.chdir(path)
        self.addCleanup(os.chdir, previous)

    def write_memory(self, stem: str, text: str) -> Path:
        path = self.memory / f"{stem}.md"
        path.write_text(text, encoding="utf-8")
        return path

    def run_import(self, *argv: str, memory_dir: bool = True) -> tuple[int, str, str]:
        extra = ["--memory-dir", str(self.memory)] if memory_dir else []
        module = importlib.reload(importlib.import_module("cairn.sources.claude"))
        source = module.ClaudeSource()
        args = cli.build_parser(source).parse_args(["memory", "import", *extra, *argv])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = args.func(args)
            finally:
                source.flush_catalog()
        return code, out.getvalue(), err.getvalue()

    def run_json(self, *argv: str) -> tuple[int, dict, str]:
        code, out, err = self.run_import("--json", *argv)
        return code, json.loads(out), err

    def rows(self, path: Path, heading: str | None = "## Conversation") -> list:
        rows, problems = decisions.read_table(path, heading)
        self.assertEqual(problems, [])
        return rows

    def tree_hash(self) -> str:
        digest = hashlib.sha256()
        for base in (self.root / "data", self.projects):
            for path in sorted(p for p in base.rglob("*") if p.is_file()):
                digest.update(str(path).encode() + b"\0" + path.read_bytes())
        return digest.hexdigest()


class TestReader(unittest.TestCase):
    def read(self, text: str) -> Memory | MemoryProblem:
        with self.subTest(text=text[:40]):
            from tempfile import TemporaryDirectory

            with TemporaryDirectory() as tmp:
                path = Path(tmp) / "note.md"
                path.write_text(text, encoding="utf-8")
                return read_memory(path)

    def test_reader_nested_metadata(self):
        memory = read_memory(FIXTURES / "release-train.md")
        assert isinstance(memory, Memory)
        self.assertEqual(
            (memory.stem, memory.name, memory.type, memory.origin, memory.modified),
            ("release-train", "release-train", "feedback", SID, "2026-10-30T13:46:12.010Z"),
        )
        self.assertEqual(memory.description, "Ship each change as its own PR off a fresh main")

    def test_reader_flat_form(self):
        memory = read_memory(FIXTURES / "flat-form.md")
        assert isinstance(memory, Memory)
        self.assertEqual((memory.type, memory.origin), ("project", UNEXPORTED))
        self.assertEqual(memory.name, "Flat form note")

    def test_reader_trailing_space_metadata(self):
        memory = self.read(memory_text(type_="user", metadata_line="metadata: "))
        assert isinstance(memory, Memory)
        self.assertEqual(memory.type, "user")

    def test_reader_quoted_escapes(self):
        memory = read_memory(FIXTURES / "quoted-escapes.md")
        assert isinstance(memory, Memory)
        self.assertEqual(memory.description, 'Say "done" only after the C:\\build step passes')
        single = read_memory(FIXTURES / "api-docs.md")
        assert isinstance(single, Memory)
        self.assertEqual(single.description, "The widget API docs live in the docs/api folder")
        quoted = self.read("---\ndescription: 'it''s fine'\ntype: user\n---\n")
        assert isinstance(quoted, Memory)
        self.assertEqual(quoted.description, "it's fine")

    def test_reader_rejects_block_scalar(self):
        problem = self.read("---\nname: x\ndescription: |\n  long text\ntype: user\n---\n")
        self.assertEqual(
            problem, MemoryProblem("note", "frontmatter line 3: block scalars are not supported")
        )
        for text, reason in (
            ("no head\n", "no frontmatter"),
            ("---\nmetadata:\n    type: user\n---\n", "frontmatter line 3: unexpected indentation"),
            ("---\ntags:\n  - a\n---\n", "frontmatter line 3: list items are not supported"),
            ("---\ndescription: [a]\n---\n", "frontmatter line 2: flow values are not supported"),
            ("---\ndescription: x\n", "frontmatter line 3: the frontmatter is not closed by '---'"),
            ('---\ndescription: "a\\qb"\n---\n', "frontmatter line 2: unsupported escape '\\q'"),
        ):
            self.assertEqual(self.read(text), MemoryProblem("note", reason))

    def test_reader_keeps_unknown_keys_out_of_the_way(self):
        memory = self.read(
            "---\nname: x\ndescription: y\nfuture_key: z\nmetadata:\n  type: user\n"
            "  new_field: 1\n---\n"
        )
        assert isinstance(memory, Memory)
        self.assertEqual((memory.description, memory.type), ("y", "user"))


class TestDiscovery(unittest.TestCase):
    def test_dir_name_docs_rule(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(claude_project_dir_name("/tmp/a_b.c"), "-tmp-a-b-c")

    def test_project_dir_name_env(self):
        env = {"CLAUDE_CODE_PROJECT_DIR_NAME": "my_project-1"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(claude_project_dir_name("/tmp/x"), "-tmp-x")
        with mock.patch.dict(os.environ, {**env, "CLAUDE_CONFIG_DIR": "/tmp/c"}, clear=True):
            self.assertEqual(claude_project_dir_name("/tmp/x"), "my_project-1")
        bad = {"CLAUDE_CODE_PROJECT_DIR_NAME": "a/b", "CLAUDE_CONFIG_DIR": "/tmp/c"}
        with mock.patch.dict(os.environ, bad, clear=True):
            self.assertEqual(claude_project_dir_name("/tmp/x"), "-tmp-x")


class TestCurrentProject(MemoryFixture):
    def test_dir_name_long_path_glob(self):
        deep = self.root / ("d" * 120) / ("e" * 120)
        deep.mkdir(parents=True)
        self.chdir(deep)
        with mock.patch.object(claude_memory, "project_root", return_value=deep):
            name = claude_project_dir_name(deep)
            self.assertGreater(len(name), 200)
            code, _, err = self.run_import("--dry-run", memory_dir=False)
            self.assertEqual(code, 1)
            self.assertIn("Error: no Claude memory directory at ", err)
            first = self.projects / f"{name[:200]}-hash1" / "memory"
            shutil.copytree(FIXTURES, first)
            code, out, err = self.run_import("--dry-run", memory_dir=False)
            self.assertEqual(code, 0, err)
            self.assertIn("Dry run: would import 3 memories; nothing written", out)
            shutil.copytree(FIXTURES, self.projects / f"{name[:200]}-hash2" / "memory")
            code, _, err = self.run_import("--dry-run", memory_dir=False)
        self.assertEqual(code, 1)
        self.assertEqual(
            err,
            f"Error: more than one project directory starts with {name[:200]}; "
            "pass --memory-dir DIR\n",
        )

    def test_missing_directory_names_the_flag_and_code(self):
        empty = self.root / "elsewhere"
        empty.mkdir()
        self.chdir(empty)
        with mock.patch.object(claude_memory, "project_root", return_value=empty):
            code, out, err = self.run_import(memory_dir=False)
        expected = self.projects / claude_project_dir_name(empty) / "memory"
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(
            err.splitlines(),
            [
                f"Error: no Claude memory directory at {expected} for {empty}; "
                "pass --memory-dir DIR",
                "code: MEMORY_DIR_MISSING (run 'cairn errors MEMORY_DIR_MISSING')",
            ],
        )

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_worktree_resolves_main_root(self):
        main = self.root / "repo" / "main"
        main.mkdir(parents=True)
        git = ["git", "-c", "user.name=x", "-c", "user.email=x@y.z"]

        def run(*argv: str, cwd: Path = main) -> None:
            subprocess.run([*git, *argv], cwd=cwd, check=True, capture_output=True)

        run("init", "-q")
        run("commit", "-q", "--allow-empty", "-m", "init")
        linked = self.root / "repo" / "linked"
        run("worktree", "add", "-q", str(linked))
        (linked / "sub").mkdir()
        resolved = claude_memory.project_root(linked / "sub")
        self.assertEqual(resolved.resolve(), main.resolve())
        self.assertEqual(claude_memory.project_root(self.root).resolve(), self.root.resolve())
        target = self.projects / claude_project_dir_name(resolved) / "memory"
        shutil.copytree(FIXTURES, target)
        self.chdir(linked / "sub")
        code, out, err = self.run_import("--dry-run", "--json", memory_dir=False)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["directories"][0]["memory_dir"], _home(target))
        self.assertEqual(data["added"], 3)


class TestImport(MemoryFixture):
    def test_mapping_kinds(self):
        self.write_memory("prefers-short", memory_text("Prefers short replies", type_="user"))
        code, data, _ = self.run_json()
        self.assertEqual(code, 0)
        by_memory = {row["memory"]: row for row in data["rows"]}
        self.assertEqual(by_memory["example-app/release-train"]["kind"], "decision")
        self.assertEqual(by_memory["example-app/flat-form"]["kind"], "decision")
        self.assertEqual(by_memory["example-app/prefers-short"]["kind"], "fact")
        self.assertEqual(by_memory["example-app/api-docs"]["action"], "reference")
        self.assertEqual(data["skipped_reference"], 1)
        claims = [r.claim for r in self.rows(self.project_doc, None)]
        self.assertNotIn("The widget API docs live in the docs/api folder", claims)

    def test_include_reference(self):
        code, data, _ = self.run_json("--include-reference")
        self.assertEqual(code, 0)
        row = next(r for r in data["rows"] if r["memory"] == "example-app/api-docs")
        self.assertEqual((row["action"], row["kind"]), ("added", "fact"))
        self.assertEqual(data["skipped_reference"], 0)

    def test_since_from_modified_then_mtime(self):
        path = self.write_memory("no-modified", memory_text("No stamp here", modified=None))
        stamp = 1_700_000_000  # 2023-11-14 UTC
        os.utime(path, (stamp, stamp))
        self.write_memory(
            "late", memory_text("Late in the day", modified="2026-10-30T23:30:00-02:00")
        )
        _, data, _ = self.run_json()
        since = {row["memory"]: row["since"] for row in data["rows"]}
        self.assertEqual(since["example-app/no-modified"], "2023-11-14")
        self.assertEqual(since["example-app/late"], "2026-10-31")  # the UTC date
        self.assertEqual(since["example-app/release-train"], "2026-10-30")

    def test_long_description_skipped(self):
        path = self.write_memory("too-long", memory_text("x" * 501))
        code, data, err = self.run_json()
        self.assertEqual((code, data["invalid"]), (1, 1))
        self.assertIn(
            f"Warning: {path}: claim is longer than 500 characters (got 501); skipped\n", err
        )
        self.assertEqual(data["added"], 3)  # the others are still imported

    def test_invalid_type_and_description_warn(self):
        self.write_memory("odd-type", memory_text("x", type_="diary"))
        self.write_memory("no-type", memory_text("y", type_=None))
        self.write_memory("strike", memory_text("a ~~b~~ c"))
        code, data, err = self.run_json()
        self.assertEqual((code, data["invalid"]), (1, 3))
        self.assertIn("odd-type.md: unknown type 'diary'; skipped", err)
        self.assertIn("no-type.md: no type; skipped", err)
        self.assertIn(
            "strike.md: claim must not contain '~~' (it marks a struck row); skipped", err
        )

    def test_place_in_exported_session(self):
        before = self.session.read_text()
        code, out, err = self.run_import()
        self.assertEqual(code, 0, err)
        row = "| 1 | Ship each change as its own PR off a fresh main | decision | medium |"
        row += " 2026-10-30 |  | memory:example-app/release-train |"
        after = self.session.read_text()
        self.assertEqual(
            after,
            before.replace(
                "\n\n## Conversation\n",
                f"\n\n## Decisions\n\n{decisions.HEADER}\n{decisions.SEPARATOR}\n{row}\n"
                "\n## Conversation\n",
                1,
            ),
        )
        self.assertIn(
            "Added claude:3f2a9c1e#1  decision  2026-10-30  memory:example-app/release-train  "
            "Ship each change as its own PR off a fresh main\n",
            out,
        )
        self.assertIn(
            "Imported 3 memories (1 into sessions, 2 into project documents); "
            "skipped 0 existing, 0 withdrawn, 1 reference, 0 invalid\n",
            out,
        )

    def test_unknown_session_goes_to_project_doc(self):
        code, _, err = self.run_import()
        self.assertEqual(code, 0)
        self.assertIn(
            f"Warning: session {UNEXPORTED[:8]} of memory example-app/flat-form is not exported; "
            "added to decisions/example-app.md\n",
            err,
        )
        self.assertIn(
            f"Warning: collections.decisions is not configured; QMD will not index {self.project_doc}\n",
            err,
        )
        text = self.project_doc.read_text()
        memory_dir = self.memory_dir_value
        self.assertTrue(
            text.startswith(
                "---\ntype: claude-memory-decisions\nnative_id: memory:example-app\n"
                'title: "Claude memory: example-app"\nproject: example-app\n'
                f"memory_dir: {memory_dir}\nprovenance: agent\n---\n\n"
                "# Claude memory: example-app\n\n## My Notes\n\n"
                "Rows imported from Claude Code auto memory by cairn claude memory import.\n\n"
                f"## Decisions\n\n{decisions.HEADER}\n{decisions.SEPARATOR}\n"
            ),
            text,
        )
        rows = self.rows(self.project_doc, None)
        self.assertEqual(
            [(r.number, r.claim, r.kind, r.since, r.source) for r in rows],
            [
                (
                    1,
                    "The widget service keeps its queue in Postgres",
                    "decision",
                    "2026-10-12",
                    "memory:example-app/flat-form",
                ),
                (
                    2,
                    'Say "done" only after the C:\\build step passes',
                    "fact",
                    datetime.fromtimestamp((self.memory / "quoted-escapes.md").stat().st_mtime, UTC)
                    .date()
                    .isoformat(),
                    "memory:example-app/quoted-escapes",
                ),
            ],
        )

    def test_rerun_adds_nothing(self):
        self.assertEqual(self.run_import()[0], 0)
        snapshot = (self.session.read_bytes(), self.project_doc.read_bytes())
        code, data, err = self.run_json()
        self.assertEqual((code, data["added"], data["skipped_existing"]), (0, 0, 3))
        self.assertEqual(snapshot, (self.session.read_bytes(), self.project_doc.read_bytes()))
        self.assertEqual(err, "")

    def test_changed_description_warns(self):
        self.assertEqual(self.run_import()[0], 0)
        self.write_memory(
            "release-train",
            memory_text("Ship each change as a stacked PR", type_="feedback", origin=SID),
        )
        before = self.session.read_bytes()
        code, data, err = self.run_json()
        self.assertEqual((code, data["changed"], data["skipped_existing"]), (0, 1, 3))
        self.assertIn(
            "Warning: memory example-app/release-train changed since it was imported as "
            "claude:3f2a9c1e#1; supersede that row to update it\n",
            err,
        )
        self.assertEqual(self.session.read_bytes(), before)

    def test_deleted_memory_keeps_row(self):
        self.assertEqual(self.run_import()[0], 0)
        (self.memory / "flat-form.md").unlink()
        self.assertEqual(self.run_import()[0], 0)
        _, data, _ = self.recall("--json", "--source", "memory")
        self.assertIn("memory:example-app/flat-form", [r["source"] for r in data["decisions"]])

    def test_withdrawn_fingerprint_skipped(self):
        ledger = withdrawals.ledger_path(config.get_config())
        fp = withdrawals.fingerprint("The widget service keeps its queue in Postgres")
        withdrawals.withdraw(
            ledger, fp, source="claude", session_id=SID, row=9, reason="hallucination"
        )
        before = ledger.read_bytes()
        code, data, _ = self.run_json()
        self.assertEqual((code, data["skipped_withdrawn"], data["added"]), (0, 1, 2))
        self.assertEqual(ledger.read_bytes(), before)
        claims = [r.claim for r in self.rows(self.project_doc, None)]
        self.assertNotIn("The widget service keeps its queue in Postgres", claims)

    def test_dry_run_writes_nothing(self):
        before = self.tree_hash()
        code, out, _ = self.run_import("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self.tree_hash(), before)
        self.assertFalse(self.project_doc.exists())
        self.assertIn(
            "Would add claude:3f2a9c1e  decision  2026-10-30  memory:example-app/release-train", out
        )
        self.assertTrue(out.endswith("Dry run: would import 3 memories; nothing written\n"))

    def test_json_summary_shape(self):
        self.write_memory("broken", "no frontmatter\n")
        code, data, _ = self.run_json()
        self.assertEqual(code, 1)
        self.assertEqual(
            sorted(data),
            sorted(
                [
                    "schema_version",
                    "dry_run",
                    "directories",
                    "added",
                    "skipped_existing",
                    "skipped_withdrawn",
                    "skipped_reference",
                    "unknown_session",
                    "changed",
                    "invalid",
                    "failed",
                    "rows",
                ]
            ),
        )
        self.assertEqual((data["schema_version"], data["dry_run"]), (1, False))
        self.assertEqual(
            data["directories"],
            [
                {
                    "memory_dir": self.memory_dir_value,
                    "label": "example-app",
                    "project": "example-app",
                }
            ],
        )
        self.assertEqual((data["added"], data["unknown_session"], data["invalid"]), (3, 2, 1))
        actions = {r["memory"]: r["action"] for r in data["rows"]}
        self.assertEqual(
            actions,
            {
                "example-app/api-docs": "reference",
                "example-app/broken": "invalid",
                "example-app/flat-form": "added",
                "example-app/quoted-escapes": "added",
                "example-app/release-train": "added",
            },
        )
        train = next(r for r in data["rows"] if r["memory"] == "example-app/release-train")
        self.assertEqual(
            train,
            {
                "memory": "example-app/release-train",
                "action": "added",
                "placement": "session",
                "ref": f"claude:{SID}#1",
                "kind": "decision",
                "since": "2026-10-30",
                "path": str(self.session),
                "changed": False,
                "reason": None,
            },
        )
        flat = next(r for r in data["rows"] if r["memory"] == "example-app/flat-form")
        self.assertEqual((flat["placement"], flat["ref"]), ("project", "memory:example-app#1"))

    def test_session_without_my_notes_fails_without_fallback(self):
        self.session.write_text(doc().replace("## My Notes", "## Other"), encoding="utf-8")
        code, data, err = self.run_json()
        self.assertEqual((code, data["failed"]), (1, 1))
        self.assertIn("has no ## My Notes section", err)
        claims = [r.source for r in self.rows(self.project_doc, None)]
        self.assertNotIn("memory:example-app/release-train", claims)

    def test_all_projects_and_memory_dir_are_exclusive(self):
        with self.assertRaises(SystemExit) as stop, redirect_stderr(io.StringIO()):
            self.run_import("--all-projects")
        self.assertEqual(stop.exception.code, 2)

    def test_all_projects_lists_every_memory_directory(self):
        shutil.copytree(FIXTURES, self.projects / "-tmp-other-app" / "memory")
        code, data, _ = self.run_import("--all-projects", "--json", memory_dir=False)
        self.assertEqual(code, 0)
        data = json.loads(data)
        self.assertEqual([d["label"] for d in data["directories"]], ["example-app", "other-app"])
        self.assertTrue((self.root / "data" / "decisions" / "other-app.md").exists())


class TestImportSafety(MemoryFixture):
    """Identity, cross-document deduplication, failure reporting and dry-run purity."""

    def test_shared_label_fails_every_colliding_directory(self):
        data = json.loads(self.config.read_text())
        data["path_resolution"] = {"home_slug_prefixes": ["-tmp-Code-", "-tmp-"]}
        self.config.write_text(json.dumps(data))
        config.get_config.cache_clear()
        shutil.copytree(FIXTURES, self.projects / "-tmp-Code-example-app" / "memory")
        code, out, _ = self.run_import("--all-projects", "--json", memory_dir=False)
        data = json.loads(out)
        self.assertEqual((code, data["added"]), (1, 0))
        self.assertEqual(data["failed"], 6)
        reasons = {r["reason"] for r in data["rows"] if r["action"] == "failed"}
        self.assertEqual(
            reasons,
            {
                "label example-app is shared by 2 memory directories; "
                "import one of them with --memory-dir DIR"
            },
        )
        self.assertFalse(self.project_doc.exists())
        self.assertNotIn("memory:", self.session.read_text())

    def test_project_document_of_another_directory_is_refused(self):
        self.assertEqual(self.run_import()[0], 0)
        other = self.root / "copy" / "example-app"
        shutil.copytree(FIXTURES, other)
        (other / "fresh.md").write_text(memory_text("A fresh note"), encoding="utf-8")
        before = self.project_doc.read_bytes()
        code, out, _ = self.run_import("--json", "--memory-dir", str(other), memory_dir=False)
        data = json.loads(out)
        self.assertEqual((code, data["added"]), (1, 0))
        fresh = next(r for r in data["rows"] if r["memory"] == "example-app/fresh")
        self.assertEqual(fresh["action"], "failed")
        self.assertIn(f"was imported from {self.memory_dir_value}", fresh["reason"])
        self.assertEqual(self.project_doc.read_bytes(), before)

    def test_scan_runs_under_the_import_lock(self):
        held = []
        scan = claude_memory._existing_markers

        def probe(*args, **kwargs):
            lock = claude_memory.import_lock(config.get_config())
            try:
                with claude_memory.locked(lock, blocking=False):
                    held.append(False)
            except BlockingIOError:
                held.append(True)
            return scan(*args, **kwargs)

        with mock.patch.object(claude_memory, "_existing_markers", probe):
            self.assertEqual(self.run_import()[0], 0)
        self.assertEqual(held, [True])

    def test_marker_in_a_session_of_another_project_is_found(self):
        self.assertEqual(self.run_import()[0], 0)
        self.write_memory(
            "release-train",
            memory_text("Ship each change as its own PR off a fresh main", origin=UNEXPORTED),
        )
        code, data, _ = self.run_json()
        self.assertEqual((code, data["added"], data["skipped_existing"]), (0, 0, 3))
        sources = [r.source for r in self.rows(self.project_doc, None)]
        self.assertNotIn("memory:example-app/release-train", sources)

    def test_damaged_previous_table_blocks_the_add(self):
        self.assertEqual(self.run_import()[0], 0)
        text = self.project_doc.read_text()
        self.project_doc.write_text(text.replace("| # |", "| no |", 1), encoding="utf-8")
        exported = self.write_doc("2026-10-29-9b1d07aa.md", doc(UNEXPORTED, rows=None))
        before = exported.read_bytes()
        code, data, err = self.run_json()
        self.assertEqual(code, 1)
        flat = next(r for r in data["rows"] if r["memory"] == "example-app/flat-form")
        self.assertEqual(flat["action"], "failed")
        self.assertIn("cannot check", flat["reason"])
        self.assertIn(f"Error: cannot check {self.project_doc} for imported memories", err)
        self.assertEqual(exported.read_bytes(), before)

    def test_target_that_is_not_utf8_fails_with_a_summary(self):
        self.project_doc.parent.mkdir(parents=True)
        self.project_doc.write_bytes(b"---\ntype: claude-memory-decisions\n\xff\n---\n")
        code, data, _ = self.run_json()
        self.assertEqual(code, 1)
        failed = [r for r in data["rows"] if r["action"] == "failed"]
        self.assertEqual(len(failed), 2)
        self.assertEqual({r["reason"] for r in failed}, {f"{self.project_doc} is not UTF-8"})
        train = next(r for r in data["rows"] if r["memory"] == "example-app/release-train")
        self.assertEqual(train["action"], "added")  # a later group still runs

    def test_apply_refusal_sets_reason(self):
        self.session.write_text(doc().replace("## My Notes", "## Other"), encoding="utf-8")
        _, data, _ = self.run_json()
        train = next(r for r in data["rows"] if r["memory"] == "example-app/release-train")
        self.assertEqual(train["action"], "failed")
        self.assertIn("has no ## My Notes section", train["reason"])

    def test_dry_run_writes_no_cache_or_lock(self):
        def files() -> dict[str, bytes]:
            return {
                str(p): p.read_bytes()
                for p in sorted(self.root.rglob("*"))
                if p.is_file() and not p.is_relative_to(self.root / "data" / "logs")
            }

        before = files()
        code, _, _ = self.run_import("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(files(), before)

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_recall_from_a_subdirectory_lists_the_project_document(self):
        main = self.root / "repo" / "example-app"
        (main / "sub").mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=main, check=True)
        target = (
            self.projects / claude_project_dir_name(claude_memory.project_root(main)) / "memory"
        )
        shutil.copytree(FIXTURES, target)
        self.chdir(main / "sub")
        self.assertEqual(self.run_import(memory_dir=False)[0], 0)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = recall.main(["decisions", "--source", "memory", "--json"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(len(json.loads(out.getvalue())["decisions"]), 2)


if __name__ == "__main__":
    unittest.main()
