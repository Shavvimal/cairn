"""Images of a Claude session: saved beside the document, linked from the turn.

This file imports nothing the change added, so each test runs on the base tree too
(``make discriminates``). The image set has its own tests in ``test_images.py``.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from cairn import admin, config
from tests.test_shared_store import StoreFixture

PNG = b"\x89PNG\r\n\x1a\nPASTEDORCHARD"
JPEG = b"\xff\xd8\xffTOOLORCHARD"
GIF = b"GIF89aSECONDPASTE"
SID = "s1"


def block(content: bytes, media_type: str = "image/png") -> dict:
    data = base64.b64encode(content).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def name_of(content: bytes, extension: str) -> str:
    return f"{hashlib.sha256(content).hexdigest()[:16]}.{extension}"


def user(content, ts="2026-03-28T10:00:00Z", **extra) -> dict:
    return {
        "type": "user",
        "sessionId": SID,
        "timestamp": ts,
        "message": {"role": "user", "content": content},
        **extra,
    }


def assistant(text="seen", ts="2026-03-28T10:00:09Z", **extra) -> dict:
    return {
        "type": "assistant",
        "sessionId": SID,
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}],
        },
        **extra,
    }


class ImageExportFixture(StoreFixture):
    def setUp(self):
        super().setUp()
        self.jsonl = self.claude / "-demo" / f"{SID}.jsonl"
        self.jsonl.parent.mkdir(parents=True)

    def set_mode(self, mode: object, collection: str = "claude-code-sessions") -> None:
        data = json.loads(self.config.read_text())
        if mode is None:
            data["collections"][collection].pop("images", None)
        else:
            data["collections"][collection]["images"] = mode
        self.config.write_text(json.dumps(data))
        config.get_config.cache_clear()

    def write(self, *records: dict) -> None:
        self.jsonl.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records))

    def export(self, check=True):
        return self.run_cli("claude", "export", "--all", check=check)

    @property
    def out(self) -> Path:
        return self.root / "data" / "claude-code-sessions" / "-demo"

    def doc(self) -> str:
        (path,) = self.out.glob("*.md")
        return path.read_text()

    def saved(self) -> dict[str, bytes]:
        folder = self.out / "images"
        return {p.name: p.read_bytes() for p in folder.iterdir()} if folder.is_dir() else {}


class TestPastedImages(ImageExportFixture):
    def test_pasted_image_is_saved_and_linked_where_it_was_pasted(self):
        self.write(
            user(
                [{"type": "text", "text": "compare [Image #4] with the plan"}, block(PNG)],
                imagePasteIds=[4],
            ),
            assistant(),
        )
        self.export()
        name = name_of(PNG, "png")
        self.assertEqual(self.saved(), {name: PNG})
        doc = self.doc()
        self.assertIn(f"compare ![Image #4](./images/{name}) with the plan\n", doc)
        # The title is what the user typed, not a link.
        self.assertIn('title: "compare [Image #4] with the plan"', doc)
        self.assertIn("# compare [Image #4] with the plan\n", doc)

    def test_each_image_takes_its_own_placeholder_in_block_order(self):
        self.write(
            user(
                [
                    {"type": "text", "text": "after [Image #7] comes [Image #6]"},
                    block(PNG),
                    block(GIF, "image/gif"),
                ],
                imagePasteIds=[6, 7],
            ),
            assistant(),
        )
        self.export()
        png, gif = name_of(PNG, "png"), name_of(GIF, "gif")
        self.assertEqual(self.saved(), {png: PNG, gif: GIF})
        self.assertIn(
            f"after ![Image #7](./images/{gif}) comes ![Image #6](./images/{png})\n", self.doc()
        )

    def test_image_without_a_placeholder_is_appended(self):
        # No imagePasteIds (a dragged file on an older client), a list that does not
        # match the blocks, and a prompt that is only a picture.
        self.write(
            user([{"type": "text", "text": "no ids here"}, block(PNG)]),
            user(
                [{"type": "text", "text": "ids [Image #1] mismatch"}, block(GIF, "image/gif")],
                ts="2026-03-28T10:00:01Z",
                imagePasteIds=[1, 2],
            ),
            user([block(JPEG, "image/jpeg")], ts="2026-03-28T10:00:02Z", imagePasteIds=[9]),
            assistant(),
        )
        self.export()
        png, gif, jpg = name_of(PNG, "png"), name_of(GIF, "gif"), name_of(JPEG, "jpg")
        self.assertEqual(self.saved(), {png: PNG, gif: GIF, jpg: JPEG})
        doc = self.doc()
        self.assertIn(f"no ids here\n\n![Image](./images/{png})\n", doc)
        self.assertIn(f"ids [Image #1] mismatch\n\n![Image](./images/{gif})\n", doc)
        self.assertIn(f"### User - 2026-03-28T10:00:02Z\n\n![Image #9](./images/{jpg})\n", doc)

    def test_meta_record_and_unsavable_block_change_nothing(self):
        svg = block(b"<svg/>", "image/svg+xml")
        self.write(
            user([{"type": "text", "text": "skill body"}, block(PNG)], isMeta=True),
            user(
                [{"type": "text", "text": "keep [Image #2] as text"}, svg],
                ts="2026-03-28T10:00:01Z",
                imagePasteIds=[2],
            ),
            assistant(),
        )
        self.export()
        self.assertEqual(self.saved(), {})
        doc = self.doc()
        self.assertIn("keep [Image #2] as text\n", doc)
        self.assertNotIn("skill body", doc)
        self.assertNotIn("](./images/", doc)


class TestImageModes(ImageExportFixture):
    def records(self) -> tuple[dict, ...]:
        tool = {
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": [{"type": "text", "text": "screenshot"}, block(JPEG, "image/jpeg")],
        }
        return (
            user([{"type": "text", "text": "see [Image #1]"}, block(PNG)], imagePasteIds=[1]),
            user([tool], ts="2026-03-28T10:00:02Z"),
            assistant(),
        )

    def test_default_saves_pasted_images_only(self):
        self.write(*self.records())
        self.export()
        self.assertEqual(self.saved(), {name_of(PNG, "png"): PNG})
        self.assertIn("screenshot\n[image]\n", self.doc())

    def test_all_also_saves_tool_result_images(self):
        self.set_mode("all")
        self.write(*self.records())
        self.export()
        jpg = name_of(JPEG, "jpg")
        self.assertEqual(self.saved(), {name_of(PNG, "png"): PNG, jpg: JPEG})
        self.assertIn(f"screenshot\n![image](./images/{jpg})\n", self.doc())

    def test_off_exports_as_before(self):
        self.set_mode("off")
        self.write(*self.records())
        self.export()
        self.assertEqual(self.saved(), {})
        self.assertFalse((self.out / "images").exists())
        doc = self.doc()
        self.assertIn("see [Image #1]\n", doc)
        self.assertNotIn("](./images/", doc)

    def test_a_changed_mode_re_renders_an_unchanged_transcript(self):
        self.set_mode("off")
        self.write(*self.records())
        self.export()
        before = self.doc()
        self.export()
        self.assertEqual(self.doc(), before)  # same mode: the document is current
        self.set_mode("pasted")
        self.export()
        name = name_of(PNG, "png")
        self.assertIn(f"see ![Image #1](./images/{name})\n", self.doc())
        self.assertEqual(self.saved(), {name: PNG})

    def test_re_export_keeps_the_saved_file(self):
        self.write(*self.records())
        self.export()
        target = self.out / "images" / name_of(PNG, "png")
        os.utime(target, ns=(1_000_000_000, 1_000_000_000))
        self.write(*self.records(), assistant("more", ts="2026-03-28T10:00:20Z"))
        self.export()
        self.assertIn("more", self.doc())
        self.assertEqual(target.stat().st_mtime_ns, 1_000_000_000)

    def test_subagent_image_lands_beside_the_child_document(self):
        self.write(user("plan the orchard"), assistant())
        child = self.jsonl.parent / SID / "subagents" / "agent-a1.jsonl"
        child.parent.mkdir(parents=True)
        common = {"isSidechain": True, "agentId": "a1"}
        child.write_text(
            "".join(
                json.dumps(r, separators=(",", ":")) + "\n"
                for r in (
                    user(
                        [{"type": "text", "text": "read [Image #1]"}, block(PNG)],
                        imagePasteIds=[1],
                        **common,
                    ),
                    assistant("child done", **common),
                )
            )
        )
        self.export()
        (child_doc,) = self.out.glob("*/agent-a1.md")
        name = name_of(PNG, "png")
        self.assertIn(f"read ![Image #1](./images/{name})\n", child_doc.read_text())
        self.assertEqual((child_doc.parent / "images" / name).read_bytes(), PNG)
        self.assertFalse((self.out / "images").exists())


class TestImageConfig(ImageExportFixture):
    def test_default_and_explicit_modes(self):
        cfg = config.load_config_from(self.config)
        self.assertEqual(cfg.collection("claude-code-sessions").images, "pasted")
        self.assertEqual(cfg.collection("codex-sessions").images, "off")
        self.set_mode("all")
        self.assertEqual(
            config.load_config_from(self.config).collection("claude-code-sessions").images, "all"
        )

    def test_bad_mode_is_refused(self):
        for bad in ("some", True, ""):
            with self.subTest(bad=bad):
                self.set_mode(bad)
                with self.assertRaisesRegex(
                    config.ConfigError,
                    r"collections\.claude-code-sessions\.images must be one of off, pasted, all",
                ):
                    config.load_config_from(self.config)

    def test_key_is_refused_on_a_source_that_saves_no_images(self):
        self.set_mode("pasted", "codex-sessions")
        with self.assertRaisesRegex(
            config.ConfigError, "'codex-sessions' cannot save images"
        ) as caught:
            config.load_config_from(self.config)
        self.assertEqual(caught.exception.code, "CONFIG_UNKNOWN_KEY")

    def run_admin(self, fn, *args) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = fn(*args)
        config.get_config.cache_clear()
        return code, out.getvalue(), err.getvalue()

    def test_config_set_and_show(self):
        code, out, _err = self.run_admin(admin._config_set, "claude.images", "all")
        self.assertEqual(code, 0, out)
        self.assertEqual(
            json.loads(self.config.read_text())["collections"]["claude-code-sessions"]["images"],
            "all",
        )
        code, out, _err = self.run_admin(admin._config_show, True)
        views = {c["name"]: c for c in json.loads(out)["collections"]}
        self.assertEqual(views["claude-code-sessions"]["images"], "all")
        self.assertEqual(views["codex-sessions"]["images"], "off")
        code, out, _err = self.run_admin(admin._config_show, False)
        (claude_line,) = [line for line in out.splitlines() if "claude-code-sessions" in line]
        self.assertIn(" images=all", claude_line)
        self.assertNotIn("images=", "".join(x for x in out.splitlines() if "codex-sessions" in x))

    def test_config_set_refuses_a_bad_value_and_a_wrong_source(self):
        before = self.config.read_text()
        for key, value, expected in (
            ("claude.images", "some", "images expects off|pasted|all, got 'some'"),
            ("codex.images", "all", "codex-sessions cannot save images"),
        ):
            with self.subTest(key=key):
                code, _out, err = self.run_admin(admin._config_set, key, value)
                self.assertEqual(code, 1)
                self.assertIn(expected, err)
                self.assertEqual(self.config.read_text(), before)


if __name__ == "__main__":
    unittest.main()
