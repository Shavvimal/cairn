"""``cairn recall digest`` and ``cairn recall frame``: one UTC day, condensed, with provenance.

A digest is one Markdown file per session with activity in the window, holding only
that day's frames in condensed form (:mod:`cairn.digest_condense`), and one
``.frames.jsonl`` sidecar that locates every frame of the whole session (ordinal,
offset from the conversation heading, declared length, SHA-1 prefix). A model reads
the digest and cites ``f<ordinal>``; ``cairn recall frame`` resolves that citation
back to the exported text and says whether the frame changed since.

The digest is deterministic: the same exports and flags give byte-identical files.
It reads the exported store only, writes only into its output directory, and calls
no model.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path

from . import provenance as provenance_mod
from .digest_condense import (
    Condenser,
    parse_parts,
    peer_answer,
    peer_prompt,
    preamble_match,
    select,
    text_of,
    verdict_lines,
)
from .frames import (
    MARKER,
    CorruptExport,
    Frame,
    LegacyExport,
    effective_times,
    locate,
    read_text,
    utc_iso,
)
from .frames import sha1_8 as frame_sha1_8
from .frontmatter import _escape_scalar
from .mutations import _LINK_REF, ROUTINE_OWNER_PREFIX, _bare_link
from .recall_gaps import coverage_note
from .storage import atomic_write
from .store import SessionRecord
from .timeutil import parse_date_range

DIGEST_SOURCES = ("claude", "codex", "antigravity", "cursor")
DEFAULT_MAX_CHARS = 40000
OUT_MARKER = ".cairn-digest"
_DIGEST_FILE = re.compile(r"^[a-z-]+-[A-Za-z0-9]+(\.frames\.jsonl|\.md)$")
_ARTIFACT_PATH = re.compile(r"^- `(.+)`$")
_ARTIFACT_LINK = re.compile(r"^- \[[^\]]*\]\((.+)\)$")


@dataclass(frozen=True)
class Lineage:
    """Where a session sits: its role, its caller and the orphan-peer facts (6.1)."""

    role: str  # primary, peer or subagent
    parent: str | None
    orphan_peer: bool = False
    owner: str | None = None  # from an owner-form preamble (``routine:daily``)


@dataclass
class Digest:
    """One written session: its two files and its ``--json`` row."""

    record: SessionRecord
    stem: str
    markdown: str
    sidecar: str
    row: dict
    first_ts: datetime
    owner: str | None = None  # front matter ``owner``, else the owner-form preamble


def one_day(expr: str) -> tuple[datetime, datetime]:
    """The ``[start, end)`` of ``expr``; ValueError unless it is exactly one day."""
    start, end = parse_date_range(expr)
    if end - start != timedelta(days=1):
        raise ValueError(f"digest needs a one-day window; got {start.date()} to {end.date()}")
    return start, end


def default_out(start: datetime) -> Path:
    """``$XDG_CACHE_HOME/cairn/digests/DATE``, the output directory without ``--out``."""
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache / "cairn" / "digests" / start.date().isoformat()


def check_out(out: Path) -> None:
    """Refuse a non-empty directory that a previous digest did not write."""
    if out.exists() and not out.is_dir():
        raise ValueError(f"refusing to write into {out}: not a directory")
    if out.is_dir() and any(out.iterdir()) and not (out / OUT_MARKER).is_file():
        raise ValueError(f"refusing to write into non-empty {out} without {OUT_MARKER}")


def lineage(record: SessionRecord, frames: list[Frame]) -> Lineage:
    """Role and parent, in order: subagent, peer by link, peer by preamble, primary (6.1).

    The preamble counts only in the first user frame with text (:func:`first_user_frame`).
    """
    if record.is_subagent:
        return Lineage("subagent", f"{record.source}:{record.parent_id}")
    links = record.metadata.get("links")
    entries = [_bare_link(item) for item in links] if isinstance(links, list) else []
    parents = [e.split(" ", 1)[1].strip() for e in entries if e.startswith("parent ")]
    if parents:
        if len(parents) > 1:
            print(
                f"Warning: {record.reference} has {len(parents)} parent links; using {parents[0]}",
                file=sys.stderr,
            )
        return Lineage("peer", parents[0])
    first = first_user_frame(frames)
    if first is None or (match := preamble_match(first.body)) is None:
        return Lineage("primary", None)
    caller = match["caller"]
    if caller and match["caller_source"] == match["ref_source"] and _LINK_REF.match(caller):
        return Lineage("peer", caller)
    return Lineage("primary", None, orphan_peer=True, owner=match["owner"])


def first_user_frame(frames: list[Frame]) -> Frame | None:
    """The first user frame with text after its header, or ``None``.

    Only this frame can carry the invoke preamble of a peer run, the test that
    ``provenance.at_export`` applies to the first user message: a preamble pasted
    later in a session a person drove makes it neither a peer nor ``agent``. The
    Codex exporter already drops host-context user messages (``<environment_context>``,
    AGENTS.md instructions), so they never come first.
    """
    for frame in frames:
        if frame.role != "user":
            continue
        text = frame.body.replace("\r\n", "\n")
        if (text.split("\n", 1)[1] if "\n" in text else "").strip():
            return frame
    return None


def opens_with_preamble(frames: list[Frame]) -> bool:
    """Whether the first user frame with text starts with the invoke preamble."""
    first = first_user_frame(frames)
    return first is not None and preamble_match(first.body) is not None


def artifacts(text: str, anchor: int) -> list[str]:
    """Paths or URIs listed in the first ``## Artifacts`` section before My Notes."""
    region = text[:anchor].replace("\r\n", "\n")
    region = region.split("\n## My Notes\n", 1)[0]
    if "\n## Artifacts\n" not in region:
        return []
    section = region.split("\n## Artifacts\n", 1)[1]
    found = []
    for line in section.split("\n"):
        if line.startswith("## "):
            break
        if match := _ARTIFACT_PATH.match(line) or _ARTIFACT_LINK.match(line):
            found.append(match[1])
    return found


