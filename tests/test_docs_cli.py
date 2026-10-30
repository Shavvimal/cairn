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
FENCE = re.compile(r"^\s*(```|~~~)")
CONTINUATION = re.compile(r"\\\s*$")
INLINE_SPAN = re.compile(r"(`+)(.+?)(?<!`)\1(?!`)")
WRAPPERS = (
    r"(?:(?:[A-Z_][A-Z0-9_]*=\S*|uv\s+run|python3?\s+-m|nohup|exec|command|time"
    r"|timeout\s+\S+)\s+)*"
)
COMMAND_START = re.compile(
    r"(?:^|&&|\|\||;|\|\s|\$\(|`|\()\s*(?:\$\s+)?" + WRAPPERS + r"(?<![\w./-])cairn(?=\s|$)"
)
NAME_PLACEHOLDER = re.compile(r"^[A-Z][A-Z0-9_]*(?::[A-Z0-9_]+)?$")
COMMAND_WORD = re.compile(r"^[a-z][a-z-]*$")
HELP_FLAGS = frozenset({"-h", "--help", "--version", "-V"})
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


# ---------------------------------------------------------------------------
# Regions
# ---------------------------------------------------------------------------


def code_regions(path: str, text: str) -> Iterator[Region]:
    """Yield fenced lines (continuations joined) and inline ``cairn`` spans, minus markers."""
    lines = text.split("\n")
    in_fence = False
    exempt = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if FENCE.match(line):
            in_fence = not in_fence
            above = lines[i - 1] if i > 0 else ""
            exempt = in_fence and (HISTORICAL in above or SKIP in above)
            i += 1
            continue
        if in_fence:
            start, code = i, line
            while (
                CONTINUATION.search(code) and i + 1 < len(lines) and not FENCE.match(lines[i + 1])
            ):
                i += 1
                code = CONTINUATION.sub(" ", code) + " " + lines[i].strip()
            if not exempt:
                yield Region(path, start + 1, "fence", code)
        elif not (SKIP in line or HISTORICAL in line or (i > 0 and HISTORICAL in lines[i - 1])):
            for match in INLINE_SPAN.finditer(line):
                span = match.group(2).strip().replace("\\|", "|")
                if span.startswith("cairn "):
                    yield Region(path, i + 1, "inline", span)
        i += 1


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


def _is_placeholder(word: str) -> bool:
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
            end = text.find(c, i + 1)
            end = n if end < 0 else end
            chars.append(text[i + 1 : end])
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
            match = re.match(r"\s*([a-z][a-z-]*)", text[i + 1 :])
            pipe_into = match.group(1) if match else None
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


def invocations(region: Region) -> Iterator[Invocation]:
    """Each ``cairn`` at command position in a region, tokenised up to its end."""
    for match in COMMAND_START.finditer(region.text):
        tail = region.text[match.end() :]
        words, pipe_into, stop = tokenise(tail)
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


def parse_error(parser: argparse.ArgumentParser, argv: list[str]) -> str | None:
    """None when ``argv`` parses, else the parser's last stderr line."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        try:
            parser.parse_args(argv)
        except SystemExit as exc:
            lines = [line for line in err.getvalue().splitlines() if line.strip()]
            return lines[-1] if lines else f"exit {exc.code}"
    return None


def _relaxed(error: str, argv: list[str], inline: bool) -> bool:
    if not inline:
        return False
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
        for value in pool:
            trial = [value if word == sentinel else word for word in argv]
            error = parse_error(parser, trial)
            if error is None or _relaxed(error, trial, inline):
                return trial
            if _names(error, value):
                continue
            if named or error != current:
                return trial
        return None

    for _ in range(4 * len(argv) + 4):
        error = parse_error(parser, argv)
        if error is None or _relaxed(error, argv, inline):
            return None
        live = [word for word in argv if word in placeholders]
        named = [word for word in live if word in error]
        if named:
            sentinel = named[0]
            original = placeholders[sentinel]
            if "unrecognized arguments" in error and original.endswith(("...", "…")):
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
        routes = commands
    elif first in commands:
        routes = [first]
    else:
        return f"cairn: unknown command {first!r}"
    if invocation.pipe_into is not None:
        for command in routes:
            if invocation.pipe_into in subcommand_names(command):
                left = next((w for w, _ in reversed(words)), first)
                return PIPE_MESSAGE.format(left, invocation.pipe_into)
    if any(word in HELP_FLAGS for word, _ in words[1:]):
        return None  # rule 14: these exit before parsing completes
    placeholders: dict[str, str] = {}
    rest: list[str] = []
    for word, placeholder in words[1:]:
        if placeholder:
            sentinel = SENTINEL.format(len(placeholders))
            placeholders[sentinel] = word
            rest.append(sentinel)
        else:
            rest.append(word)
    inline = invocation.kind == "inline"
    first_error: str | None = None
    for command in routes:
        error = _check_route(command, rest, placeholders, inline)
        if error is None:
            return None
        first_error = first_error or error
    assert first_error is not None
    for sentinel, original in placeholders.items():
        first_error = first_error.replace(sentinel, original)
    return first_error


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
    for folder in ("skills", "references", "docs"):
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
        self.assertEqual(invocation.pipe_into, "head")

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
            mock.patch.object(subprocess, "run", refuse),
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
    r"(?:^|&&|\|\||;|\|\s|\$\()\s*(?:\$\s+)?([A-Za-z0-9_./-]+)((?:\s+[a-z][a-z-]*)*)"
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
    """(line, leading words) for each command at command position in a shell fence."""
    fence: str | None = None
    continued = False
    for number, line in enumerate(text.split("\n"), start=1):
        opened = FENCE.match(line)
        if opened:
            fence = None if fence is not None else line.strip().lstrip("`~").strip().lower()
            continue
        if fence not in SHELL_FENCES or not line.strip() or line.lstrip().startswith("#"):
            continue
        if continued:
            continued = bool(CONTINUATION.search(line))
            continue  # the rest of the previous command's argv
        continued = bool(CONTINUATION.search(line))
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


if __name__ == "__main__":
    unittest.main()
