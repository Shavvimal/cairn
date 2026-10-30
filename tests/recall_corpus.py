"""Build the recall corpus: invented exported documents for the retrieval-quality test.

``tests/fixtures/recall-corpus.spec.json`` describes 33 documents as data (source, ids,
project, title, turns). This module turns them into the exported store under
``tests/fixtures/recall-corpus/`` with the real exporters: every session goes through
the source's ``render_document`` and the base ``export_one`` inside a temporary home, so
the frontmatter, the metadata block, the provenance and exporter stamps, the file name
and the ``<!-- cairn:turn N -->`` lengths are what an export writes today. Two documents
cannot come from an exporter and are composed from the same shared helpers: the
pre-schema file (no ``cairn_schema`` and no ``activity_dates``, as written before schema
2) and the peer-exchange document (``render_exchange``).

Rewrite the committed corpus after a renderer change, then review the diff::

    uv run python -m tests.recall_corpus --write
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import patch

from cairn import config
from cairn.exchange import ThreadState, exchange_document_path, render_exchange
from cairn.frontmatter import head_close, head_open, notes_block, preserved_tail, title_line
from cairn.paths import session_dir_of, subagent_path
from cairn.rendering import render_conversation
from cairn.schema import Message, SessionData
from cairn.sources.base import ConversationSource

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SPEC = FIXTURES / "recall-corpus.spec.json"
CORPUS = FIXTURES / "recall-corpus"
COLLECTIONS = {
    "claude": "claude-code-sessions",
    "codex": "codex-sessions",
    "cursor": "cursor-sessions",
}
CWD_ROOT = "/tmp/corpus"
REVISION = "fixture"


def load_spec(path: Path = SPEC) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError(f"{path.name}: schema_version must be 1")
    return list(data["documents"])


def messages(doc: dict[str, Any]) -> list[Message]:
    return [{"role": role, "timestamp": ts, "text": text} for role, ts, text in doc["turns"]]


def _stamp(message: Message) -> str:
    stamp = message.get("timestamp")
    if not stamp:
        raise ValueError(f"a corpus turn has no timestamp: {message}")
    return stamp


def location(doc: dict[str, Any]) -> dict[str, str | None]:
    """cwd, project, repo label and repo folder of one document."""
    project, repo = doc["project"], doc.get("repo")
    return {
        "cwd": f"{CWD_ROOT}/{project}/{repo}" if repo else f"{CWD_ROOT}/{project}",
        "project": project,
        "repo": f"{project}-{repo}" if repo else project,
        "repo_subdir": repo,
    }


@contextmanager
def temp_home() -> Iterator[Path]:
    """A throwaway home and config, so export_one writes nowhere real."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        cfg = root / "config.json"
        cfg.write_text(
            json.dumps(
                {
                    "data_root": str(root / "data"),
                    "qmd_binary": "/missing/qmd",
                    "collections": {
                        name: {"store": str(root / "stores" / name)}
                        for name in COLLECTIONS.values()
                    },
                    "project_groups": {},
                    "repo_catalog": {},
                    "project_descriptions": {},
                }
            ),
            encoding="utf-8",
        )
        env = {
            "HOME": str(root / "home"),
            "CAIRN_CONFIG": str(cfg),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_CONFIG_HOME": str(root / "xdg"),
            "XDG_STATE_HOME": str(root / "state"),
        }
        with patch.dict(os.environ, env):
            config.get_config.cache_clear()
            try:
                yield root
            finally:
                config.get_config.cache_clear()


def _sources(data_root: Path) -> dict[str, ConversationSource]:
    from cairn.sources.claude import ClaudeSource
    from cairn.sources.codex import CodexSource
    from cairn.sources.cursor import CursorSource

    result: dict[str, ConversationSource] = {
        "claude": ClaudeSource(),
        "codex": CodexSource(),
        "cursor": CursorSource(),
    }
    for name, source in result.items():
        source.output_dir = data_root / COLLECTIONS[name]
    return result


