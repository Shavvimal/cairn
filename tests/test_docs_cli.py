"""Every documented ``cairn`` invocation parses against the real argparse parsers.

The scanner reads README.md, CONTRIBUTING.md, skills, references, docs and the
``cairn help`` overview, finds each ``cairn`` at command position in a fenced block
or an inline code span that starts with ``cairn ``, and parses the argv with the
parser the dispatcher would run. A mismatch fails with ``path:line`` and the
parser's own error. Two markers exempt text on purpose:

- ``<!-- cairn-cli: historical -->`` on the line above a fence exempts the block;
  on, or directly above, a line with inline code it exempts that line's spans.
- ``<!-- cairn-cli: skip -->`` on a line exempts that line's inline spans; on the
  line above a fence it exempts a block that holds no commands (a quoted file body).

The module reads the repo and writes nothing.
"""

from __future__ import annotations

import argparse
import builtins
import contextlib
import importlib
import io
import os
import re
import subprocess
import unittest
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from unittest import mock

from cairn import __main__ as dispatch

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL = "<!-- cairn-cli: historical -->"
SKIP = "<!-- cairn-cli: skip -->"
FENCE_OPEN = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
FENCE_CLOSE = re.compile(r"^\s*(`{3,}|~{3,})\s*$")
BLOCKQUOTE = re.compile(r"^\s*(?:>\s?)+")
BLOCK_START = re.compile(r"^\s*(?:#{1,6}\s|\||[-*+]\s|\d+[.)]\s)")
CONTINUATION = re.compile(r"\\\s*$")
INLINE_SPAN = re.compile(r"(`+)(.+?)(?<!`)\1(?!`)", re.S)
WRAPPERS = (
    r"(?:(?:[A-Z_][A-Z0-9_]*=\S*|uv\s+run|python3?\s+-m|nohup|exec|command|time"
    r"|timeout\s+\S+)\s+)*"
)
COMMAND_START = re.compile(
    r"(?:^|&&|\|\||;|\|\s|\$\(|`|\()\s*(?:\$\s+)?" + WRAPPERS + r"(?<![\w./-])cairn(?=\s|$)"
)
NAME_PLACEHOLDER = re.compile(r"^(?:[A-Z][A-Z0-9_-]*(?::[A-Z0-9_-]+)?|N[a-z])$")
COMMAND_WORD = re.compile(r"^[a-z][a-z-]*$")
OPTION_VALUE = re.compile(r"^(--?[A-Za-z][A-Za-z0-9_-]*=)(.+)$")
BASE_CANDIDATES = ("1", "2026-01-01", "7d")
SENTINEL = "__cairn_ph_{}__"
PIPE_MESSAGE = "usage alternation '{} | {}' runs as a shell pipe; write one command per line"
FAILURE_FOOTER = (
    "Fix the doc to the current CLI (check `cairn <command> --help`). If the example "
    "documents\nan older release, put <!-- cairn-cli: historical --> on the line above its "
    "fence. For an\ninline span that is not a command, put <!-- cairn-cli: skip --> on its "
    "line.\nRerun: uv run python -m unittest tests.test_docs_cli"
)
SHELL_FENCES = frozenset({"bash", "sh", "shell", "console"})
UNIX_FILTERS = frozenset({"tail", "head", "jq", "grep", "less", "sort", "wc", "tee", "cat"})


@dataclass(frozen=True)
class Region:
    """One scannable piece of a document: a fenced line or one inline code span."""

    path: str
    line: int
    kind: str  # "fence" | "inline"
    text: str


@dataclass(frozen=True)
class Invocation:
    path: str
    line: int
    kind: str
    text: str  # source text after ``cairn``, whitespace collapsed
    words: tuple[tuple[str, bool], ...]  # (word, is_placeholder)
    pipe_into: str | None  # first word after a spaced pipe, for the alternation rule
    variants: tuple[tuple[tuple[str, bool], ...], ...] = ()  # argv per later alternative
    compact: tuple[tuple[str, tuple[str, ...]], ...] = ()  # (word before, alternatives)


# ---------------------------------------------------------------------------
# Regions
# ---------------------------------------------------------------------------


def fence_open(line: str) -> tuple[str, int, str] | None:
    """(delimiter char, delimiter length, info string) when ``line`` opens a fence."""
    match = FENCE_OPEN.match(line)
    if match is None or (match.group(1)[0] == "`" and "`" in match.group(2)):
        return None
    return match.group(1)[0], len(match.group(1)), match.group(2).strip()


def fence_closes(line: str, opener: tuple[str, int, str]) -> bool:
    """True when ``line`` closes the fence ``opener`` opened: same char, at least as long."""
    match = FENCE_CLOSE.match(line)
    return bool(match and match.group(1)[0] == opener[0] and len(match.group(1)) >= opener[1])


def _unquote(line: str) -> tuple[str, bool]:
    """The line without its blockquote prefix, and whether it had one."""
    match = BLOCKQUOTE.match(line)
    return (line[match.end() :], True) if match else (line, False)


def _inline_regions(path: str, lines: list[str], block: list[tuple[int, str]]) -> Iterator[Region]:
    """Inline ``cairn`` spans in one paragraph; a span can cross a soft line break."""
    if not block:
        return
    text = "\n".join(code for _, code in block)
    starts = []
    offset = 0
    for index, code in block:
        starts.append((offset, index))
        offset += len(code) + 1
    for match in INLINE_SPAN.finditer(text):
        index = next(i for start, i in reversed(starts) if start <= match.start())
        line = lines[index]
        above = lines[index - 1] if index > 0 else ""
        if SKIP in line or HISTORICAL in line or HISTORICAL in above or above.strip() == SKIP:
            continue
        span = re.sub(r"\s*\n\s*", " ", match.group(2).strip()).replace("\\|", "|")
        if span.startswith("cairn "):
            yield Region(path, index + 1, "inline", span)


