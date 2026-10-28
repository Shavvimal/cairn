"""Condensing rules for ``cairn recall digest``: one frame body in, a short body out.

The input is a frame body as the renderer wrote it (:mod:`cairn.rendering`): the role
header, a blank line, an optional reasoning block, the message text, then tool blocks
and (Cursor only) code blocks. The output keeps what says what happened (user intent,
assistant prose, the head and tail of tool output) and drops reasoning and bulk.

Known limits, both deliberate. The text is not escaped by the renderer, so a message
line with the full shape of a tool block (open tag, ``arguments:`` or ``result:``, a
fence, the closing fence, the close tag) is condensed as a tool call. Thinking text
that itself contains a blank line followed by ``</details>`` ends the stripped
reasoning block early, and its remainder shows in the digest. Both affect only the
condensed view: ordinals, offsets and hashes come from :mod:`cairn.frames`, never
from parsing a body.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

USER_CAP = 2000
NOTICE_CAP = 300  # task notifications and system frames
TOOL_ARGS_CAP = 120
OUTPUT_KEEP = 600
OUTPUT_HEAD = 200
OUTPUT_TAIL = 400
AGENT_KEEP = 6000
AGENT_HALF = 3000
PEER_ANSWER_CAP = 6000
PEER_PROMPT_CAP = 300

_REASONING_OPEN = "<details><summary>Reasoning</summary>\n"
_REASONING_CLOSE = "\n\n</details>"
_UNRECOVERABLE = "> [reasoning: unrecoverable]"
_TOOL_OPEN = re.compile(r'<tool name="([^"]*)" status="([^"]*)">')
_FENCE = re.compile(r"(`{3,})[^`\n]*")
_TASK_NOTIFICATION = re.compile(r"<task-notification>.*?</task-notification>", re.DOTALL)
_DIGEST_MARKER_LINE = re.compile(r"<!-- f\d+ ")
# The spec's ``^[\s>*_]*VERDICT:``, kept to one line so a match never takes a blank line.
_VERDICT_LINE = re.compile(r"^(?:[^\S\n]|[>*_])*VERDICT:.*$", re.MULTILINE)

# The spec's regex, plus an optional leading mode command: agy records a ``--mode plan``
# prompt as ``/plan [cairn invoke] ...`` (seen in real Antigravity exports), plus the
# owner form ``being invoked by routine:daily`` that ``cairn invoke --owner`` writes when
# there is no caller (the owner label follows ``mutations.OWNER_RE``).
INVOKE_PREAMBLE = re.compile(
    r"(?:/[a-z][a-z-]* )?"
    r"\[cairn invoke\] You are (?P<peer>[a-z][a-z0-9-]*) being invoked by "
    r"(?:(?P<caller_source>[a-z][a-z0-9-]*) session "
    r"(?P<caller>(?P<ref_source>[a-z][a-z0-9-]*):\S+)|an operator"
    r"|(?P<owner>[A-Za-z0-9][A-Za-z0-9._:-]{0,127})) "
    r"via cairn \(round (?P<round>\d+)\)\."
)
_PROMPT_SEPARATOR = "\n\n---\n\n"


@dataclass(frozen=True)
class Tool:
    """One rendered tool block; ``arguments`` and ``result`` are ``None`` when absent."""

    name: str
    arguments: str | None
    result: str | None


# A body part in render order: message text, a tool block, or a Cursor code block.
Part = tuple[str, str] | Tool


def omitted(count: int) -> str:
    return f"[... {count} chars omitted]"


def head(text: str, keep: int) -> str:
    """The first ``keep`` characters, then an omission line when anything was cut."""
    return text if len(text) <= keep else f"{text[:keep]}\n{omitted(len(text) - keep)}"


def head_tail(text: str, keep: int, first: int, last: int) -> str:
    """Up to ``keep`` characters as they are, else ``first`` + omission + ``last``."""
    if len(text) <= keep:
        return text
    return f"{text[:first]}\n{omitted(len(text) - first - last)}\n{text[-last:]}"


def _strip_reasoning(rest: str) -> str:
    if rest.startswith(_REASONING_OPEN):
        # The block ends at the first blank line + "</details>" that closes a line.
        end = rest.find(_REASONING_CLOSE, len(_REASONING_OPEN) - 1)
        while end != -1:
            after = end + len(_REASONING_CLOSE)
            if after == len(rest) or rest[after] == "\n":
                return rest[after:].lstrip("\n")
            end = rest.find(_REASONING_CLOSE, end + 1)
    if rest == _UNRECOVERABLE or rest.startswith(_UNRECOVERABLE + "\n"):
        return rest[len(_UNRECOVERABLE) :].lstrip("\n")
    return rest


def _fenced(lines: list[str], index: int) -> tuple[str, int, str] | None:
    """A fence opening at ``lines[index]``: (content, index after the close, info string)."""
    if index >= len(lines) or not (match := _FENCE.fullmatch(lines[index])):
        return None
    fence = match[1]
    for close in range(index + 1, len(lines)):
        if lines[close] == fence:
            return "\n".join(lines[index + 1 : close]), close + 1, lines[index][len(fence) :]
    return None


def _tool_at(lines: list[str], index: int) -> tuple[Tool, int] | None:
    match = _TOOL_OPEN.fullmatch(lines[index])
    if match is None:
        return None
    position = index + 1
    values: dict[str, str] = {}
    for key in ("arguments", "result"):
        if position < len(lines) and lines[position] == f"{key}:":
            fenced = _fenced(lines, position + 1)
            if fenced is None or fenced[2]:
                return None
            values[key], position, _ = fenced
    if not values or position >= len(lines) or lines[position] != "</tool>":
        return None
    return Tool(match[1], values.get("arguments"), values.get("result")), position + 1


def parse_parts(body: str, cursor: bool = False) -> list[Part]:
    """Split a frame body (header included) into text, tool and code parts.

    ``\\r\\n`` is normalised to ``\\n`` first; condensed text never carries it. Code
    blocks are recognised only for Cursor, and only as the fenced blocks that close
    the body, because that is where the renderer puts them.
    """
    text = body.replace("\r\n", "\n")
    rest = text.split("\n", 1)[1] if "\n" in text else ""
    lines = _strip_reasoning(rest.removeprefix("\n")).split("\n")
    parts: list[Part] = []
    buffer: list[str] = []
    index = 0
    while index < len(lines):
        found = _tool_at(lines, index)
        if found is None:
            buffer.append(lines[index])
            index += 1
            continue
        parts.append(("text", "\n".join(buffer)))
        buffer = []
        parts.append(found[0])
        index = found[1]
    parts.append(("text", "\n".join(buffer)))
    if cursor:
        parts = _split_code_blocks(parts)
    return [p for p in parts if isinstance(p, Tool) or p[1].strip("\n")]


def _split_code_blocks(parts: list[Part]) -> list[Part]:
    last = parts[-1]
    if isinstance(last, Tool):
        return parts
    lines = last[1].strip("\n").split("\n")
    blocks: list[str] = []
    end = len(lines)
    while end > 0:
        start = next(
            (
                i
                for i in range(end - 1, -1, -1)
                if (f := _fenced(lines, i)) is not None and f[1] == end
            ),
            None,
        )
        if start is None:
            break
        blocks.insert(0, _fenced(lines, start)[0])  # type: ignore[index]
        end = start
        while end > 0 and not lines[end - 1]:
            end -= 1
    if not blocks:
        return parts
    return [*parts[:-1], ("text", "\n".join(lines[:end])), *(("code", b) for b in blocks)]


def one_line(text: str) -> str:
    return " ".join(text.split())


def text_of(parts: list[Part]) -> str:
    """The message text of a body, without tool or code blocks."""
    return "\n\n".join(
        p[1].strip("\n") for p in parts if not isinstance(p, Tool) and p[0] == "text"
    )


def condense_user(text: str) -> str:
    """Task notifications to their first lines, then the user cap (5.2)."""

    def notice(match: re.Match[str]) -> str:
        short = head(match[0], NOTICE_CAP)
        # Keep the omission marker a line of its own when text follows on the same line.
        follows = match.end() < len(text) and text[match.end()] != "\n"
        return short + "\n" if short != match[0] and follows else short

    return head(_TASK_NOTIFICATION.sub(notice, text), USER_CAP)


class Condenser:
    """Condenses frames of one session in ordinal order (output dedup is stateful)."""

    def __init__(self, cursor: bool = False):
        self.cursor = cursor
        self._last_output: tuple[str, int] | None = None
        # Per ordinal: (referenced ordinal, placeholder line, the full condensed output),
        # so a digest can restore an output whose referent the cap dropped.
        self.refs: dict[int, list[tuple[int, str, str]]] = {}

    def _output(self, text: str, ordinal: int) -> str:
        digest = hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()
        full = head_tail(text, OUTPUT_KEEP, OUTPUT_HEAD, OUTPUT_TAIL)
        if self._last_output and self._last_output[0] == digest:
            target = self._last_output[1]
            placeholder = f"[same output as f{target}]"
            self.refs.setdefault(ordinal, []).append((target, placeholder, escape(full)))
            return placeholder
        self._last_output = (digest, ordinal)
        return full

    def _tool(self, tool: Tool, ordinal: int) -> list[str]:
        call = f"[tool {tool.name}]"
        if tool.arguments is not None:
            args = one_line(tool.arguments)
            cut = len(args) > TOOL_ARGS_CAP
            call += f" {args[:TOOL_ARGS_CAP]}" + (" …" if cut else "")
        if tool.result is None:
            return [call]
        return [call, "[result]", self._output(tool.result, ordinal)]

    def frame(self, role: str, body: str, ordinal: int) -> str:
        """The condensed body of one frame for a primary or subagent digest."""
        pieces: list[str] = []
        for part in parse_parts(body, self.cursor):
            if isinstance(part, Tool):
                pieces.extend(self._tool(part, ordinal))
                continue
            kind, text = part
            text = text.strip("\n")
            if kind == "code" or role == "tool":
                pieces.append(self._output(text, ordinal))
            elif role == "assistant":
                pieces.append(text)
            elif role == "agent":
                pieces.append(head_tail(text, AGENT_KEEP, AGENT_HALF, AGENT_HALF))
            elif role == "system":
                pieces.append(head(text, NOTICE_CAP))
            else:  # user, and "message" for a role the renderer did not know
                pieces.append(condense_user(text))
        return escape("\n".join(pieces))


def escape(text: str) -> str:
    """Prefix a quoted digest marker so it cannot pose as a frame marker."""
    return "\n".join(
        "\\" + line if _DIGEST_MARKER_LINE.match(line) else line for line in text.split("\n")
    )


def preamble_match(body: str) -> re.Match[str] | None:
    """The invoke preamble at the start of a user frame's text, if present."""
    text = body.replace("\r\n", "\n")
    rest = text.split("\n", 1)[1].removeprefix("\n") if "\n" in text else ""
    return INVOKE_PREAMBLE.match(rest)


