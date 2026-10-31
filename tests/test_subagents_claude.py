"""Claude subagent transcripts: child documents, the parent index, peer turns, hooks, recall."""

from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path

from cairn.frontmatter import parse_frontmatter
from cairn.rendering import render_conversation
from tests.test_shared_store import StoreFixture

PARENT = "p1"
T0 = "2026-03-28T10:00:00Z"


def _user(text, ts, **extra):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}, **extra}


def _assistant(content, ts, stop="end_turn", **extra):
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {"role": "assistant", "content": content, "stop_reason": stop},
        **extra,
    }


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Compact separators, as Claude Code writes them (the line-skip marker relies on it).
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records))
    return path


class SubagentFixture(StoreFixture):
    def setUp(self):
        super().setUp()
        self.project_dir = self.claude / "-demo"
        self.parent_jsonl = self.write_parent()
        self.subagents = self.project_dir / PARENT / "subagents"
        self.write_child("a1", meta=self.meta_a1(), final="Orchard report ORCHARDCHILDONLY")
        self.write_child(
            "a2",
            folder=self.subagents / "workflows" / "wf_abc",
            meta={
                "agentType": "workflow-subagent",
                "description": "features:pass2",
                "workflowPhase": "Write",
                "spawnDepth": 1,
                "requestShape": "foreground",
            },
        )
        (self.subagents / "workflows" / "wf_abc" / "journal.jsonl").write_text('{"x": 1}\n')
        self.write_child("a3", meta=None, stop="tool_use")
        self.write_child(
            "a4",
            meta={"agentType": "Explore", "description": "Stopped scout", "stoppedByUser": True},
            stop="tool_use",
        )
        self.write_child(
            "af",
            meta={
                "agentType": "fork",
                "description": "Forked scout",
                "isFork": True,
                "parentAgentId": "a1",
                "finished": True,
                "requestShape": "background",
            },
        )

    @staticmethod
    def meta_a1(**overrides):
        meta = {
            "agentType": "general-purpose",
            "description": 'Scout the "orchard"',
            "toolUseId": "toolu_1",
            "spawnDepth": 1,
            "requestShape": "background",
            "model": "opus",
            "finished": True,
        }
        meta.update(overrides)
        return meta

    def write_parent(self):
        common = {"sessionId": PARENT, "cwd": self.cwd, "gitBranch": "main"}
        agent_call = {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "Agent",
            "input": {
                "description": "Scout the orchard",
                "subagent_type": "general-purpose",
                "prompt": "Scout the orchard",
                "run_in_background": True,
            },
        }
        records = [
            _user("Plan the orchard work", "2026-03-28T10:00:00Z", **common),
            _assistant([agent_call], "2026-03-28T10:00:01Z", stop="tool_use", **common),
            {
                **common,
                "type": "user",
                "timestamp": "2026-03-28T10:00:02Z",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "launched"}
                    ],
                },
            },
            _user("SIDECHAIN LEAK", "2026-03-28T10:00:03Z", isSidechain=True, **common),
            _user(
                "Another Claude session sent a message:\n"
                '<agent-message from="agent-a1">Halfway there</agent-message>',
                "2026-03-28T10:00:04Z",
                isMeta=True,
                origin={
                    "kind": "peer",
                    "from": "agent-a1",
                    "name": "orchard-scout",
                    "body": "Halfway: PEERBODY three trees",
                },
                **common,
            ),
            _user(
                "<task-notification><task-id>a1</task-id><tool-use-id>toolu_1</tool-use-id>"
                "<result>Final orchard report</result></task-notification>",
                "2026-03-28T10:00:05Z",
                origin={"kind": "task-notification"},
                **common,
            ),
            _assistant("All done", "2026-03-28T10:00:06Z", **common),
        ]
        return _write_jsonl(self.project_dir / f"{PARENT}.jsonl", records)

    def write_child(
        self,
        agent_id,
        meta,
        folder=None,
        stop="end_turn",
        final="Child report",
        session_id=PARENT,
        extra=(),
    ):
        folder = folder or self.subagents
        common = {
            "isSidechain": True,
            "agentId": agent_id,
            "sessionId": session_id,
            "cwd": "/elsewhere/other-repo",
        }
        records = [
            _user(
                f"Prompt for {agent_id}\nsecond line",
                "2026-03-28T10:01:00Z",
                parentUuid=None,
                **common,
            ),
            _assistant(
                [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}],
                "2026-03-28T10:01:01Z",
                stop="tool_use",
                **common,
            ),
            {
                **common,
                "type": "user",
                "timestamp": "2026-03-28T10:01:02Z",
                "toolUseResult": {"type": "create", "filePath": f"/elsewhere/{agent_id}.md"},
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "TOOLRESULT ok"}],
                },
            },
            {"type": "api-request-blob", "sessionId": session_id, "message": {"content": "BLOB"}},
            *extra,
            _assistant(final, "2026-03-28T10:01:03Z", stop=stop, **common),
        ]
        path = _write_jsonl(folder / f"agent-{agent_id}.jsonl", records)
        if meta is not None:
            (folder / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta))
        return path

    # ---- helpers ----
    def export_all(self):
        return self.run_cli("claude", "export", "--all")

    @property
    def out(self) -> Path:
        return self.root / "data" / "claude-code-sessions"

    def parent_doc(self) -> Path:
        docs = [p for p in self.out.rglob("*.md") if not p.name.startswith("agent-")]
        self.assertEqual(len(docs), 1, docs)
        return docs[0]

    def child_doc(self, agent_id) -> Path:
        return self.parent_doc().with_suffix("") / f"agent-{agent_id}.md"

    def fm(self, path: Path) -> dict:
        return parse_frontmatter(path.read_text())

    def stamps(self) -> dict[str, tuple[int, int]]:
        return {
            str(p.relative_to(self.out)): (p.stat().st_ino, p.stat().st_mtime_ns)
            for p in self.out.rglob("*.md")
        }