def code_regions(path: str, text: str) -> Iterator[Region]:
    """Yield fenced lines (continuations joined) and inline ``cairn`` spans, minus markers."""
    lines = text.split("\n")
    opener: tuple[str, int, str] | None = None
    quoted = False  # the open fence sits in a blockquote
    exempt = False
    block: list[tuple[int, str]] = []  # the paragraph being read for inline spans
    i = 0
    while i < len(lines):
        line, in_quote = _unquote(lines[i])
        if opener is None:
            fence = fence_open(line)
            if fence is None and line.strip() and not BLOCK_START.match(line):
                block.append((i, line))
                i += 1
                continue
            yield from _inline_regions(path, lines, block)
            block = []
            if fence is not None:
                opener, quoted = fence, in_quote
                above = lines[i - 1] if i > 0 else ""
                exempt = HISTORICAL in above or SKIP in above
            elif line.strip():
                block.append((i, line))  # a heading, list item or table row starts a block
            i += 1
            continue
        code = line if quoted else lines[i]
        if fence_closes(code, opener):
            opener = None
            i += 1
            continue
        start = i
        while CONTINUATION.search(code) and i + 1 < len(lines):
            following = _unquote(lines[i + 1])[0] if quoted else lines[i + 1]
            if fence_closes(following, opener):
                break
            i += 1
            code = CONTINUATION.sub("", code).rstrip() + " " + following.strip()
        if not exempt:
            yield Region(path, start + 1, "fence", code)
        i += 1
    yield from _inline_regions(path, lines, block)


# ---------------------------------------------------------------------------
# Tokeniser
# ---------------------------------------------------------------------------


def _skip_group(text: str, i: int) -> int:
    """From an alternative separator inside ``[...]``, return the index after its ``]``."""
    depth = 0
    while i < len(text):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            if depth == 0:
                return i + 1
            depth -= 1
        i += 1
    return i


def _closing(text: str, i: int, opener: str, closer: str) -> int:
    """Index after the ``closer`` that balances the ``opener`` at ``i``."""
    depth = 0
    while i < len(text):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


BRACE_PLACEHOLDER = re.compile(
    r"^\{[a-z][a-z0-9_]*\}$"
)  # {harness}, {name}: a registry verify argv


def _quote_end(text: str, i: int) -> int:
    """Index of the quote that closes the one at ``i`` (``\\"`` does not close ``"``)."""
    quote, j = text[i], i + 1
    while j < len(text) and text[j] != quote:
        j += 2 if quote == '"' and text[j] == "\\" else 1
    return min(j, len(text))


def _is_placeholder(word: str) -> bool:
    if BRACE_PLACEHOLDER.match(word):
        return True
    return bool(NAME_PLACEHOLDER.match(word) or word.endswith(("...", "…")) or word in ("...", "…"))


def tokenise(text: str) -> tuple[tuple[tuple[str, bool], ...], str | None, int]:
    """Split the text after ``cairn`` into (word, is_placeholder) pairs.

    Returns the words, the first word after a spaced pipe (or None) and the index
    where the invocation ends.
    """
    words: list[tuple[str, bool]] = []
    chars: list[str] = []
    state = {"has": False, "quoted": False, "marked": False}
    depth = 0
    pipe_into: str | None = None

    def flush() -> None:
        if state["has"]:
            word = "".join(chars)
            placeholder = not state["quoted"] and (state["marked"] or _is_placeholder(word))
            words.append((word, placeholder))
        chars.clear()
        state.update(has=False, quoted=False, marked=False)

    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in "'\"":
            end = _quote_end(text, i)
            chars.append(text[i + 1 : end].replace('\\"', '"') if c == '"' else text[i + 1 : end])
            state.update(has=True, quoted=True)
            i = end + 1
            continue
        if c == "<":
            end = text.find(">", i + 1)
            if text.startswith("<<", i) or end < 0 or text[i + 1 : i + 2].isspace():
                break  # heredoc or input redirect
            chars.append(text[i : end + 1])
            state.update(has=True, marked=True)
            i = end + 1
            continue
        if text.startswith("$(", i):
            end = _closing(text, i + 1, "(", ")")
            chars.append(text[i:end])
            state.update(has=True, marked=True)
            i = end
            continue
        if c == "$" and re.match(r"\$\{?[A-Za-z_]", text[i:]):
            match = re.match(r"\$(?:\{[^}]*\}|[A-Za-z_][A-Za-z0-9_]*)", text[i:])
            assert match is not None
            chars.append(match.group(0))
            state.update(has=True, marked=True)
            i += match.end()
            continue
        if text.startswith("{{", i):
            end = text.find("}}", i)
            end = n if end < 0 else end + 2
            chars.append(text[i:end])
            state.update(has=True, marked=True)
            i = end
            continue
        if c == "[":
            flush()
            depth += 1
            i += 1
            continue
        if c == "]":
            flush()
            depth = max(0, depth - 1)
            i += 1
            continue
        if c.isspace():
            flush()
            j = i
            while j < n and text[j].isspace():
                j += 1
            if j - i >= 2 and depth == 0 and j < n and text[j].islower():
                break  # the description column starts here
            i = j
            continue
        if c == "|":
            if depth > 0:
                flush()
                i = _skip_group(text, i + 1)
                depth -= 1
                continue
            if text.startswith("||", i):
                break
            spaced = (i > 0 and text[i - 1].isspace()) or text[i + 1 : i + 2].isspace()
            if state["has"] and not spaced:
                while i < n and not text[i].isspace() and text[i] != "]":
                    i += 1
                continue
            match = re.match(r"\s*([a-z][a-z-]*)(\s+\S)?", text[i + 1 :])
            pipe_into = match.group(1) if match else None
            following = match.group(2) if match else None
            if pipe_into in UNIX_FILTERS and (following is None or following.strip() == "-"):
                pipe_into = None  # `| tail -n 5`, `| head`: a real pipe into a filter
            break
        if c in ";&`)":
            break
        if c == ">":
            if "".join(chars).isdigit() and not state["quoted"]:
                chars.clear()
                state.update(has=False)
            break
        if c == "#" and not state["has"]:
            break
        chars.append(c)
        state["has"] = True
        i += 1
    else:
        i = n
    flush()
    return tuple(words), pipe_into, i