def marker(frame: Frame, in_window: bool = True) -> str:
    """The ``<!-- fN TS ROLE -->`` line that cites one frame."""
    ts = utc_iso(frame.ts) if frame.ts else "-"
    extra = "" if in_window else " in_window: false"
    return f"<!-- f{frame.ordinal} {ts} {frame.role}{extra} -->"


def _block(frame: Frame, body: str, in_window: bool) -> str:
    """A frame's marker line followed by its condensed body, if any."""
    line = marker(frame, in_window)
    return f"{line}\n{body}" if body else line


def _context(frames: list[Frame], first: int, times: list, start: datetime) -> Frame | None:
    """The last user frame before the window, for a session continued from an earlier day."""
    for frame in reversed(frames[:first]):
        stamp = times[frame.ordinal]
        if frame.role in ("user", "message") and stamp is not None and stamp < start:
            return frame
    return None


def primary_blocks(
    record: SessionRecord, frames: list[Frame], window: list[bool], times: list, start: datetime
) -> tuple[list[tuple[int, bool, bool, str]], Condenser]:
    """``(ordinal, is_user, in_window, block)`` for a primary or subagent session, and
    the condenser that made them (its ``refs`` resolve dedup lines after the cap)."""
    condenser = Condenser(cursor=record.source == "cursor")
    in_window = [f for f in frames if window[f.ordinal]]
    blocks = []
    context = _context(frames, in_window[0].ordinal, times, start)
    if context is not None:
        body = condenser.frame(context.role, context.body, context.ordinal)
        blocks.append((context.ordinal, True, False, _block(context, body, False)))
    for frame in in_window:
        body = condenser.frame(frame.role, frame.body, frame.ordinal)
        is_user = frame.role in ("user", "message")
        blocks.append((frame.ordinal, is_user, True, _block(frame, body, True)))
    return blocks, condenser


def peer_blocks(frames: list[Frame], window: list[bool]) -> list[tuple[int, bool, bool, str]]:
    """Per round: the prompt head and the last answer with text; plus every verdict line."""
    starts = [f.ordinal for f in frames if f.role == "user" and preamble_match(f.body)]
    bounds = sorted({0, *starts, len(frames)})
    blocks: dict[int, tuple[int, bool, bool, str]] = {}
    for low, high in pairwise(bounds):
        if not any(window[low:high]):
            continue
        if low in starts:
            prompt = frames[low]
            body = peer_prompt(prompt.body)
            blocks[low] = (low, True, window[low], _block(prompt, body, window[low]))
        for frame in reversed(frames[low:high]):
            if (
                frame.role == "assistant"
                and window[frame.ordinal]
                and text_of(parse_parts(frame.body)).strip()
            ):
                body = peer_answer(frame.body)
                blocks[frame.ordinal] = (frame.ordinal, False, True, _block(frame, body, True))
                break
    for frame in frames:
        if frame.role != "assistant" or not window[frame.ordinal] or frame.ordinal in blocks:
            continue
        if verdicts := verdict_lines(frame.body):
            blocks[frame.ordinal] = (frame.ordinal, False, True, _block(frame, verdicts, True))
    return [blocks[o] for o in sorted(blocks)]