class TestChildDocuments(SubagentFixture):
    """T1: one claude-subagent document per child, with lineage in the head."""

    def test_children_land_in_the_parent_folder_with_head_fields(self):
        self.export_all()
        parent = self.parent_doc()
        children = sorted(p.name for p in parent.with_suffix("").glob("*.md"))
        self.assertEqual(
            children, ["agent-a1.md", "agent-a2.md", "agent-a3.md", "agent-a4.md", "agent-af.md"]
        )
        a1 = self.fm(self.child_doc("a1"))
        self.assertEqual(a1["type"], "claude-subagent")
        self.assertEqual(a1["session_id"], "agent-a1")
        self.assertEqual(a1["parent_session_id"], PARENT)
        self.assertEqual(a1["agent_id"], "a1")
        self.assertEqual(a1["agent_type"], "general-purpose")
        self.assertEqual(a1["description"], 'Scout the "orchard"')
        self.assertEqual(a1["title"], 'Scout the "orchard"')
        self.assertEqual(a1["tool_use_id"], "toolu_1")
        self.assertEqual(a1["spawn_depth"], "1")
        self.assertEqual(a1["request_shape"], "background")
        self.assertEqual(a1["model"], "opus")
        self.assertEqual(a1["git_branch"], "main")
        self.assertEqual(a1["status"], "done")
        self.assertNotIn("workflow_run", a1)
        # Lineage and location come from the parent, never from the child's cwd.
        parent_fm = self.fm(parent)
        self.assertEqual(a1["project"], parent_fm["project"])
        self.assertEqual(a1["cwd"], parent_fm["cwd"])
        self.assertEqual(a1["agent_cwd"], "/elsewhere/other-repo")

        a2 = self.fm(self.child_doc("a2"))
        self.assertEqual(a2["workflow_run"], "wf_abc")
        self.assertEqual(a2["workflow_phase"], "Write")
        self.assertNotIn("tool_use_id", a2)
        self.assertEqual(a2["status"], "done")  # no meta "finished": transcript end_turn

        a3 = self.fm(self.child_doc("a3"))
        self.assertEqual(a3["title"], "Prompt for a3")
        self.assertEqual(a3["status"], "active")
        self.assertNotIn("agent_type", a3)

        a4 = self.fm(self.child_doc("a4"))
        self.assertEqual(a4["stopped_by_user"], "true")
        self.assertEqual(a4["status"], "done")

        fork = self.fm(self.child_doc("af"))
        self.assertEqual(fork["is_fork"], "true")
        self.assertEqual(fork["parent_agent_id"], "a1")

    def test_child_body_has_parent_link_prompt_first_report_last(self):
        self.export_all()
        body = self.child_doc("a1").read_text()
        self.assertIn(f"Parent: [{PARENT}](../{self.parent_doc().name})", body)
        self.assertIn("`/elsewhere/a1.md`", body)  # Artifacts from toolUseResult
        conversation = body.split("## Conversation", 1)[1]
        self.assertLess(conversation.index("Prompt for a1"), conversation.index("TOOLRESULT"))
        self.assertTrue(conversation.rstrip().endswith("Orchard report ORCHARDCHILDONLY"))
        self.assertNotIn("BLOB", body)