def mask_literals(text: str) -> str:
    """``text`` with quoted literals and comments filled, so no command starts inside them.

    A command substitution inside double quotes still runs, so it stays visible.
    """
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "'":
            end = _quote_end(text, i)
            out[i + 1 : end] = "x" * (end - i - 1)
            i = end + 1
        elif c == '"':
            i += 1
            while i < n and text[i] != '"':
                if text.startswith('\\"', i):
                    out[i : i + 2] = "xx"
                    i += 2
                elif text.startswith("$(", i):
                    i = _closing(text, i + 1, "(", ")")
                elif text[i] == "`":
                    end = text.find("`", i + 1)
                    i = n if end < 0 else end + 1
                else:
                    out[i] = "x"
                    i += 1
            i += 1
        elif c == "#" and (i == 0 or text[i - 1].isspace()):
            out[i:] = "x" * (n - i)
            break
        else:
            i += 1
    return "".join(out)


def _split_top(text: str) -> list[str]:
    """Split ``text`` at a spaced ``|`` outside nested brackets and quotes."""
    parts, depth, quote, start = [], 0, "", 0
    for i, c in enumerate(text):
        if quote:
            quote = "" if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
        elif (
            c == "|" and depth == 0 and (text[i - 1 : i].isspace() or text[i + 1 : i + 2].isspace())
        ):  # a spaced `|` separates alternatives; `a|b` is one word's alternation
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


def _resolve(text: str, k: int, compact: list[tuple[str, tuple[str, ...]]]) -> tuple[str, int]:
    """``text`` with alternative ``k`` (or the last) chosen in every group and bare alternation.

    Returns the text and the largest number of alternatives seen.
    """
    out: list[str] = []
    widest = 1
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in "'\"":
            end = _quote_end(text, i)
            out.append(text[i : end + 1])
            i = end + 1
        elif c == "[":
            end = _closing(text, i, "[", "]")
            alts = _split_top(text[i + 1 : end - 1])
            widest = max(widest, len(alts))
            inner, deeper = _resolve(alts[min(k, len(alts) - 1)].strip(), k, compact)
            widest = max(widest, deeper)
            out.append("[" + inner + "]")
            i = end
        elif not c.isspace() and c != "|":
            end = i
            while end < n and not text[end].isspace() and text[end] not in "[]'\"":
                end += 1
            word = text[i:end]
            alts = word.split("|")
            if len(alts) > 1 and "||" not in word and all(alts):
                widest = max(widest, len(alts))
                before = "".join(out).split()
                compact.append((before[-1] if before else "", tuple(alts)))
                word = alts[min(k, len(alts) - 1)]
            out.append(word)
            i = end
        else:
            out.append(c)
            i += 1
    return "".join(out), widest


def alternatives(text: str) -> tuple[list[str], tuple[tuple[str, tuple[str, ...]], ...]]:
    """One text per alternative index, so every ``[a | b]`` and ``a|b`` branch is parsed.

    Also returns each bare ``a|b`` with the word before it, for the compact-pipe rule.
    """
    compact: list[tuple[str, tuple[str, ...]]] = []
    first, widest = _resolve(text, 0, compact)
    texts = [first] + [_resolve(text, k, [])[0] for k in range(1, widest)]
    return texts, tuple(compact)


def invocations(region: Region) -> Iterator[Invocation]:
    """Each ``cairn`` at command position in a region, tokenised up to its end."""
    consumed = 0
    for match in COMMAND_START.finditer(mask_literals(region.text)):
        if match.start() < consumed:
            continue  # inside the previous invocation's argv
        tail = region.text[match.end() :]
        words, pipe_into, stop = tokenise(tail)
        consumed = match.end() + stop
        texts, compact = alternatives(tail[:stop])
        variants = tuple(tokenise(text)[0] for text in texts[1:])
        shown = tail[:stop]
        if pipe_into is not None:
            rest = re.split(r"\s{2,}(?=[a-z])", tail[stop:], maxsplit=1)[0]
            shown = tail[:stop] + rest
        yield Invocation(
            region.path,
            region.line,
            region.kind,
            " ".join(shown.split()),
            words,
            pipe_into,
            variants,
            compact,
        )


# ---------------------------------------------------------------------------
# Routing and parsing
# ---------------------------------------------------------------------------


def _commands() -> dict[str, str]:
    return {**dispatch._SOURCES, **dispatch._ADMIN}


def builder_spec(spec: str) -> str:
    """``module:main`` -> ``module:build_parser``; ``module:x_main`` -> ``module:build_x_parser``."""
    module, function = spec.split(":", 1)
    if function == "main":
        return f"{module}:build_parser"
    if function.endswith("_main"):
        return f"{module}:build_{function[: -len('_main')]}_parser"
    raise ValueError(f"{spec}: no builder name for this function")


