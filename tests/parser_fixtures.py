"""Score an adapter's parse of an invented fixture against its expected messages.

Layout: ``tests/fixtures/<source>/<case>.<ext>`` plus ``<case>.expected.json``::

    {"fixture_id": "claude/basic", "source": "claude", "spec_target": "2026-10-30",
     "expected_roles": ["user", "assistant"],
     "expected_messages": [{"role": "user", "text_contains": "Investigate the widget"}],
     "expected_finding": null}

An expected message matches the first unmatched parsed message with the same role
whose text (with its thinking and tool-call names) contains ``text_contains``.
Message recall is matched / expected; a parser change shows up as that number.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCORED_ROLES = ("user", "assistant")
QUOTE_CHARS = 15


def expected_files() -> list[Path]:
    """Every ``<case>.expected.json`` under the per-source fixture folders."""
    return sorted(FIXTURES.glob("*/*.expected.json"))


def load_expected(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def input_of(expected_path: Path) -> Path:
    """The fixture input beside an expected file (``.jsonl`` or ``.json``)."""
    stem = expected_path.name.removesuffix(".expected.json")
    for suffix in (".jsonl", ".json"):
        candidate = expected_path.with_name(stem + suffix)
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"no input beside {expected_path}")


def haystack(message: dict) -> str:
    """The searchable text of one parsed message."""
    parts = [str(message.get("text") or ""), str(message.get("thinking") or "")]
    for call in message.get("tool_calls") or []:
        if isinstance(call, dict):
            parts.append(str(call.get("name") or ""))
    return "\n".join(parts)


@dataclass
class FixtureScore:
    fixture_id: str
    expected: int
    matched: int
    parsed_roles: list[str]
    missing: list[dict] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return self.matched / self.expected if self.expected else 1.0

    def describe(self, target_line: str) -> str:
        """``claude/basic: message recall 3/4 (0.75); missing: user "Investigate the"; ...``"""
        text = (
            f"{self.fixture_id}: message recall {self.matched}/{self.expected} ({self.recall:.2f})"
        )
        if self.missing:
            quoted = ", ".join(
                f'{m["role"]} "{m["text_contains"][:QUOTE_CHARS]}"' for m in self.missing
            )
            text += f"; missing: {quoted}"
        return f"{text}; {target_line}"


def score(fixture_id: str, expected: list[dict], messages: list[dict]) -> FixtureScore:
    scored = [m for m in messages if m.get("role") in SCORED_ROLES]
    used: set[int] = set()
    matched = 0
    missing: list[dict] = []
    for want in expected:
        hit = next(
            (
                i
                for i, m in enumerate(scored)
                if i not in used
                and m.get("role") == want["role"]
                and want["text_contains"] in haystack(m)
            ),
            None,
        )
        if hit is None:
            missing.append(want)
        else:
            used.add(hit)
            matched += 1
    return FixtureScore(
        fixture_id, len(expected), matched, [str(m["role"]) for m in scored], missing
    )


__all__ = [
    "FIXTURES",
    "FixtureScore",
    "expected_files",
    "haystack",
    "input_of",
    "load_expected",
    "score",
]