class TestParentDocument(SubagentFixture):
    """T2: the parent index, the Agent turn, and no sidechain leakage."""

    def test_index_agent_turn_and_no_leakage(self):
        self.export_all()
        parent = self.parent_doc()
        text = parent.read_text()
        stem = parent.stem
        self.assertIn(
            f'- [Scout the "orchard"](./{stem}/agent-a1.md) - `agent-a1`, '
            "general-purpose, background, done",
            text,
        )
        self.assertIn(
            f"- [features:pass2](./{stem}/agent-a2.md) - `agent-a2`, "
            "workflow-subagent, workflow wf_abc, running",
            text,
        )
        self.assertIn(
            f"- [agent a3](./{stem}/agent-a3.md) - `agent-a3`, unknown, unknown, running", text
        )
        self.assertIn("`agent-a4`, Explore, unknown, stopped", text)
        self.assertLess(text.index("## Subagents"), text.index("## My Notes"))
        self.assertIn("### Agent - 2026-03-28T10:00:04Z", text)
        self.assertIn("from agent agent-a1 (orchard-scout):\n\nHalfway: PEERBODY three trees", text)
        self.assertIn("### User - 2026-03-28T10:00:05Z", text)  # task-notification
        self.assertIn("<result>Final orchard report</result>", text)
        self.assertNotIn("SIDECHAIN LEAK", text)
        self.assertNotIn("ORCHARDCHILDONLY", text)
        fm = self.fm(parent)
        # Plan prompt + task-notification; the peer message is not user input.
        self.assertEqual(fm["user_message_count"], "2")
        self.assertNotIn("parent_session_id", fm)