@cache
def build_route_parser(command: str) -> argparse.ArgumentParser:
    """The parser ``cairn <command>`` runs, built through the module's seam."""
    module, function = builder_spec(_commands()[command]).split(":", 1)
    parser = getattr(importlib.import_module(module), function)()
    assert isinstance(parser, argparse.ArgumentParser)
    return parser


def _walk(parser: argparse.ArgumentParser) -> Iterator[argparse.Action]:
    for action in parser._actions:
        yield action
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                yield from _walk(child)


@cache
def candidates(command: str) -> tuple[str, ...]:
    values: list[str] = list(BASE_CANDIDATES)
    for action in _walk(build_route_parser(command)):
        values.extend(str(choice) for choice in action.choices or ())
    return tuple(dict.fromkeys(values))


@cache
def subcommand_names(command: str) -> frozenset[str]:
    names: set[str] = set()
    for action in _walk(build_route_parser(command)):
        if isinstance(action, argparse._SubParsersAction):
            names.update(action.choices)
    return frozenset(names)


@cache
def option_names(command: str) -> frozenset[str]:
    """Every option string in the route's parser tree."""
    names: set[str] = set()
    for action in _walk(build_route_parser(command)):
        names.update(action.option_strings)
    return frozenset(names)


def parse_error(parser: argparse.ArgumentParser, argv: list[str]) -> str | None:
    """None when ``argv`` parses, else the parser's last stderr line."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        try:
            parser.parse_args(argv)
        except SystemExit as exc:
            if exc.code == 0:
                return None  # --help, or a route's own --version action
            lines = [line for line in err.getvalue().splitlines() if line.strip()]
            return lines[-1] if lines else f"exit {exc.code}"
    return None


def _relaxed(error: str, argv: list[str], inline: bool, command: str) -> bool:
    if not inline:
        return False
    known = option_names(command)
    if any(w.startswith("-") and w.split("=", 1)[0] not in known for w in argv if w != "-"):
        return False  # argparse reports a missing argument before an unknown option
    if "the following arguments are required" in error:
        return True
    if re.search(r"one of the arguments .* is required", error):
        return True
    return "expected one argument" in error and bool(argv) and argv[-1].startswith("-")


def _names(error: str, value: str) -> bool:
    if f"'{value}'" in error:
        return True
    match = re.search(r"unrecognized arguments: (.*)$", error)
    return bool(match and value in match.group(1).split())


def _check_route(command: str, rest: list[str], placeholders: dict[str, str], inline: bool):
    """Parse ``rest`` with placeholder substitution. Returns None or the error text."""
    parser = build_route_parser(command)
    pool = candidates(command)
    argv = list(rest)
    tried: set[str] = set()

    def substitute(sentinel: str, current: str, named: bool) -> list[str] | None:
        trials = [[word.replace(sentinel, value) for word in argv] for value in pool]
        errors = [parse_error(parser, trial) for trial in trials]
        for trial, error in zip(trials, errors, strict=True):
            if error is None or _relaxed(error, trial, inline, command):
                return trial  # a value that passes outright wins over an earlier partial one
        for value, trial, error in zip(pool, trials, errors, strict=True):
            assert error is not None
            if _names(error, value):
                continue
            if named or error != current:
                return trial
        return None

    for _ in range(4 * len(argv) + 4):
        error = parse_error(parser, argv)
        if error is None or _relaxed(error, argv, inline, command):
            return None
        live = [s for s in placeholders if any(s in word for word in argv)]
        named = [s for s in live if s in error]
        if named:
            sentinel = named[0]
            original = placeholders[sentinel]
            if (
                "unrecognized arguments" in error
                and original.endswith(("...", "…"))
                and sentinel in argv
            ):
                argv.remove(sentinel)
                continue
            if sentinel in tried:
                return error
            tried.add(sentinel)
            trial = substitute(sentinel, error, named=True)
            if trial is None:
                return error
            argv = trial
            continue
        for sentinel in [word for word in live if word not in tried]:
            tried.add(sentinel)
            trial = substitute(sentinel, error, named=False)
            if trial is not None:
                argv = trial
                break
        else:
            return error
    return error


def _routes(words: list[tuple[str, bool]]) -> list[str]:
    """The routes to try for a dispatcher-slot placeholder, narrowed by what follows it."""
    first = words[0][0]
    routes = list(dispatch._SOURCES)
    if "source" not in first.lower():
        routes += list(dispatch._ADMIN)
    if len(words) > 1 and not words[1][1] and not words[1][0].startswith("-"):
        routes = [c for c in routes if subcommand_names(c)]  # a literal verb needs a verb slot
    return routes


def _check_argv(words: list[tuple[str, bool]], routes: list[str], inline: bool) -> str | None:
    placeholders: dict[str, str] = {}
    rest: list[str] = []
    for word, placeholder in words[1:]:
        if placeholder:
            sentinel = SENTINEL.format(len(placeholders))
            option = OPTION_VALUE.match(word)
            if option:  # --opt=<value>: the option name stays literal (rule 9)
                placeholders[sentinel] = option.group(2)
                rest.append(option.group(1) + sentinel)
            else:
                placeholders[sentinel] = word
                rest.append(sentinel)
        else:
            rest.append(word)
    first_error: str | None = None
    for command in routes:
        error = _check_route(command, rest, placeholders, inline)
        if error is None:
            return None
        first_error = first_error or error
    if first_error is None:
        return "cairn: no route takes this argv"
    for sentinel, original in placeholders.items():
        first_error = first_error.replace(sentinel, original)
    return first_error


def check(invocation: Invocation) -> str | None:
    """None when the invocation parses, else the violation text after ``cairn <text>:``."""
    words = list(invocation.words)
    if not words:
        return None
    first, first_is_placeholder = words[0]
    if not first_is_placeholder and not COMMAND_WORD.match(first):
        return None  # rule 15: prose such as "cairn - local session…"
    if all(placeholder for _, placeholder in words):
        return None  # rule 16: a usage template
    if first == "help" and not first_is_placeholder:
        return None
    commands = list(dispatch._SOURCES) + list(dispatch._ADMIN)
    if first_is_placeholder:
        routes = _routes(words)
    elif first in commands:
        routes = [first]
    else:
        return f"cairn: unknown command {first!r}"
    for command in routes:
        names = subcommand_names(command)
        if invocation.pipe_into is not None and invocation.pipe_into in names:
            left = next((w for w, _ in reversed(words)), first)
            return PIPE_MESSAGE.format(left, invocation.pipe_into)
        for before, alts in invocation.compact:
            if invocation.kind == "inline" or before.startswith("-"):
                continue  # inline `note|close` is prose shorthand; `--x a|b` is a value
            if sum(alt in names for alt in alts) >= 2:
                return PIPE_MESSAGE.format(alts[0], alts[1])  # `install|uninstall` pipes too
    inline = invocation.kind == "inline"
    for argv in (words, *[list(v) for v in invocation.variants]):
        if argv and argv[0] == words[0]:
            error = _check_argv(argv, routes, inline)
            if error is not None:
                return error
    return None


def scan_text(path: str, text: str) -> tuple[int, list[str]]:
    """(invocations seen, violations) for one document."""
    seen = 0
    violations: list[str] = []
    for region in code_regions(path, text):
        for invocation in invocations(region):
            seen += 1
            error = check(invocation)
            if error is not None:
                violations.append(
                    f"{invocation.path}:{invocation.line}: cairn {invocation.text}: {error}"
                )
    return seen, violations


def doc_files() -> list[Path]:
    files = [ROOT / "README.md", ROOT / "CONTRIBUTING.md"]
    for folder in ("skills", "references", "docs", ".agents/skills"):
        files.extend(sorted((ROOT / folder).rglob("*.md")))
    return [path for path in files if path.is_file()]


def help_document() -> tuple[str, str]:
    """The ``cairn help`` text as one fence, with line numbers in ``__main__.py``."""
    source = (ROOT / "src" / "cairn" / "__main__.py").read_text()
    lines = source.split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith('_HELP = """'))
    body = dispatch._HELP.split("\n")
    return "src/cairn/__main__.py", "\n" * start + "```\n" + "\n".join(body) + "\n```\n"