def peer_prompt(body: str) -> str:
    """The first characters of a peer round's prompt, after the preamble."""
    text = body.replace("\r\n", "\n").split("\n", 1)[1].removeprefix("\n")
    prompt = text.split(_PROMPT_SEPARATOR, 1)[1] if _PROMPT_SEPARATOR in text else ""
    return escape(head(prompt.strip("\n"), PEER_PROMPT_CAP))


def peer_answer(body: str) -> str:
    """An assistant frame's prose, without reasoning or tool blocks, capped."""
    return escape(head(text_of(parse_parts(body)), PEER_ANSWER_CAP))


def verdict_lines(body: str) -> str:
    return escape("\n".join(m[0] for m in _VERDICT_LINE.finditer(text_of(parse_parts(body)))))


def select(blocks: list[tuple[int, bool, str]], cap: int) -> set[int]:
    """Ordinals to keep under ``cap`` (5.3): users newest first, then the tail, then the head.

    ``blocks`` holds ``(ordinal, is_user, block)`` in ordinal order. Blocks are joined
    with one newline, so each costs its length plus one against ``cap + 1``.
    """
    budget = cap + 1
    if sum(len(b) + 1 for _, _, b in blocks) <= budget:
        return {ordinal for ordinal, _, _ in blocks}
    kept: set[int] = set()
    total = 0
    for ordinal, is_user, block in reversed(blocks):
        if is_user and total + len(block) + 1 <= budget:
            kept.add(ordinal)
            total += len(block) + 1
    others = [(o, b) for o, is_user, b in blocks if not is_user]
    for sequence in (reversed(others), others):
        for ordinal, block in sequence:
            if ordinal in kept:
                continue
            if total + len(block) + 1 > budget:
                break
            kept.add(ordinal)
            total += len(block) + 1
    return kept


__all__ = [
    "INVOKE_PREAMBLE",
    "Condenser",
    "Tool",
    "escape",
    "parse_parts",
    "peer_answer",
    "peer_prompt",
    "preamble_match",
    "select",
    "verdict_lines",
]