def _restore_outputs(blocks: list[tuple[int, bool, bool, str]], kept: set[int], refs: dict) -> bool:
    """Replace, in place, each ``[same output as fN]`` of a kept block whose frame ``N``
    the cap dropped with the full output. Returns whether anything changed; the caller
    then selects again, because a block only ever grows."""
    changed = False
    for index, (ordinal, is_user, in_window, block) in enumerate(blocks):
        if ordinal not in kept:
            continue
        for target, placeholder, full in refs.get(ordinal, ()):
            if target not in kept and placeholder in block:
                block = block.replace(placeholder, full, 1)
                changed = True
        blocks[index] = (ordinal, is_user, in_window, block)
    return changed


def _scalar(value: object) -> str:
    """One front-matter value: null, a bool, an int, or a double-quoted one-line string."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return '"' + _escape_scalar(str(value).replace("\r", " ").replace("\n", " ")) + '"'


def front_matter(fields: list[tuple[str, object]]) -> str:
    """The ``---`` block for ``fields``; a list becomes a block sequence of quoted items."""
    lines = ["---"]
    for key, value in fields:
        if isinstance(value, list):
            lines.append(f"{key}:" if value else f"{key}: []")
            lines.extend("  - " + _scalar(str(v)) for v in value)
        else:
            lines.append(f"{key}: {_scalar(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def build(
    record: SessionRecord, text: str, start: datetime, end: datetime, max_chars: int
) -> Digest | None:
    """The digest of one session, or ``None`` when it has no frame in the window.

    Raises :class:`LegacyExport` or :class:`CorruptExport` from the walk.
    """
    anchor, frames = locate(text)
    times = effective_times(frames)
    window = [t is not None and start <= t < end for t in times]
    if not any(window):
        return None
    family = lineage(record, frames)
    condenser = None
    if family.role == "peer":
        blocks = peer_blocks(frames, window)
    else:
        blocks, condenser = primary_blocks(record, frames, window, times, start)
    kept = select([(o, is_user, b) for o, is_user, _, b in blocks], max_chars)
    while condenser is not None and _restore_outputs(blocks, kept, condenser.refs):
        kept = select([(o, is_user, b) for o, is_user, _, b in blocks], max_chars)
    body = "\n".join(b for o, _, _, b in blocks if o in kept)
    dropped = [b for o, _, in_win, b in blocks if in_win and o not in kept]
    today = [f for f in frames if window[f.ordinal]]
    first_ts = times[today[0].ordinal]
    last_ts = times[today[-1].ordinal]
    assert first_ts is not None and last_ts is not None  # in-window frames have a time
    fields: list[tuple[str, object]] = [
        ("ref", record.reference),
        ("source", record.source),
        ("title", record.title),
        ("project", record.project),
        ("cwd", record.cwd),
        ("role", family.role),
        ("parent", family.parent),
        ("orphan_peer", family.orphan_peer),
    ]
    # A document exported before the key existed reads ``agent`` when its first user
    # message with text is an invoke preamble (a parent link is front-matter evidence).
    value = provenance_mod.effective(
        record.metadata, record.source, record.path, peer_body=opens_with_preamble(frames)
    )
    fields.append(("provenance", value))
    owner = provenance_mod.owner_of(record.metadata) or family.owner
    if owner:
        fields.append(("owner", str(owner)))
    fields += [
        ("date", start.date().isoformat()),
        ("session_started_at", utc_iso(record.started_at) if record.started_at else None),
        ("first_ts", utc_iso(first_ts)),
        ("last_ts", utc_iso(last_ts)),
        ("frames_today", len(today)),
        ("frames_total", len(frames)),
        ("raw_chars_today", sum(f.length for f in today)),
        ("artifacts", artifacts(text, anchor)),
        ("truncated_frames", len(dropped)),
        ("truncated_chars", sum(len(b) for b in dropped)),
    ]
    sidecar = "".join(
        json.dumps(
            {
                "ordinal": f.ordinal,
                "ts": utc_iso(f.ts) if f.ts else None,
                "role": f.role,
                "offset": f.offset,
                "length": f.length,
                "sha1_8": frame_sha1_8(f.body),
                "in_window": window[f.ordinal],
                "kept": f.ordinal in kept,
            }
        )
        + "\n"
        for f in frames
    )
    row = {
        "ref": record.reference,
        "path": "",
        "frames_path": "",
        "role": family.role,
        "parent": family.parent,
        "provenance": value,
        "owner": str(owner) if owner else None,
        "project": record.project,
        "cwd": record.cwd,
        "frames_today": len(today),
        "chars": len(body),
        "truncated": {"frames_dropped": len(dropped), "chars_dropped": sum(map(len, dropped))},
    }
    markdown = front_matter(fields) + body + "\n"
    return Digest(record, "", markdown, sidecar, row, first_ts, str(owner) if owner else None)


def _stems(digests: list[Digest]) -> None:
    """``<source>-<shortid>``; sessions whose short forms collide use the full cleaned id."""
    clean = [re.sub(r"[^A-Za-z0-9]", "", d.record.session_id) for d in digests]
    short = [f"{d.record.source}-{c[:12]}" for d, c in zip(digests, clean, strict=True)]
    for digest, cleaned, stem in zip(digests, clean, short, strict=True):
        collides = short.count(stem) > 1
        digest.stem = f"{digest.record.source}-{cleaned}" if collides else stem


def write(out: Path, digests: list[Digest]) -> None:
    """Remove stale digest files, then write the marker and every session's two files."""
    check_out(out)
    out.mkdir(parents=True, exist_ok=True)
    for path in sorted(out.iterdir()):
        if path.is_file() and _DIGEST_FILE.match(path.name):
            path.unlink()
    atomic_write(out / OUT_MARKER, "cairn recall digest output; files here are replaced.\n")
    for digest in digests:
        markdown, frames = out / f"{digest.stem}.md", out / f"{digest.stem}.frames.jsonl"
        atomic_write(markdown, digest.markdown)
        atomic_write(frames, digest.sidecar)
        digest.row["path"], digest.row["frames_path"] = str(markdown), str(frames)