def scan(paths: list[Path] | None = None) -> tuple[int, list[str], dict[str, int]]:
    """Scan the doc set. Returns (count, violations, count per file)."""
    total = 0
    violations: list[str] = []
    per_file: dict[str, int] = {}
    for path in paths if paths is not None else doc_files():
        rel = path.relative_to(ROOT).as_posix()
        seen, found = scan_text(rel, path.read_text())
        total += seen
        per_file[rel] = seen
        violations.extend(found)
    return total, violations, per_file


def _failure(violations: list[str]) -> str:
    return (
        "Docs name cairn invocations the CLI rejects:\n"
        + "\n".join(violations)
        + "\n\n"
        + FAILURE_FOOTER
    )


def _one(text: str, kind: str = "fence") -> Invocation:
    found = [inv for inv in invocations(Region("x.md", 1, kind, text))]
    assert len(found) == 1, found
    return found[0]


def _argv(text: str) -> list[str]:
    return [word for word, _ in _one(text).words]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTokeniser(unittest.TestCase):
    def test_prompt_prefix_and_continuation_are_joined(self) -> None:
        doc = "```bash\n$ cairn recall digest yesterday \\\n  --json\n```\n"
        regions = list(code_regions("x.md", doc))
        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0].line, 2)
        [invocation] = list(invocations(regions[0]))
        self.assertEqual(
            [w for w, _ in invocation.words], ["recall", "digest", "yesterday", "--json"]
        )

    def test_comment_redirect_and_pipe_end_the_argv(self) -> None:
        self.assertEqual(_argv("cairn sync --all # hourly"), ["sync", "--all"])
        self.assertEqual(_argv("cairn sync --all 2>&1"), ["sync", "--all"])
        self.assertEqual(_argv("cairn sync --all > out.log"), ["sync", "--all"])
        invocation = _one("cairn recall list today --json | head -n 5")
        self.assertEqual([w for w, _ in invocation.words], ["recall", "list", "today", "--json"])
        self.assertIsNone(invocation.pipe_into)  # a pipe into a filter, not an alternation
        self.assertEqual(_one("cairn invoke status | tail ID").pipe_into, "tail")

    def test_angle_placeholder_with_space_is_one_word(self) -> None:
        invocation = _one("cairn recall expand <child file> --max-msgs 5")
        self.assertEqual(invocation.words[2], ("<child file>", True))
        self.assertEqual(_one("cairn resume claude:<id>").words[1], ("claude:<id>", True))

    def test_optional_group_keeps_first_alternative(self) -> None:
        self.assertEqual(
            _argv("cairn claude export [--since 7d | --all]"), ["claude", "export", "--since", "7d"]
        )
        self.assertEqual(
            _argv("cairn agents wait [--session S:ID [--all | --x] | ID...] --json"),
            ["agents", "wait", "--session", "S:ID", "--all", "--json"],
        )

    def test_bare_alternation_keeps_first(self) -> None:
        self.assertEqual(
            _argv("cairn hook install --harness claude|codex|antigravity"),
            ["hook", "install", "--harness", "claude"],
        )

    def test_description_column_ends_the_argv(self) -> None:
        self.assertEqual(
            _argv("cairn routine run NAME   run due days now"), ["routine", "run", "NAME"]
        )
        self.assertEqual(_argv("cairn sync --all  # note"), ["sync", "--all"])

    def test_quoted_ellipsis_is_a_literal_value(self) -> None:
        invocation = _one('cairn codex note "…"')
        self.assertEqual(invocation.words[-1], ("…", False))
        self.assertIsNone(check(invocation))

    def test_ellipsis_word_is_dropped_when_unrecognised(self) -> None:
        invocation = _one("cairn sync --all ...")
        self.assertEqual(invocation.words[-1], ("...", True))
        self.assertIsNone(check(invocation))
        self.assertIsNone(check(_one("cairn agents wait ID...")))

    def test_command_position_wrappers(self) -> None:
        for text in (
            "python -m cairn sync --all",
            "python3 -m cairn sync --all",
            "uv run cairn sync --all",
            "CAIRN_CONFIG=/tmp/c.json cairn sync --all",
            "make check && cairn sync --all",
            "out=$(cairn sync --all)",
        ):
            with self.subTest(text=text):
                self.assertEqual([w for w, _ in _one(text).words], ["sync", "--all"])
        for text in ("a cairn b", "gh repo clone org/cairn --ref x", "import cairn.cli"):
            with self.subTest(text=text):
                self.assertEqual(list(invocations(Region("x.md", 1, "fence", text))), [])

    def test_quoted_or_commented_cairn_is_not_a_command(self) -> None:
        for text in (
            "cairn codex note 'Use ; cairn sync --imaginary in examples'",
            'cairn codex note "Use ; cairn sync --imaginary in examples"',
            "cairn sync --all # old example: ; cairn sync --imaginary",
        ):
            with self.subTest(text=text):
                self.assertEqual(scan_text("x.md", f"```\n{text}\n```\n"), (1, []))
        self.assertEqual(len(list(invocations(Region("x.md", 1, "fence", 'x="$(cairn sync)"')))), 1)