class TestRevisions(SubagentFixture):
    """T3: idempotence and which documents each native change rewrites."""

    def test_revision_rules(self):
        self.export_all()
        first = self.stamps()
        self.export_all()
        self.assertEqual(self.stamps(), first)

        parent_rel = str(self.parent_doc().relative_to(self.out))
        stem = parent_rel.removesuffix(".md")

        def changed(before):
            after = self.stamps()
            return {rel for rel in after if before.get(rel) != after[rel]}

        child = self.subagents / "agent-a1.jsonl"
        with child.open("a") as handle:
            handle.write(
                json.dumps(_assistant("Appended", "2026-03-28T10:02:00Z", agentId="a1")) + "\n"
            )
        before = self.stamps()
        self.export_all()
        self.assertEqual(changed(before), {f"{stem}/agent-a1.md"})

        self.write_child("a5", meta={"description": "Late child", "finished": True})
        before = self.stamps()
        self.export_all()
        self.assertEqual(changed(before), {parent_rel, f"{stem}/agent-a5.md"})
        self.assertIn("[Late child]", self.parent_doc().read_text())

        (self.subagents / "agent-a1.meta.json").write_text(json.dumps(self.meta_a1(finished=False)))
        before = self.stamps()
        self.export_all()
        self.assertEqual(changed(before), {parent_rel, f"{stem}/agent-a1.md"})
        self.assertIn("general-purpose, background, running", self.parent_doc().read_text())

    def test_any_rendered_index_field_rerenders_the_parent(self):
        """Regression: a change to request shape or workflow run alone must re-render."""
        self.export_all()
        parent_rel = str(self.parent_doc().relative_to(self.out))
        (self.subagents / "agent-a1.meta.json").write_text(
            json.dumps(self.meta_a1(requestShape="foreground"))
        )
        before = self.stamps()
        self.export_all()
        self.assertNotEqual(self.stamps()[parent_rel], before[parent_rel])
        self.assertIn("general-purpose, foreground, done", self.parent_doc().read_text())

        moved = self.subagents / "workflows" / "wf_new"
        moved.mkdir(parents=True)
        for name in ("agent-a2.jsonl", "agent-a2.meta.json"):
            (self.subagents / "workflows" / "wf_abc" / name).rename(moved / name)
        before = self.stamps()
        self.export_all()
        self.assertNotEqual(self.stamps()[parent_rel], before[parent_rel])
        self.assertIn("workflow wf_new", self.parent_doc().read_text())

    def test_running_child_moves_to_done_when_it_finishes(self):
        self.export_all()
        self.assertEqual(self.fm(self.child_doc("a3"))["status"], "active")
        self.write_child("a3", meta={"finished": True}, stop="end_turn")
        self.export_all()
        self.assertEqual(self.fm(self.child_doc("a3"))["status"], "done")


class TestWindow(SubagentFixture):
    """T4: a windowed export selects a parent by its newest child."""

    def test_old_parent_with_fresh_child_is_exported(self):
        old = time.time() - 10 * 86400
        for path in self.project_dir.rglob("*"):
            if path.is_file():
                os.utime(path, (old, old))
        quiet = self.native("claude", "p2")
        os.utime(quiet, (old, old))
        result = self.run_cli("claude", "export", "--since", "1d")
        self.assertIn("Found 0 sessions", result.stdout)
        os.utime(self.subagents / "agent-a1.jsonl")  # touch: now
        result = self.run_cli("claude", "export", "--since", "1d")
        self.assertIn("Found 1 sessions", result.stdout)
        self.assertTrue(self.child_doc("a1").exists())
        self.assertFalse(any("p2" in p.name for p in self.out.rglob("*.md")))


