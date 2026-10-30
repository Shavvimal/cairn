"""``cairn instructions``: the recall-first block in the user's agent instruction file.

cairn writes one short marked block into the user-level instruction file of Claude
Code (``CLAUDE.md``) or Codex (``AGENTS.md``, or a non-empty ``AGENTS.override.md``).
The block tells the agent to run recall before it asks the user about past work.
Text outside the markers is never changed. An opt-out (``remove``, or a block the
user deleted by hand) is kept in a state ledger, so a later ``install`` from the
setup or update workflow does not add the block back without ``--force``.

Spec: docs/recall-first-block-spec.md.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import state_home
from .harnesses.claude import claude_home
from .harnesses.codex_models import codex_home
from .storage import ConcurrentChangeError, atomic_write, lock_path, locked

# The block text version. It changes only when the text changes, never with a release.
BLOCK_VERSION = 1
LEDGER_SCHEMA_VERSION = 1

BEGIN_RE = re.compile(r"^<!-- cairn:recall-first:begin(?: v(\d+))? -->$")
END_LINE = "<!-- cairn:recall-first:end -->"

HARNESSES = ("claude", "codex")
LABELS = {"claude": "Claude Code", "codex": "Codex"}
# (recall skill, search skill) as each harness names them.
SKILL_NAMES = {"claude": ("/cairn:recall", "/cairn:search"), "codex": ("$recall", "$search")}

_BODY = (
    "## Past work: check cairn before you ask",
    "",
    "cairn keeps this user's past coding sessions, notes and meetings on this machine.",
    '- Before you ask the user about past work, a past decision or "where were we", run '
    '`{recall}` with a time ("yesterday", "last week") or a topic.',
    "- If the question involves a literal (an error string, a path, an identifier), run "
    "`{search}` with `--exact`.",
    "- If you still need to ask, ask with a hypothesis built from the results: "
    '"On Tuesday you chose X because Y. Is that still the plan?"',
    "- Recall adds to what the user tells you. It never replaces it: when the two differ, "
    "the user is right.",
    "- cairn manages the text between these markers. Opt out: "
    "`cairn instructions remove --harness {harness}`.",
)

REASON_REMOVED = "cairn instructions remove"
REASON_DELETED = "block deleted by hand"

# File states found on disk.
NO_FILE, ABSENT, CURRENT, STALE, EDITED, DAMAGED = (
    "no-file",
    "absent",
    "current",
    "stale",
    "edited",
    "damaged",
)


class InstructionsError(Exception):
    """A refusal or failure: the message is printed after ``Error: `` and exits 1."""

    def __init__(self, message: str, path: Path | None = None) -> None:
        super().__init__(message)
        self.path = path


def begin_line(version: int = BLOCK_VERSION) -> str:
    return f"<!-- cairn:recall-first:begin v{version} -->"


def render_lines(harness: str) -> list[str]:
    """The block for ``harness`` as lines without newlines, markers included."""
    recall, search = SKILL_NAMES[harness]
    body = [line.format(recall=recall, search=search, harness=harness) for line in _BODY]
    return [begin_line(), *body, END_LINE]


def render(harness: str, newline: str = "\n") -> str:
    """The exact block bytes for ``harness`` in the given newline style."""
    return "".join(line + newline for line in render_lines(harness))


def block_sha256(text: str) -> str:
    """sha256 of a block, newline style ignored (CRLF counts as LF)."""
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# targets and the ledger
# ---------------------------------------------------------------------------


def harness_home(harness: str) -> Path:
    return claude_home() if harness == "claude" else codex_home()


def _has_text(path: Path) -> bool:
    try:
        return bool(path.read_bytes().strip())
    except OSError:
        return False


def target_path(harness: str) -> Path:
    """The user instruction file the harness loads (section 3.4 of the spec)."""
    home = harness_home(harness)
    if harness == "claude":
        return home / "CLAUDE.md"
    override = home / "AGENTS.override.md"
    return override if _has_text(override) else home / "AGENTS.md"


def _require_home(harness: str, path: Path) -> None:
    home = harness_home(harness)
    if not home.is_dir():
        label = LABELS[harness]
        raise InstructionsError(
            f"{home} does not exist; is {label} installed? "
            f"cairn edits {path} only inside an existing {label} home",
            path,
        )


def ledger_path() -> Path:
    return state_home() / "cairn" / "recall-first.json"


def load_ledger() -> dict[str, Any]:
    path = ledger_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"schema_version": LEDGER_SCHEMA_VERSION, "harnesses": {}}
    except (OSError, UnicodeDecodeError) as exc:
        raise _bad_ledger(path, str(exc)) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise _bad_ledger(path, str(exc)) from exc
    if not isinstance(data, dict):
        raise _bad_ledger(path, "top level is not an object")
    if data.get("schema_version") != LEDGER_SCHEMA_VERSION:
        raise _bad_ledger(path, f"unknown schema_version {data.get('schema_version')!r}")
    harnesses = data.get("harnesses")
    if not isinstance(harnesses, dict) or not all(isinstance(v, dict) for v in harnesses.values()):
        raise _bad_ledger(path, "'harnesses' is not an object of objects")
    return data


def _bad_ledger(path: Path, detail: str) -> InstructionsError:
    return InstructionsError(
        f"{path} is not valid JSON ({detail}); move it aside to reset the recall-first opt-outs"
    )


def save_ledger(data: dict[str, Any]) -> None:
    atomic_write(ledger_path(), json.dumps(data, indent=2, sort_keys=True) + "\n")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# scanning a file
# ---------------------------------------------------------------------------


def _split_lines(text: str) -> list[str]:
    """Split on ``\\n`` only, keeping line ends (``str.splitlines`` splits on more)."""
    return re.findall(r"[^\n]*\n|[^\n]+$", text)


def _bare(line: str) -> str:
    line = line[:-1] if line.endswith("\n") else line
    return line[:-1] if line.endswith("\r") else line


@dataclass
class Scan:
    """One instruction file as read: its bytes, lines and marker positions."""

    path: Path
    exists: bool
    raw: bytes | None
    bom: bool = False
    newline: str = "\n"
    lines: list[str] = field(default_factory=list)
    begins: list[int] = field(default_factory=list)
    ends: list[int] = field(default_factory=list)
    version: int | None = None

    @property
    def damaged(self) -> bool:
        if not self.begins and not self.ends:
            return False
        return len(self.begins) != 1 or len(self.ends) != 1 or self.ends[0] < self.begins[0]

    @property
    def present(self) -> bool:
        return bool(self.begins) and not self.damaged

    @property
    def block(self) -> str:
        return "".join(self.lines[self.begins[0] : self.ends[0] + 1])

    def text(self, lines: list[str]) -> str:
        return ("﻿" if self.bom else "") + "".join(lines)


def scan(path: Path) -> Scan:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return Scan(path=path, exists=False, raw=None)
    except OSError as exc:
        raise InstructionsError(f"cannot read {path}: {exc}", path) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InstructionsError(f"{path} is not UTF-8 text; cairn will not edit it", path) from exc
    bom = text.startswith("﻿")
    lines = _split_lines(text[1:] if bom else text)
    result = Scan(path=path, exists=True, raw=raw, bom=bom, lines=lines)
    if lines and lines[0].endswith("\r\n"):
        result.newline = "\r\n"
    for index, line in enumerate(lines):
        bare = _bare(line)
        match = BEGIN_RE.match(bare)
        if match:
            result.begins.append(index)
            if match.group(1):
                result.version = int(match.group(1))
        elif bare == END_LINE:
            result.ends.append(index)
    return result


def classify(found: Scan, harness: str, record: dict[str, Any] | None) -> str:
    if not found.exists:
        return NO_FILE
    if found.damaged:
        return DAMAGED
    if not found.present:
        return ABSENT
    if found.block == render(harness, found.newline):
        return CURRENT
    recorded = (record or {}).get("block_sha256")
    if recorded:
        return EDITED if recorded != block_sha256(found.block) else STALE
    return EDITED if found.version == BLOCK_VERSION else STALE


def _damaged_error(found: Scan) -> InstructionsError:
    return InstructionsError(
        f"recall-first markers in {found.path} are damaged ({len(found.begins)} begin, "
        f"{len(found.ends)} end; expected one of each, begin first); "
        "fix or delete them by hand, then re-run",
        found.path,
    )


@dataclass
class Inspection:
    """Read-only view of one harness: target, file state and the ledger record."""

    harness: str
    path: Path
    state: str
    version: int | None
    record: dict[str, Any] | None
    override_active: bool

    @property
    def declined(self) -> bool:
        return (self.record or {}).get("status") == "declined"

    @property
    def deleted_by_hand(self) -> bool:
        installed = (self.record or {}).get("status") == "installed"
        return installed and self.state in (ABSENT, NO_FILE) and _same_path(self.record, self.path)


def inspect(harness: str, ledger: dict[str, Any] | None = None) -> Inspection:
    """Classify the harness's target without the lock and without writing anything."""
    path = target_path(harness)
    _require_home(harness, path)
    if ledger is None:
        ledger = load_ledger()
    record = ledger["harnesses"].get(harness)
    found = scan(path)
    return Inspection(
        harness=harness,
        path=path,
        state=classify(found, harness, record),
        version=found.version if found.present else None,
        record=record,
        override_active=path.name == "AGENTS.override.md",
    )