class TestRegions(unittest.TestCase):
    def test_historical_marker_exempts_next_fence_only(self) -> None:
        doc = (
            f"{HISTORICAL}\n```bash\ncairn routine kickstart N --kill\n```\n"
            "```bash\ncairn routine kickstart N --kill\n```\n"
        )
        regions = list(code_regions("x.md", doc))
        self.assertEqual([r.line for r in regions], [6])

    def test_skip_marker_exempts_its_inline_line(self) -> None:
        doc = (
            f"`cairn routine install: nothing to do` {SKIP}\n"
            "`cairn routine install: nothing to do`\n"
            f"{HISTORICAL}\n`cairn invoke --owner x`\n"
            f"{SKIP}\n```\ncairn keeps notes here.\n```\n"
        )
        regions = list(code_regions("x.md", doc))
        self.assertEqual([r.line for r in regions], [2])

    def test_inline_span_must_start_with_cairn(self) -> None:
        doc = "Run `qmd search cairn x` or ``cairn sync --all`` or `cairn.cli`.\n"
        regions = list(code_regions("x.md", doc))
        self.assertEqual([r.text for r in regions], ["cairn sync --all"])

    def test_inline_span_crosses_a_soft_break(self) -> None:
        doc = "Run `cairn sync\n  --imaginary` now.\n"
        self.assertEqual(
            [(r.line, r.text) for r in code_regions("x.md", doc)], [(1, "cairn sync --imaginary")]
        )
        self.assertEqual(len(scan_text("x.md", doc)[1]), 1)
        self.assertEqual(scan_text("x.md", f"{SKIP} `cairn sync\n--imaginary`\n"), (0, []))

    def test_blockquoted_fence_is_scanned(self) -> None:
        doc = "> ```bash\n> cairn sync --imaginary\n> ```\n"
        self.assertEqual([r.text for r in code_regions("x.md", doc)], ["cairn sync --imaginary"])

    def test_longer_fence_holds_a_shorter_one(self) -> None:
        doc = "````md\n```bash\ncairn sync --imaginary\n```\n````\n`cairn sync --all`\n"
        regions = list(code_regions("x.md", doc))
        self.assertEqual(
            [(r.line, r.kind) for r in regions],
            [(2, "fence"), (3, "fence"), (4, "fence"), (6, "inline")],
        )

    def test_continuation_keeps_a_lowercase_argument(self) -> None:
        doc = "```\ncairn recall expand \\\n  example.md\n```\n"
        self.assertEqual(scan_text("x.md", doc), (1, []))

    def test_skip_marker_above_an_inline_line(self) -> None:
        self.assertEqual(list(code_regions("x.md", f"{SKIP}\n`cairn invoke result <id>`\n")), [])

    def test_escaped_quote_stays_inside_the_literal(self) -> None:
        doc = '```\ncairn codex note "say \\"hi; cairn sync --imaginary\\" twice"\n```\n'
        self.assertEqual(scan_text("x.md", doc), (1, []))