class TestHookExport(SubagentFixture):
    """T5: the hook path handles parent ids and agent- ids."""

    def hook(self, *args, check=True):
        return self.run_cli("hook", "export", "--source", "claude", *args, check=check)

    def test_agent_id_exports_parent_index_and_that_child(self):
        child = self.subagents / "agent-a1.jsonl"
        result = self.hook("--session-id", "agent-a1", "--transcript", str(child))
        self.assertEqual(Path(result.stdout.strip()), self.child_doc("a1"))
        self.assertIn("## Subagents", self.parent_doc().read_text())
        self.assertFalse(self.child_doc("a3").exists())
        # Without a transcript the child is found by id.
        self.child_doc("a1").unlink()
        result = self.hook("--session-id", "agent-a1")
        self.assertEqual(Path(result.stdout.strip()), self.child_doc("a1"))

    def test_parent_id_exports_the_family(self):
        result = self.hook("--session-id", PARENT, "--transcript", str(self.parent_jsonl))
        self.assertEqual(Path(result.stdout.strip()), self.parent_doc())
        for agent_id in ("a1", "a2", "a3", "a4", "af"):
            self.assertTrue(self.child_doc(agent_id).exists(), agent_id)

    def test_session_mismatch_fails_loud(self):
        bad = self.write_child("bad", meta={}, session_id="someone-else")
        result = self.hook("--session-id", "agent-bad", "--transcript", str(bad), check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not match parent", result.stderr)
        # The parent is written, then the hook fails so the durable job retries.
        result = self.hook("--session-id", PARENT, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("agent-bad", result.stderr)
        self.assertTrue(self.child_doc("a1").exists())
        result = self.run_cli("claude", "export", "--all", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("subagent agent-bad", result.stderr)


class TestVisibility(SubagentFixture):
    """T6: listing, recall, notes and resume on child ids."""

    def test_list_recall_note_expand_resume(self):
        self.export_all()
        listed = json.loads(self.run_cli("claude", "list", "--all", "--json").stdout)
        self.assertEqual([row["session_id"] for row in listed], [PARENT])

        base = ["recall", "list", "2026-03-28", "--all-projects", "--min-msgs", "1", "--json"]
        rows = json.loads(self.run_cli(*base).stdout)
        self.assertEqual([r["session_id"] for r in rows], [PARENT])
        self.assertEqual(rows[0]["type"], "claude-session")
        self.assertIsNone(rows[0]["parent"])
        rows = json.loads(self.run_cli(*base, "--include-subagents").stdout)
        children = {r["session_id"]: r for r in rows if r["parent"]}
        self.assertEqual(
            set(children), {"agent-a1", "agent-a2", "agent-a3", "agent-a4", "agent-af"}
        )
        self.assertEqual(children["agent-a1"]["parent"], PARENT)
        self.assertEqual(children["agent-a1"]["type"], "claude-subagent")
        text = self.run_cli(*base[:-1], "--include-subagents").stdout
        self.assertIn(f"(subagent of {PARENT})", text)

        expand = self.run_cli("recall", "expand", "claude:agent-a1", "--all-projects").stdout
        self.assertIn("ORCHARDCHILDONLY", expand)

        self.run_cli("claude", "note", "child note", "--session-id", "agent-a1")
        self.assertIn("child note", self.fm(self.child_doc("a1"))["comments"])
        self.assertNotIn("child note", self.parent_doc().read_text())

        result = self.run_cli("resume", "claude:agent-a1", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"claude:{PARENT}", result.stderr)
        result = self.run_cli("claude", "resume", str(self.child_doc("a1")), check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"claude:{PARENT}", result.stderr)


class TestExtraction(SubagentFixture):
    """T7 and T15: generator input, file operations, Agent rendering."""

    def test_generator_input_and_file_operations(self):
        from cairn.jsonl import iter_jsonl
        from cairn.sources.claude import extract_session_data

        child = self.subagents / "agent-a1.jsonl"
        data = extract_session_data((r for _, r in iter_jsonl(child)), sidechain=True)
        self.assertEqual(data["session_id"], "agent-a1")
        self.assertEqual(data["agent_id"], "a1")
        self.assertEqual(data["parent_session_id"], PARENT)
        self.assertTrue(data["finished"])
        self.assertEqual(data["files_created"], ["/elsewhere/a1.md"])
        self.assertEqual(data["messages_ordered"][0]["text"], "Prompt for a1\nsecond line")

        parent = extract_session_data(r for _, r in iter_jsonl(self.parent_jsonl))
        self.assertEqual(parent["session_id"], PARENT)
        self.assertNotIn("SIDECHAIN LEAK", json.dumps(parent["messages_ordered"]))
        agent_turns = [m for m in parent["messages_ordered"] if m["role"] == "agent"]
        self.assertEqual(len(agent_turns), 1)
        self.assertNotIn("Halfway: PEERBODY three trees", parent["user_messages"])

    def test_peer_without_body_strips_the_wrapper(self):
        from cairn.sources.claude import extract_session_data

        record = _user(
            'Another Claude session sent a message:\n<agent-message from="x">plain text</agent-message>',
            T0,
            isMeta=True,
            origin={"kind": "peer", "from": "x"},
        )
        data = extract_session_data([record])
        self.assertEqual(data["messages_ordered"][0]["text"], "from agent x:\n\nplain text")

    def test_agent_turn_renders_with_its_header(self):
        lines = render_conversation(
            [{"role": "agent", "timestamp": T0, "text": "from agent x:\n\nhi"}]
        )
        self.assertIn(f"### Agent - {T0}", "\n".join(lines))


class TestDiscovery(SubagentFixture):
    def test_jsonl_helpers(self):
        from cairn import jsonl

        files = jsonl.iter_subagent_files(self.parent_jsonl)
        self.assertEqual(
            [p.name for p in files],
            [
                "agent-a1.jsonl",
                "agent-a3.jsonl",
                "agent-a4.jsonl",
                "agent-af.jsonl",
                "agent-a2.jsonl",
            ],
        )
        self.assertEqual(jsonl.workflow_run_of(files[-1]), "wf_abc")
        self.assertIsNone(jsonl.workflow_run_of(files[0]))
        self.assertEqual(jsonl.subagent_parent_jsonl(files[-1]), self.parent_jsonl)
        self.assertEqual(
            jsonl.find_subagent_jsonl(self.claude, "a2"), (files[-1], self.parent_jsonl)
        )
        self.assertIsNone(jsonl.find_subagent_jsonl(self.claude, "zz"))
        self.assertEqual(jsonl.iter_session_files(self.claude), [self.parent_jsonl])
        skipped = [r for _, r in jsonl.iter_jsonl(files[0], ('"type":"api-request',))]
        self.assertFalse(any(r.get("type") == "api-request-blob" for r in skipped))

    def test_malformed_meta_fails_loud(self):
        (self.subagents / "agent-a1.meta.json").write_text("{not json")
        result = self.run_cli("claude", "export", "--all", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("malformed subagent meta", result.stderr)

    def test_malformed_meta_does_not_block_the_family(self):
        """Regression: one corrupt meta must not stop the parent or healthy siblings."""
        (self.subagents / "agent-a1.meta.json").write_text("{not json")
        result = self.run_cli("claude", "export", "--all", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("subagent agent-a1: malformed subagent meta", result.stderr)
        text = self.parent_doc().read_text()
        self.assertIn("[agent a1 (meta unreadable)]", text)
        self.assertFalse(self.child_doc("a1").exists())
        for agent_id in ("a2", "a3", "a4", "af"):
            self.assertTrue(self.child_doc(agent_id).exists(), agent_id)
        # The hook path writes the family, then fails so the durable job retries.
        self.child_doc("a2").unlink()
        hook = self.run_cli(
            "hook", "export", "--source", "claude", "--session-id", PARENT, check=False
        )
        self.assertEqual(hook.returncode, 1)
        self.assertIn("agent-a1", hook.stderr)
        self.assertTrue(self.child_doc("a2").exists())
        # Repairing the meta re-renders the index row and exports the child.
        (self.subagents / "agent-a1.meta.json").write_text(json.dumps(self.meta_a1()))
        self.export_all()
        self.assertIn('[Scout the "orchard"]', self.parent_doc().read_text())
        self.assertTrue(self.child_doc("a1").exists())


if __name__ == "__main__":
    unittest.main()


class TestMultilineDescription(SubagentFixture):
    """Phase 1 review #2: a multiline meta description must still yield a valid document."""

    def test_multiline_description_renders_one_line_title(self):
        self.write_child(
            "a9",
            meta=self.meta_a1(description="First line\nSecond line\r\nThird"),
            final="multi report",
        )
        self.export_all()
        child = self.child_doc("a9")
        fm = self.fm(child)  # parse_frontmatter must not raise
        self.assertEqual(fm["title"], "First line Second line Third")
        self.assertEqual(fm["description"], "First line Second line Third")
        index = self.parent_doc().read_text()
        self.assertIn("[First line Second line Third](./", index)