# ---------------------------------------------------------------------------
# install and remove
# ---------------------------------------------------------------------------


@dataclass
class Result:
    harness: str
    path: Path
    state_before: str
    state_after: str
    action: str
    block_version: int | None
    dry_run: bool
    message: str
    diff: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "harness": self.harness,
            "path": str(self.path),
            "state_before": self.state_before,
            "state_after": self.state_after,
            "action": self.action,
            "block_version": self.block_version,
            "dry_run": self.dry_run,
        }
        if self.diff is not None:
            data["diff"] = self.diff
        return data


def _diff(path: Path, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            _split_lines(before),
            _split_lines(after),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def _write(found: Scan, text: str) -> None:
    """Write ``text`` through a symlink, keep the mode, abort if the file changed."""
    current_mode = stat.S_IMODE(os.stat(found.path).st_mode) if found.exists else 0o644
    try:
        atomic_write(
            found.path,
            text,
            mode=current_mode,
            follow_symlinks=True,
            check_unchanged=True,
            expected=found.raw,
        )
    except ConcurrentChangeError as exc:
        raise _race(found.path) from exc
    except OSError as exc:
        raise InstructionsError(f"cannot write {found.path}: {exc}", found.path) from exc


def _race(path: Path) -> InstructionsError:
    return InstructionsError(
        f"{path} changed while cairn was updating it; nothing was written. Re-run", path
    )


def _skipped(harness: str, record: dict[str, Any]) -> str:
    date = str(record.get("updated_at") or "")[:10] or "an unknown date"
    reason = record.get("reason") or REASON_REMOVED
    return (
        f"skipped: the recall-first block for {harness} was removed on {date} ({reason}); "
        "pass --force to add it back"
    )


def _decline(ledger: dict[str, Any], harness: str, path: Path, reason: str) -> None:
    record = dict(ledger["harnesses"].get(harness) or {})
    record.update(
        {
            "block_sha256": record.get("block_sha256"),
            "block_version": record.get("block_version"),
            "created_file": bool(record.get("created_file")),
            "old_target": None,
            "path": str(path),
            "reason": reason,
            "status": "declined",
            "updated_at": _now(),
        }
    )
    ledger["harnesses"][harness] = record


def _same_path(record: dict[str, Any] | None, path: Path) -> bool:
    """True when the ledger record is about ``path`` (a Codex override moves the target)."""
    return (record or {}).get("path") == str(path)


def _old_target(record: dict[str, Any] | None, path: Path) -> tuple[Path, bool] | None:
    """A Codex block left in a file Codex no longer loads, with its ``created_file`` flag.

    An ``AGENTS.override.md`` that gains text moves the target. The previous target is
    the record's ``path``, or ``old_target`` once a later install has moved the record.
    """
    record = record or {}
    candidates: list[tuple[str, bool]] = []
    if record.get("path") and record["path"] != str(path):
        candidates.append((record["path"], bool(record.get("created_file"))))
    old = record.get("old_target")
    if isinstance(old, dict) and old.get("path") and old["path"] != str(path):
        candidates.append((old["path"], bool(old.get("created_file"))))
    for raw, created in candidates:
        try:
            found = scan(Path(raw))
        except InstructionsError:
            continue
        if found.present:
            return Path(raw), created
    return None


def install(harness: str, *, force: bool = False, dry_run: bool = False) -> Result:
    path = target_path(harness)
    _require_home(harness, path)
    load_ledger()  # fail on an unreadable ledger before taking the lock
    with locked(lock_path(ledger_path(), "recall-first")):
        ledger = load_ledger()
        record = ledger["harnesses"].get(harness)
        found = scan(path)
        state = classify(found, harness, record)
        if state == DAMAGED:
            raise _damaged_error(found)

        def result(state_after: str, action: str, message: str, **extra: Any) -> Result:
            version = BLOCK_VERSION if state_after == CURRENT else found.version
            return Result(
                harness, path, state, state_after, action, version, dry_run, message, **extra
            )

        installed = (record or {}).get("status") == "installed"
        if state in (ABSENT, NO_FILE) and installed and _same_path(record, path):
            # The user deleted the block by hand: that is an opt-out.
            if not dry_run:
                _decline(ledger, harness, path, REASON_DELETED)
                save_ledger(ledger)
                record = ledger["harnesses"][harness]
            else:
                record = {**record, "reason": REASON_DELETED, "updated_at": _now()}
            if not force:
                return result(state, "skipped", _skipped(harness, record))
        if (record or {}).get("status") == "declined" and not force:
            return result(state, "skipped", _skipped(harness, record or {}))
        if state == CURRENT:
            return result(
                CURRENT,
                "unchanged",
                f"unchanged: recall-first block v{BLOCK_VERSION} in {path} is current",
            )
        if state == EDITED and not force:
            raise InstructionsError(
                f"the recall-first block in {path} was edited by hand since cairn wrote it; "
                "move your text outside the markers, or pass --force to replace it",
                path,
            )

        block = render(harness, found.newline)
        if state == NO_FILE:
            new_text = block
            action, message = (
                "created",
                (
                    f"created: {path} did not exist; cairn created it with only the recall-first block"
                ),
            )
        elif state == ABSENT:
            body = "".join(found.lines)
            if body:
                if not body.endswith("\n"):
                    body += found.newline
                body += found.newline
            new_text = ("﻿" if found.bom else "") + body + block
            action = "added"
            message = f"added: recall-first block v{BLOCK_VERSION} appended to {path}"
        else:
            begin, end = found.begins[0], found.ends[0]
            lines = [*found.lines[:begin], block, *found.lines[end + 1 :]]
            new_text = found.text(lines)
            action = "updated"
            was = f"v{found.version}" if found.version is not None else "an unversioned block"
            message = (
                f"updated: recall-first block in {path} upgraded from {was} to v{BLOCK_VERSION}"
            )

        before = found.text(found.lines) if found.exists else ""
        notes = []
        old = _old_target(record, path)
        if old is not None:
            notes.append(
                f"note: a recall-first block is still in {old[0]}, which Codex no longer "
                f"loads; run 'cairn instructions remove --harness {harness}' first to clear it"
            )
        if dry_run:
            return result(CURRENT, action, message, diff=_diff(path, before, new_text), notes=notes)

        _write(found, new_text)
        previous_created = bool((record or {}).get("created_file")) and _same_path(record, path)
        ledger["harnesses"][harness] = {
            "block_sha256": block_sha256(block),
            "block_version": BLOCK_VERSION,
            "created_file": state == NO_FILE or previous_created,
            "old_target": None if old is None else {"path": str(old[0]), "created_file": old[1]},
            "path": str(path),
            "reason": None,
            "status": "installed",
            "updated_at": _now(),
        }
        save_ledger(ledger)
        return result(CURRENT, action, message, notes=notes)


def _strip_block(found: Scan) -> list[str]:
    begin, end = found.begins[0], found.ends[0]
    lines = [*found.lines[:begin], *found.lines[end + 1 :]]
    ran_to_end = end + 1 == len(found.lines)
    if ran_to_end and begin > 0 and _bare(found.lines[begin - 1]) == "":
        del lines[begin - 1]
    return lines


def _remove_file(found: Scan) -> None:
    try:
        if found.path.read_bytes() != found.raw:
            raise _race(found.path)
        found.path.unlink()
    except FileNotFoundError as exc:
        raise _race(found.path) from exc
    except OSError as exc:
        raise InstructionsError(f"cannot delete {found.path}: {exc}", found.path) from exc


def remove(harness: str, *, dry_run: bool = False) -> Result:
    path = target_path(harness)
    _require_home(harness, path)
    load_ledger()
    with locked(lock_path(ledger_path(), "recall-first")):
        ledger = load_ledger()
        record = ledger["harnesses"].get(harness)
        found = scan(path)
        state = classify(found, harness, record)
        if state == DAMAGED:
            raise _damaged_error(found)
        notes: list[str] = []
        old = _old_target(record, path)
        if not found.present:
            if not dry_run:
                if old is not None:
                    notes.append(_clear_old(*old))
                _decline(ledger, harness, path, REASON_REMOVED)
                save_ledger(ledger)
            message = f"absent: no recall-first block in {path}; opt-out recorded"
            return Result(
                harness, path, state, state, "absent", None, dry_run, message, notes=notes
            )

        lines = _strip_block(found)
        created = bool((record or {}).get("created_file"))
        delete = created and not "".join(lines).strip() and not found.path.is_symlink()
        new_text = "" if delete else found.text(lines)
        message = (
            f"removed: recall-first block from {path}; opt-out recorded, "
            "install will not add it back without --force"
        )
        if delete:
            message += " (file deleted: cairn created it and nothing else was in it)"
        state_after = NO_FILE if delete else ABSENT
        if dry_run:
            diff = _diff(path, found.text(found.lines), new_text)
            return Result(
                harness, path, state, state_after, "removed", None, True, message, diff=diff
            )
        if delete:
            _remove_file(found)
        else:
            _write(found, new_text)
        if old is not None:
            notes.append(_clear_old(*old))
        _decline(ledger, harness, path, REASON_REMOVED)
        save_ledger(ledger)
        return Result(
            harness, path, state, state_after, "removed", None, False, message, notes=notes
        )


def _clear_old(old_path: Path, created: bool) -> str:
    """Remove the block from a file the harness no longer loads (Codex override moves)."""
    found = scan(old_path)
    lines = _strip_block(found)
    if created and not "".join(lines).strip() and not old_path.is_symlink():
        _remove_file(found)
        return f"removed: recall-first block from {old_path} (file deleted)"
    _write(found, found.text(lines))
    return f"removed: recall-first block from {old_path}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _status_line(view: Inspection) -> str:
    state = "declined" if view.state in (ABSENT, NO_FILE) and view.declined else view.state
    version = f"v{view.version}" if view.version is not None else "-"
    return f"{view.harness} {state} {version} {view.path}"


def _status_json(view: Inspection) -> dict[str, Any]:
    return {
        "harness": view.harness,
        "path": str(view.path),
        "state_before": view.state,
        "state_after": view.state,
        "action": "none",
        "block_version": view.version,
        "dry_run": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cairn instructions",
        description="Manage the recall-first block in the user instruction file "
        "(~/.claude/CLAUDE.md, or AGENTS.md in the Codex home).",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    install_p = sub.add_parser("install", help="add or upgrade the block")
    install_p.add_argument("--harness", choices=HARNESSES, required=True)
    install_p.add_argument("--dry-run", action="store_true", help="print a diff; write nothing")
    install_p.add_argument(
        "--force", action="store_true", help="override an opt-out or a hand edit"
    )
    install_p.add_argument("--json", action="store_true")
    remove_p = sub.add_parser("remove", help="delete the block and record an opt-out")
    remove_p.add_argument("--harness", choices=HARNESSES, required=True)
    remove_p.add_argument("--dry-run", action="store_true", help="print a diff; write nothing")
    remove_p.add_argument("--json", action="store_true")
    show_p = sub.add_parser("show", help="print the exact block for a paste by hand")
    show_p.add_argument("--harness", choices=HARNESSES, required=True)
    status_p = sub.add_parser("status", help="print the block state per harness")
    status_p.add_argument("--harness", choices=HARNESSES)
    status_p.add_argument("--json", action="store_true")
    return parser


def _fail(error: InstructionsError, harness: str, as_json: bool) -> int:
    print(f"Error: {error}", file=sys.stderr)
    if as_json:
        path = str(error.path) if error.path is not None else None
        print(json.dumps({"harness": harness, "path": path, "error": str(error)}))
    return 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    as_json = bool(getattr(args, "json", False))
    if args.action == "show":
        sys.stdout.write(render(args.harness))
        return 0
    if args.action == "status":
        harnesses = [args.harness] if args.harness else list(HARNESSES)
        views = []
        for harness in harnesses:
            if args.harness is None and not harness_home(harness).is_dir():
                continue
            try:
                views.append(inspect(harness))
            except InstructionsError as error:
                return _fail(error, harness, as_json)
        if as_json:
            print(json.dumps([_status_json(view) for view in views], indent=2))
        elif not views:
            print("no Claude Code or Codex home found; nothing to report", file=sys.stderr)
        for view in [] if as_json else views:
            print(_status_line(view))
        return 0
    try:
        if args.action == "install":
            result = install(args.harness, force=args.force, dry_run=args.dry_run)
        else:
            result = remove(args.harness, dry_run=args.dry_run)
    except InstructionsError as error:
        return _fail(error, args.harness, as_json)
    for note in result.notes:
        print(note, file=sys.stderr)
    if as_json:
        print(json.dumps(result.as_json(), indent=2))
    elif result.dry_run and result.diff:
        sys.stdout.write(result.diff)
    else:
        print(result.message)
    return 0


__all__ = [
    "BLOCK_VERSION",
    "SKILL_NAMES",
    "Inspection",
    "InstructionsError",
    "inspect",
    "install",
    "main",
    "remove",
    "render",
]