class TestRouting(unittest.TestCase):
    def test_every_dispatch_target_has_a_builder(self) -> None:
        for command, spec in _commands().items():
            with self.subTest(command=command, spec=spec):
                module, function = builder_spec(spec).split(":", 1)
                builder = getattr(importlib.import_module(module), function, None)
                self.assertTrue(callable(builder), f"{spec}: missing {module}:{function}")
                self.assertIsInstance(build_route_parser(command), argparse.ArgumentParser)

    def test_builders_do_no_io(self) -> None:
        modules = {builder_spec(spec).split(":", 1)[0] for spec in _commands().values()}
        for module in modules:
            importlib.import_module(module)  # sources read config at import, not in the builder

        def refuse(*_args, **_kwargs):
            raise AssertionError("a parser builder did I/O")

        with (
            mock.patch.object(builtins, "open", refuse),
            mock.patch.object(io, "open", refuse),  # Path.open, read_text, write_text
            mock.patch.object(os, "open", refuse),
            mock.patch.object(os, "listdir", refuse),
            mock.patch.object(os, "scandir", refuse),
            mock.patch.object(subprocess, "Popen", refuse),  # run, check_output, call
        ):
            for spec in _commands().values():
                module, function = builder_spec(spec).split(":", 1)
                with self.subTest(spec=spec):
                    getattr(importlib.import_module(module), function)()

    def test_placeholder_in_command_slot_tries_registry_order(self) -> None:
        self.assertIsNone(check(_one("cairn <source> export --since 7d")))
        self.assertIsNone(check(_one("cairn <command> install --harness codex")))
        error = check(_one("cairn <source> export --no-such-flag"))
        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn(f"cairn {next(iter(dispatch._SOURCES))}", error)


SYNTHETIC = """\
# Synthetic

```bash
cairn no-such-command --all
cairn sync --no-such-flag
cairn recall verify journal.md
cairn routine install | uninstall NAME
cairn sync --all
```
"""


class TestScanner(unittest.TestCase):
    def test_self_check_catches_each_class(self) -> None:
        seen, violations = scan_text("x.md", SYNTHETIC)
        self.assertEqual(seen, 5)
        self.assertEqual([v.split(":")[1] for v in violations], ["4", "5", "6", "7"], violations)
        self.assertIn("unknown command 'no-such-command'", violations[0])
        self.assertIn("unrecognized arguments: --no-such-flag", violations[1])
        self.assertIn("the following arguments are required: --digest", violations[2])
        self.assertIn("usage alternation 'install | uninstall'", violations[3])
        marked = SYNTHETIC.replace("```bash", f"{HISTORICAL}\n```bash")
        self.assertEqual(scan_text("x.md", marked)[1], [])

    def test_inline_relaxations_apply_to_inline_only(self) -> None:
        inline = "Use `cairn recall verify journal.md`, `cairn routine` or `cairn recall digest --out`.\n"
        self.assertEqual(scan_text("x.md", inline), (3, []))
        fenced = (
            "```\ncairn recall verify journal.md\ncairn routine\ncairn recall digest --out\n```\n"
        )
        self.assertEqual(len(scan_text("x.md", fenced)[1]), 3)

    def test_option_value_placeholder_keeps_the_option(self) -> None:
        for text in (
            "cairn recall expand --no-such-flag=<file>",
            "cairn recall expand --max-msgs=<count>",
        ):
            with self.subTest(text=text):
                self.assertEqual(len(scan_text("x.md", f"```\n{text}\n```\n")[1]), 1)
        self.assertEqual(
            scan_text("x.md", "```\ncairn recall expand x.md --max-msgs=<count>\n```\n"), (1, [])
        )
        [violation] = scan_text("x.md", "```\ncairn sync --no-such-flag=<v>\n```\n")[1]
        self.assertIn("unrecognized arguments: --no-such-flag=", violation)

    def test_help_and_version_are_parsed_on_the_route(self) -> None:
        doc = "```\ncairn sync --version\ncairn sync --help\ncairn claude --version\ncairn --version\n```\n"
        seen, violations = scan_text("x.md", doc)
        self.assertEqual(seen, 4)
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("x.md:2: cairn sync --version:", violations[0])

    def test_inline_relaxation_refuses_an_unknown_option(self) -> None:
        inline = "See `cairn recall verify x.md --digset d` and `cairn recall verify x.md`.\n"
        seen, violations = scan_text("x.md", inline)
        self.assertEqual(seen, 2)
        self.assertEqual(len(violations), 1, violations)

    def test_every_alternative_is_parsed(self) -> None:
        for text in (
            "cairn sync [--hook | --no-such-flag | --all]",
            "cairn hook install --harness claude|no-such-harness|antigravity",
        ):
            with self.subTest(text=text):
                self.assertEqual(len(scan_text("x.md", f"```\n{text}\n```\n")[1]), 1)
        valid = "```\ncairn instructions status [--harness claude|codex] [--json]\n```\n"
        self.assertEqual(scan_text("x.md", valid), (1, []))

    def test_compact_subcommand_alternation_is_a_pipe_in_a_fence(self) -> None:
        [violation] = scan_text("x.md", "```\ncairn routine install|uninstall NAME\n```\n")[1]
        self.assertIn("usage alternation 'install | uninstall'", violation)
        self.assertEqual(
            scan_text("x.md", "Use `cairn codex note|close|log --session-id ID`.\n"), (1, [])
        )

    def test_a_pipe_into_a_filter_is_not_an_alternation(self) -> None:
        doc = "```\ncairn invoke list-models | tail -n 5\ncairn <source> list | head\n```\n"
        self.assertEqual(scan_text("x.md", doc), (2, []))
        self.assertEqual(len(scan_text("x.md", "```\ncairn invoke status | tail ID\n```\n")[1]), 1)

    def test_source_placeholder_with_a_bogus_verb_fails(self) -> None:
        for text in ("cairn <source> no-such-verb", "cairn SOURCE no-such-verb --json"):
            with self.subTest(text=text):
                self.assertEqual(len(scan_text("x.md", f"```\n{text}\n```\n")[1]), 1)

    def test_verb_placeholder_tries_every_subcommand(self) -> None:
        doc = (
            '```\ncairn claude <verb> "got it working"\ncairn config <action> KEY VALUE\n'
            'cairn routine <action> NAME --reason "text"\ncairn routine run NAME --day YYYY-MM-DD\n```\n'
        )
        self.assertEqual(scan_text("x.md", doc), (4, []))