def _claude(doc: dict[str, Any], msgs: list[Message], refs: list[dict]) -> dict[str, Any]:
    loc = location(doc)
    return {
        "session_id": doc["id"],
        "date": _stamp(msgs[0])[:10],
        "repo": loc["repo"],
        "git_branch": doc.get("branch"),
        "summary": "",
        "skills": [],
        "messages": len(msgs),
        "messages_ordered": msgs,
        "files_created": [],
        "files_modified": doc.get("files_modified", []),
        "subagents": refs,
        "title": doc["title"],
        "cwd": loc["cwd"],
        "project": loc["project"],
        "repo_subdir": loc["repo_subdir"],
        "first_timestamp": _stamp(msgs[0]),
        "last_timestamp": _stamp(msgs[-1]),
        "source_revision": REVISION,
    }


def _codex(doc: dict[str, Any], msgs: list[Message], refs: list[dict]) -> dict[str, Any]:
    loc = location(doc)
    return {
        "session_id": doc["id"],
        "date": _stamp(msgs[0])[:10],
        "repo": loc["repo"],
        "branch": doc.get("branch"),
        "model": doc.get("model"),
        "message_count": len(msgs),
        "messages": msgs,
        "subagents": refs,
        "title": doc["title"],
        "cwd": loc["cwd"],
        "project": loc["project"],
        "repo_subdir": loc["repo_subdir"],
        "first_timestamp": _stamp(msgs[0]),
        "last_timestamp": _stamp(msgs[-1]),
        "source_revision": REVISION,
    }


def _cursor(doc: dict[str, Any], msgs: list[Message]) -> dict[str, Any]:
    loc = location(doc)
    return {
        "composer_id": doc["id"],
        "date": _stamp(msgs[0])[:10],
        "repo": loc["repo"],
        "mode": doc.get("mode"),
        "model": doc.get("model"),
        "title": doc["title"],
        "branch": doc.get("branch"),
        "messages": len(msgs),
        "messages_ordered": msgs,
        "files_created": [],
        "files_modified": doc.get("files_modified", []),
        "status": "active",
        "cwd": loc["cwd"],
        "project": loc["project"],
        "repo_subdir": loc["repo_subdir"],
        "created_at": _stamp(msgs[0]),
        "last_activity": _stamp(msgs[-1]),
        "source_revision": REVISION,
    }


def _pre_schema(doc: dict[str, Any], msgs: list[Message]) -> str:
    """A Claude export written before schema 2: no metadata block, no provenance."""
    loc = location(doc)
    head = head_open("claude-session", _stamp(msgs[0])[:10], "session_id", doc["id"])
    head += [f"repo: {loc['repo']}", title_line(doc["title"])]
    head += head_close(doc["last_activity"], len(msgs))
    lines = [*head, *preserved_tail(None), "---", "", f"# {doc['title']}", ""]
    lines += notes_block(None, "syncs")
    lines += render_conversation(msgs)
    return "\n".join(lines)


def _exchange(doc: dict[str, Any], anchor: dict[str, Any]) -> ThreadState:
    caller = f"claude:{anchor['id']}"
    return ThreadState(
        peer="codex",
        thread_id=doc["id"],
        cwd=str(location(doc)["cwd"]),
        profile="review",
        native_policy=["--sandbox", "read-only"],
        model="gpt-5-codex",
        caller_session=caller,
        anchor_session=caller,
        created_at=doc["started_at"],
        rounds=[
            {
                "round": 1,
                "prompt": doc["prompt"],
                "started_at": doc["started_at"],
                "finished_at": doc["finished_at"],
                "outcome": "ok",
                "last_message": doc["answer"],
                "duration_s": 300.0,
                "exit_code": 0,
                "caller_session": caller,
            }
        ],
    )


