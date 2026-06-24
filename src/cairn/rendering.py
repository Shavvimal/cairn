"""Conversation rendering - the decoupled, source-agnostic markdown renderer.

Every source adapter parses its native session store into the same normalized
list of message dicts (see :mod:`cairn.schema`), and this module turns
that list into the ``## Conversation`` body. It knows nothing about Claude Code,
Codex, or Cursor - only about the message shape.

Fidelity contract (ported from the conversation_provenance_service design):
a message whose reasoning is encrypted/unrecoverable sets
``reasoning_recoverable=False`` and is rendered with an explicit
``[reasoning: unrecoverable]`` marker rather than silently dropping the turn.
"""

from __future__ import annotations

import re

from .schema import CodeBlockData, Message, ToolCallData

_ROLE_HEADERS = {"user": "### User", "assistant": "### Assistant", "tool": "### Tool"}

# Unpaired UTF-16 surrogate code points. A correctly-paired emoji is a single
# non-surrogate code point in a Python str, so anything left in this range is a lone
# half of a pair - valid in a str but impossible to encode as UTF-8.
_LONE_SURROGATES = re.compile("[\ud800-\udfff]")


def strip_lone_surrogates(text: str) -> str:
    """Drop unpaired surrogate code points so ``text`` always encodes as UTF-8.

    Some source stores (notably Cursor's SQLite) contain a lone surrogate - half of
    an emoji's surrogate pair - which crashes ``Path.write_text(encoding="utf-8")``
    with a ``UnicodeEncodeError``. Without this, a single malformed session aborts the
    whole export. Stripping the junk half-character is lossless for every valid
    document (it's a no-op when no lone surrogate is present).
    """
    return _LONE_SURROGATES.sub("", text)


def _longest_backtick_run(text: str) -> int:
    longest = run = 0
    for ch in text:
        if ch == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return longest


def _fence(content: str, language: str = "") -> str:
    fence = "`" * max(3, _longest_backtick_run(content) + 1)
    return f"{fence}{language}\n{content}\n{fence}"


def _render_tool_call(tool_call: ToolCallData) -> str:
    status = tool_call.get("status") or ""
    out = [f'<tool name="{tool_call.get("name", "")}" status="{status}">']
    if tool_call.get("arguments") is not None:
        out.append("arguments:")
        out.append(_fence(str(tool_call["arguments"])))
    if tool_call.get("result") is not None:
        out.append("result:")
        out.append(_fence(str(tool_call["result"])))
    out.append("</tool>")
    return "\n".join(out)


def _render_code_block(code_block: CodeBlockData) -> str:
    content = code_block.get("content", "")
    if code_block.get("path"):
        content = f"# path: {code_block['path']}\n{content}"
    return _fence(content, code_block.get("language") or "")


def render_artifacts(files_created: list[str], files_modified: list[str]) -> list[str]:
    """Markdown lines for the '## Artifacts' block (created + modified file lists).

    Created files keep their given order; modified files are sorted. Returns ``[]``
    when there are no artifacts, so callers can ``extend`` unconditionally. The
    source-specific dedup (modified-minus-created) happens upstream in each
    adapter's parse step, not here - this renders an already-resolved pair of lists.
    """
    if not (files_created or files_modified):
        return []
    lines = ["## Artifacts", ""]
    if files_created:
        lines.append("**Created:**")
        lines.extend(f"- `{file_path}`" for file_path in files_created)
        lines.append("")
    if files_modified:
        lines.append("**Modified:**")
        lines.extend(f"- `{file_path}`" for file_path in sorted(files_modified))
        lines.append("")
    return lines


def render_conversation(messages: list[Message]) -> list[str]:
    """Markdown lines for the '## Conversation' body - interleaved turns."""
    lines = ["## Conversation", ""]
    for message in messages:
        header = _ROLE_HEADERS.get(message.get("role", ""), "### Message")
        timestamp = message.get("timestamp")
        if timestamp:
            header = f"{header} - {timestamp}"
        lines.append(header)
        lines.append("")
        thinking = message.get("thinking")
        if thinking:
            lines.extend(
                [
                    "<details><summary>Reasoning</summary>",
                    "",
                    thinking,
                    "",
                    "</details>",
                    "",
                ]
            )
        elif message.get("reasoning_recoverable") is False:
            lines.extend(["> [reasoning: unrecoverable]", ""])
        if message.get("text"):
            lines.extend([message["text"], ""])
        for tool_call in message.get("tool_calls", []):
            lines.extend([_render_tool_call(tool_call), ""])
        for code_block in message.get("code_blocks", []):
            lines.extend([_render_code_block(code_block), ""])
    return lines


__all__ = ["render_artifacts", "render_conversation", "strip_lone_surrogates"]