class TestDocs(unittest.TestCase):
    def test_scan_finds_the_doc_set(self) -> None:
        total, _violations, per_file = scan()
        self.assertGreaterEqual(total, 400)
        for rel in (
            "README.md",
            "skills/cairn/SKILL.md",
            "skills/recall/SKILL.md",
            "references/harnesses/codex.md",
            "docs/routine-spec.md",
        ):
            with self.subTest(file=rel):
                self.assertGreater(per_file.get(rel, 0), 0)

    def test_every_documented_invocation_parses(self) -> None:
        _total, violations, _per_file = scan()
        self.assertEqual(violations, [], _failure(violations))

    def test_help_overview_invocations_parse(self) -> None:
        path, text = help_document()
        seen, violations = scan_text(path, text)
        self.assertGreaterEqual(seen, 30)
        self.assertEqual(violations, [], _failure(violations))


# ---------------------------------------------------------------------------
# Skill frontmatter
# ---------------------------------------------------------------------------

BASH_RULE = re.compile(r"^Bash\(([A-Za-z0-9_.-]+(?: [A-Za-z0-9_.-]+)*)(:\*| \*)\)$")
SHELL_START = re.compile(
    r"(?:^|&&|\|\||;|\|\s|\$\()\s*(?:\$\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
    r"([A-Za-z0-9_./-]+)((?:\s+[a-z][a-z-]*)*)"
)


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.index("\n---", 4)
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        if sep and not line.startswith(" "):
            fields[key.strip()] = value.strip()
    return fields


def _tools(value: str) -> list[str]:
    entries, depth, current = [], 0, ""
    for char in value:
        depth += char == "("
        depth -= char == ")"
        if char == "," and depth == 0:
            entries.append(current.strip())
            current = ""
        else:
            current += char
    if current.strip():
        entries.append(current.strip())
    return entries


def _shell_commands(text: str) -> Iterator[tuple[int, list[str]]]:
    """(line, leading words) for each command at command position in a shell fence.

    A continued line is joined to its command first, so `\\` then `| jq .` is read.
    """
    opener: tuple[str, int, str] | None = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line, number = lines[i], i + 1
        i += 1
        if opener is None:
            opener = fence_open(line)
            continue
        if fence_closes(line, opener):
            opener = None
            continue
        if opener[2].lower() not in SHELL_FENCES or line.lstrip().startswith("#"):
            continue
        while CONTINUATION.search(line) and i < len(lines) and not fence_closes(lines[i], opener):
            line = CONTINUATION.sub("", line).rstrip() + " " + lines[i].strip()
            i += 1
        code = re.sub(r"\"[^\"]*\"|'[^']*'", '""', line)
        code = re.sub(r"(^|\s)#.*$", "", code)
        for match in SHELL_START.finditer(code):
            yield number, [match.group(1), *match.group(2).split()]


class TestSkillFrontmatter(unittest.TestCase):
    def _skills(self) -> list[Path]:
        skills = sorted((ROOT / "skills").glob("*/SKILL.md"))
        self.assertTrue(skills)
        return skills

    def test_bash_rules_are_well_formed(self) -> None:
        for path in self._skills():
            for entry in _tools(_frontmatter(path.read_text()).get("allowed-tools", "")):
                if entry.startswith("Bash(") or entry.startswith("Bash "):
                    with self.subTest(skill=path.parent.name, entry=entry):
                        self.assertRegex(entry, BASH_RULE)

    def test_restricted_skills_allow_every_program_they_run(self) -> None:
        for path in self._skills():
            text = path.read_text()
            tools = _tools(_frontmatter(text).get("allowed-tools", ""))
            if not tools or "Bash" in tools:
                continue
            prefixes = [match.group(1).split() for match in map(BASH_RULE.match, tools) if match]
            for line, words in _shell_commands(text):
                with self.subTest(skill=path.parent.name, line=line, words=words):
                    self.assertTrue(
                        any(words[: len(prefix)] == prefix for prefix in prefixes),
                        f"{path.relative_to(ROOT)}:{line}: {words[0]} runs but allowed-tools "
                        f"has no Bash({words[0]} *) rule",
                    )


class TestShellCommands(unittest.TestCase):
    def test_continuation_and_env_prefix_are_read(self) -> None:
        doc = (
            "```bash\ncairn recall list yesterday --json \\\n  | jq .\n"
            'cairn=1 python3 -c "import os"\n```\n'
        )
        self.assertEqual(
            list(_shell_commands(doc)),
            [(2, ["cairn", "recall", "list", "yesterday"]), (2, ["jq"]), (4, ["python3"])],
        )

    def test_non_shell_fences_and_comments_are_ignored(self) -> None:
        doc = "```text\njq .\n```\n```sh\n# jq .\nrg x\n```\n"
        self.assertEqual(list(_shell_commands(doc)), [(6, ["rg", "x"])])


if __name__ == "__main__":
    unittest.main()
