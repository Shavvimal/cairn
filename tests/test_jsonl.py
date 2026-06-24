"""The shared JSONL primitives extracted from the Claude adapter (config-free)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from cairn import jsonl


class TestParseJsonl(unittest.TestCase):
    def test_skips_blank_and_malformed_lines(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "s.jsonl"
            p.write_text('{"a": 1}\n\nnot json\n{"b": 2}\n', encoding="utf-8")
            self.assertEqual(jsonl.parse_jsonl(p), [{"a": 1}, {"b": 2}])

    def test_missing_file_is_empty(self):
        self.assertEqual(jsonl.parse_jsonl(Path("/no/such/file.jsonl")), [])


class TestDiscovery(unittest.TestCase):
    def _tree(self, base: Path) -> None:
        proj = base / "-proj-one"
        proj.mkdir(parents=True)
        (proj / "aaaa1111.jsonl").write_text('{"x": 1}\n', encoding="utf-8")
        (proj / "empty.jsonl").write_text("", encoding="utf-8")  # zero-byte -> skipped
        (base / "-proj-two").mkdir()
        (base / "-proj-two" / "bbbb2222.jsonl").write_text('{"y": 2}\n', encoding="utf-8")

    def test_iter_session_files_skips_empty_and_finds_all(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            self._tree(base)
            names = sorted(p.name for p in jsonl.iter_session_files(base))
            self.assertEqual(names, ["aaaa1111.jsonl", "bbbb2222.jsonl"])

    def test_iter_missing_base_is_empty(self):
        self.assertEqual(jsonl.iter_session_files(Path("/no/such/base")), [])

    def test_find_session_jsonl(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            self._tree(base)
            found = jsonl.find_session_jsonl(base, "bbbb2222")
            assert found is not None
            self.assertEqual(found.name, "bbbb2222.jsonl")
            self.assertIsNone(jsonl.find_session_jsonl(base, "nope"))


class TestToolResultText(unittest.TestCase):
    def test_string_passthrough(self):
        self.assertEqual(jsonl.tool_result_text("hi"), "hi")

    def test_list_of_blocks(self):
        content = [{"type": "text", "text": "a"}, {"type": "image"}, "b"]
        self.assertEqual(jsonl.tool_result_text(content), "a\n[image]\nb")

    def test_none(self):
        self.assertEqual(jsonl.tool_result_text(None), "")


if __name__ == "__main__":
    unittest.main()