def build(spec: list[dict[str, Any]] | None = None) -> dict[str, bytes]:
    """Every corpus file, keyed by its path relative to the corpus root."""
    spec = load_spec() if spec is None else spec
    by_key = {doc["key"]: doc for doc in spec}
    children: dict[str, list[dict[str, Any]]] = {}
    for doc in spec:
        if doc.get("kind") == "subagent":
            children.setdefault(doc["parent"], []).append(doc)
    written: dict[str, Path] = {}
    with temp_home() as root:
        data_root = root / "data"
        sources = _sources(data_root)
        for doc in spec:
            kind = doc.get("kind", "session")
            msgs = messages(doc)
            if kind == "pre-schema":
                source = sources["claude"]
                path = source.output_path(
                    doc["id"], _stamp(msgs[0])[:10], doc["project"], doc.get("repo")
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(_pre_schema(doc, msgs).encode("utf-8"))
                written[doc["key"]] = path
                continue
            if kind == "exchange":
                parent_doc = written[doc["anchor"]]
                state = _exchange(doc, by_key[doc["anchor"]])
                path = exchange_document_path(session_dir_of(parent_doc), "codex", doc["id"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(render_exchange(state, None, None).encode("utf-8"))
                written[doc["key"]] = path
                continue
            source = sources[doc["source"]]
            if kind == "subagent":
                parent_doc = written[doc["parent"]]
                data = _child(doc, msgs, by_key[doc["parent"]], parent_doc, source)
                short = data.pop("_short")
                target: Path | None = subagent_path(session_dir_of(parent_doc), short)
            else:
                data = _parent(doc, msgs, children.get(doc["key"], []), source)
                target = None
            path_or_none = source.export_one(doc["id"], cast(SessionData, data), target)
            if path_or_none is None:
                raise ValueError(f"{doc['key']}: export_one refused the document")
            written[doc["key"]] = path_or_none
        result = {}
        for key, path in written.items():
            rel = path.relative_to(data_root).as_posix()
            if rel in result:
                raise ValueError(f"{key}: two documents share {rel}")
            result[rel] = path.read_bytes()
    return result


def _parent(
    doc: dict[str, Any],
    msgs: list[Message],
    kids: list[dict[str, Any]],
    source: ConversationSource,
) -> dict[str, Any]:
    """Session data, with the ``## Subagents`` index its exporter would render."""
    if doc["source"] == "cursor":
        return _cursor(doc, msgs)
    planned = source.output_path(doc["id"], _stamp(msgs[0])[:10], *_folder(doc))
    if doc["source"] == "claude":
        refs = [
            {
                "agent_id": kid["agent_id"],
                "title": kid["title"],
                "agent_type": kid.get("agent_type"),
                "request_shape": "task",
                "finished": True,
                "path": f"./{planned.stem}/agent-{kid['agent_id']}.md",
            }
            for kid in kids
        ]
        return _claude(doc, msgs, refs)
    refs = [
        {
            "agent_id": kid["id"],
            "title": kid["title"],
            "agent_type": kid.get("agent_type"),
            "finished": True,
            "path": f"{planned.stem}/agent-{source.short_id(kid['id'])}.md",
        }
        for kid in kids
    ]
    return _codex(doc, msgs, refs)


def _folder(doc: dict[str, Any]) -> tuple[str, str | None]:
    return doc["project"], doc.get("repo")


def _child(
    doc: dict[str, Any],
    msgs: list[Message],
    parent: dict[str, Any],
    parent_doc: Path,
    source: ConversationSource,
) -> dict[str, Any]:
    """A subagent document: the parent's location and lineage, its own transcript."""
    if doc["source"] == "claude":
        data = _claude(doc, msgs, [])
        data.update(
            agent_id=doc["agent_id"],
            parent_session_id=parent["id"],
            agent_type=doc.get("agent_type"),
            description=doc["title"],
            request_shape="task",
            finished=True,
            git_branch=parent.get("branch"),
            parent_document=parent_doc.name,
            _short=doc["agent_id"],
        )
        return data
    data = _codex(doc, msgs, [])
    data.update(
        parent_session_id=parent["id"],
        spawn_depth=1,
        agent_type=doc.get("agent_type"),
        finished=True,
        branch=parent.get("branch"),
        _short=source.short_id(doc["id"]),
    )
    return data


def write(root: Path = CORPUS) -> list[str]:
    """Replace the three collection folders under ``root`` with a fresh build."""
    files = build()
    for name in COLLECTIONS.values():
        shutil.rmtree(root / name, ignore_errors=True)
    for rel, content in sorted(files.items()):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return sorted(files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.recall_corpus",
        description="Rebuild tests/fixtures/recall-corpus from recall-corpus.spec.json.",
    )
    parser.add_argument("--write", action="store_true", help="rewrite the committed corpus")
    args = parser.parse_args(argv)
    files = write() if args.write else sorted(build())
    for rel in files:
        print(rel)
    print(f"{len(files)} files" + (" written" if args.write else " (dry run; pass --write)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