def _routine_owned(owner: object) -> bool:
    """Whether ``owner`` names a routine (``routine:<name>``): its session is a routine's
    own output and stays out of the next digest."""
    return isinstance(owner, str) and owner.startswith(ROUTINE_OWNER_PREFIX)


def run_digest(
    args: argparse.Namespace,
    records: list[SessionRecord],
    window: tuple[datetime, datetime],
    *,
    coverage: Callable[[bool, list[dict[str, str]]], dict] | None = None,
) -> int:
    """Digest every qualifying session.

    Exit codes follow the routine's ``prepare`` contract: 0 when at least one session
    was written (skipped legacy or corrupt files only warn and appear in ``skipped``),
    3 when the day has no input (zero sessions written). Errors raise and exit 1.
    ``coverage``, given whether any session was written and the skipped sessions,
    returns the store gaps for the window (:func:`cairn.recall_gaps.coverage`); it adds
    a ``coverage`` key to the JSON and one stderr note when coverage is degraded. It
    never changes the exit code.

    Sessions owned by a routine (``owner: routine:<name>``, in front matter or in the
    owner-form preamble) and their subagents are left out and listed in ``excluded``,
    unless ``--include-routine-sessions`` is given.
    """
    start, end = window
    out = Path(args.out).expanduser().absolute() if args.out else default_out(start)
    check_out(out)
    if not args.include_subagents:
        records = [r for r in records if not r.is_subagent]
    selected = [
        r
        for r in records
        if r.in_window(start, end)
        and (r.is_subagent or r.user_messages is None or r.user_messages >= args.min_msgs)
    ]
    keep_routine = args.include_routine_sessions
    digests: list[Digest] = []
    skipped: list[dict[str, str]] = []
    excluded: dict[str, str] = {}  # ref -> owner
    for record in sorted(selected, key=lambda r: r.reference):
        owner = record.metadata.get("owner")
        if not keep_routine and _routine_owned(owner):
            excluded[record.reference] = str(owner)
            continue
        try:
            digest = build(record, read_text(record.path), start, end, args.max_chars)
        except LegacyExport:
            print(
                f"Warning: skipping {record.reference}: legacy export without frames; "
                f"run cairn {record.source} export --all",
                file=sys.stderr,
            )
            skipped.append({"ref": record.reference, "reason": "legacy"})
            continue
        except CorruptExport as exc:
            print(f"Warning: skipping {record.reference}: {exc}", file=sys.stderr)
            skipped.append({"ref": record.reference, "reason": "corrupt"})
            continue
        if digest is None:
            continue
        if not keep_routine and _routine_owned(digest.owner):
            excluded[record.reference] = str(digest.owner)
            continue
        digests.append(digest)
    if excluded:
        kept = []
        for digest in digests:
            parent = digest.row["parent"]
            if digest.row["role"] == "subagent" and parent in excluded:
                excluded[digest.record.reference] = excluded[parent]
            else:
                kept.append(digest)
        digests = kept
    if getattr(args, "provenance", None):
        digests = [d for d in digests if d.row["provenance"] in args.provenance]
    digests.sort(key=lambda d: (d.first_ts, d.record.reference))
    _stems(digests)
    write(out, digests)
    rows = [d.row for d in digests]
    excluded_rows = [{"ref": ref, "owner": excluded[ref]} for ref in sorted(excluded)]
    cov = coverage(bool(digests), skipped) if coverage is not None else None
    if args.json:
        payload: dict = {"sessions": rows, "skipped": skipped, "excluded": excluded_rows}
        if cov is not None:
            payload["coverage"] = cov
        print(json.dumps(payload, indent=2))
    else:
        for row in rows:
            dropped = row["truncated"]["frames_dropped"]
            note = f"  [truncated {dropped}]" if dropped else ""
            owner = f"  owner={row['owner']}" if row["owner"] else ""
            print(
                f"{row['ref']}  {row['role']}  {row['provenance']}  "
                f"frames_today={row['frames_today']}  chars={row['chars']}{owner}{note}"
            )
        if excluded_rows:
            print(
                f"{len(excluded_rows)} routine sessions excluded; "
                "pass --include-routine-sessions to keep them"
            )
        print(f"{len(rows)} sessions written to {out}")
    if cov is not None and (degraded := coverage_note(cov)):
        print(degraded, file=sys.stderr)
    return 0 if digests else 3


