"""The image set: content-hash names, the three modes, blocks that cannot be saved."""

from __future__ import annotations

import base64
import hashlib
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from cairn.images import ImageSet, images_dir_of
from cairn.jsonl import tool_result_text

PNG = b"\x89PNG\r\n\x1a\nPASTEDORCHARD"
JPEG = b"\xff\xd8\xffTOOLORCHARD"


def block(content: bytes, media_type: str = "image/png") -> dict:
    data = base64.b64encode(content).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def name_of(content: bytes, extension: str) -> str:
    return f"{hashlib.sha256(content).hexdigest()[:16]}.{extension}"


class TestImageSet(unittest.TestCase):
    def test_pasted_names_the_file_by_content_hash(self):
        images = ImageSet("pasted")
        self.assertEqual(
            images.pasted(block(PNG), 4), f"![Image #4](./images/{name_of(PNG, 'png')})"
        )
        self.assertEqual(
            images.pasted(block(JPEG, "image/jpeg")), f"![Image](./images/{name_of(JPEG, 'jpg')})"
        )
        self.assertEqual(len(images), 2)

    def test_modes_gate_what_is_saved(self):
        for mode, pasted, tool in (("off", 0, 0), ("pasted", 1, 0), ("all", 1, 1)):
            with self.subTest(mode=mode):
                images = ImageSet(mode)
                self.assertEqual(images.pasted(block(PNG), 1) is not None, bool(pasted))
                self.assertEqual(images.tool(block(JPEG, "image/jpeg")) is not None, bool(tool))
                self.assertEqual(len(images), pasted + tool)
        with self.assertRaisesRegex(ValueError, "one of off, pasted, all, got 'some'"):
            ImageSet("some")

    def test_a_block_that_cannot_be_saved_keeps_its_placeholder(self):
        images = ImageSet("all")
        url = {"type": "image", "source": {"type": "url", "url": "https://x.invalid/a.png"}}
        torn = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "@@not base64@@"},
        }
        empty = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": ""},
        }
        for bad in (url, torn, empty, block(PNG, "image/svg+xml"), {"type": "image"}):
            with self.subTest(bad=bad):
                self.assertIsNone(images.pasted(bad, 1))
                self.assertIsNone(images.tool(bad))
        self.assertEqual(len(images), 0)

    def test_write_is_idempotent_and_leaves_an_existing_file_alone(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp) / "project" / "images"
            images = ImageSet("pasted")
            images.pasted(block(PNG), 1)
            images.pasted(block(PNG), 2)  # the same picture pasted twice is one file
            self.assertEqual(images.write(folder), 1)
            target = folder / name_of(PNG, "png")
            self.assertEqual(target.read_bytes(), PNG)
            os.utime(target, ns=(1_000_000_000, 1_000_000_000))
            self.assertEqual(images.write(folder), 0)
            self.assertEqual(target.stat().st_mtime_ns, 1_000_000_000)
            self.assertEqual([p.name for p in folder.iterdir()], [target.name])

    def test_an_empty_set_creates_no_folder(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp) / "images"
            self.assertEqual(ImageSet("pasted").write(folder), 0)
            self.assertFalse(folder.exists())

    def test_images_dir_is_beside_the_document(self):
        self.assertEqual(images_dir_of(Path("/x/demo/2026-03-28-s1.md")), Path("/x/demo/images"))

    def test_tool_result_text_uses_the_callback_and_falls_back(self):
        content = [{"type": "text", "text": "shot:"}, block(JPEG, "image/jpeg")]
        self.assertEqual(tool_result_text(content), "shot:\n[image]")
        self.assertEqual(tool_result_text(content, lambda _item: None), "shot:\n[image]")
        self.assertEqual(tool_result_text(content, ImageSet("pasted").tool), "shot:\n[image]")
        self.assertEqual(
            tool_result_text(content, ImageSet("all").tool),
            f"shot:\n![image](./images/{name_of(JPEG, 'jpg')})",
        )


if __name__ == "__main__":
    unittest.main()