def _stored_row(path: Path, ordinal: int) -> dict:
    """The sidecar row for ``ordinal``; ValueError when the file has none."""
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip() and (row := json.loads(line)).get("ordinal") == ordinal:
                return row
    raise ValueError(f"no row for ordinal {ordinal} in {path}")


def load_sidecar(path: Path) -> dict[int, dict]:
    """Every row of a ``.frames.jsonl`` sidecar by ordinal, read once."""
    rows: dict[int, dict] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip() and isinstance(row := json.loads(line), dict):
                ordinal = row.get("ordinal")
                if isinstance(ordinal, int):
                    rows[ordinal] = row
    return rows


def frame_unchanged(text: str, anchor: int, frame: Frame, row: dict) -> bool:
    """Whether ``frame`` is the frame the sidecar ``row`` recorded: its marker sits at
    ``anchor + offset`` with the stored length, the fresh walk puts it at the same
    offset, and the body has the same ``sha1_8``."""
    offset = row.get("offset")
    found = None
    if isinstance(offset, int) and offset >= 0:
        found = MARKER.match(text, anchor + offset)
    return (
        found is not None
        and int(found[1]) == row.get("length")
        and frame.offset == offset
        and frame_sha1_8(frame.body) == row.get("sha1_8")
    )


def changed_detail(ref: str, ordinal: int, row: dict, frame: Frame) -> str:
    """The text that says a cited frame changed since the digest wrote ``row``."""
    return (
        f"{ref} frame {ordinal} changed since the digest "
        f"(stored sha1_8 {row.get('sha1_8')}, offset {row.get('offset')}; "
        f"now sha1_8 {frame_sha1_8(frame.body)}, offset {frame.offset}); "
        "the export was re-rendered"
    )


def run_frame(args: argparse.Namespace, record: SessionRecord) -> int:
    """Print one frame as exported; with ``--frames``, warn when it changed since the digest."""
    text = read_text(record.path)
    try:
        anchor, frames = locate(text)
    except LegacyExport as exc:
        raise ValueError(
            f"{record.reference}: {exc}; run cairn {record.source} export --all"
        ) from None
    except CorruptExport as exc:
        raise ValueError(f"{record.reference}: {exc}") from None
    ordinal = args.ordinal
    if not 0 <= ordinal < len(frames):
        raise ValueError(f"ordinal {ordinal} out of range; session has {len(frames)} frames")
    stored = _stored_row(Path(args.frames).expanduser(), ordinal) if args.frames else None
    frame = frames[ordinal]
    if stored is None:
        print(frame.body)
        print(f"sha1_8 {frame_sha1_8(frame.body)}", file=sys.stderr)
        return 0
    if not frame_unchanged(text, anchor, frame, stored):
        print(
            f"Warning: {changed_detail(record.reference, ordinal, stored, frame)}",
            file=sys.stderr,
        )
    print(frame.body)
    return 0


__all__ = ["DIGEST_SOURCES", "build", "check_out", "one_day", "run_digest", "run_frame"]
