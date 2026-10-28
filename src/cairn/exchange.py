"""Peer-exchange documents and the per-thread state behind them (plan B4, D18).

Every ``cairn invoke`` thread keeps its state in ``thread.json`` inside its run
directory (``data_root/logs/invoke/<peer>/<thread_id>/``). :class:`ThreadState`
mirrors that file. The exchange document is the searchable view of the same
state: ``exchange-<peer>-<short>.md`` in the folder of the session that anchors
it (the caller's session folder when known, else the peer's), one ``### Round``
section per round, with the standard preserved tail and ``## My Notes`` block so
hand-written notes survive every re-render.

The document is deliberately not a session document: its ``type`` is
``agent-exchange`` and it carries no ``source:`` or ``native_id:`` key, so the
catalog parses it but no source index, listing or recall ever returns it. The
renderer validates its own head before anything is written (D18): a head key that
collides with a preserved-tail key would make ``rewrite_preserved_tail`` cut the
frontmatter in the wrong place, and a malformed block would be reported by the
catalog on every listing.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from contextlib import redirect_stdout
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path

from .config import CairnConfig
from .frontmatter import (
    _TAIL_KEYS,
    FrontmatterParseError,
    extract_my_notes_section,
    head_close,
    head_open,
    notes_block,
    parse_frontmatter,
    preserved_tail,
    read_frontmatter_head,
    resolve_title,
    title_line,
)
from .paths import exchange_path, session_dir_of
from .storage import atomic_write, document_lock, locked

EXCHANGE_TYPE = "agent-exchange"
ROUNDS_HEADING = "## Rounds"
_FORBIDDEN_HEAD_KEYS = ("source", "native_id")
_TAIL_NAMES = tuple(key.rstrip(":") for key in _TAIL_KEYS)
TITLE_PROMPT_CHARS = 60


def now_iso() -> str:
    """The current UTC time as an ISO-8601 string with second precision."""
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Thread state (thread.json)
# ---------------------------------------------------------------------------


@dataclass
class ThreadState:
    """The contents of one thread's ``thread.json``.

    ``rounds`` holds one dict per round: the prompt (without the preamble), the
    exact native argv, process identities, timestamps, outcome, progress bullets,
    the answer and the publication ledger. ``path`` is where the file lives; it is
    not serialised. ``profile`` is the name as given (built-in or config profile);
    ``profile_base`` and ``profile_args`` are what it resolved to, reused verbatim on
    a resume. ``owner`` is the owner label (``--owner``, for example
    ``routine:daily``), not to be confused with :func:`document_owner`.
    """

    peer: str
    thread_id: str | None
    cwd: str
    profile: str
    native_policy: list[str]
    model: str | None = None
    effort: str | None = None
    caller_session: str | None = None
    caller_session_doc: str | None = None
    peer_session_doc: str | None = None
    anchor: str | None = None
    anchor_session: str | None = None
    exchange_doc: str | None = None
    forked_from: str | None = None
    resume_command: str | None = None
    owner: str | None = None
    profile_base: str | None = None
    profile_args: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    rounds: list[dict] = field(default_factory=list)
    path: Path | None = field(default=None, compare=False)

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("path")
        return data

    @classmethod
    def from_dict(cls, data: dict, path: Path | None = None) -> ThreadState:
        if not isinstance(data, dict):
            raise ValueError("thread.json must hold a JSON object")
        known = {f.name for f in fields(cls)} - {"path"}
        missing = {"peer", "cwd", "profile"} - set(data)
        if missing:
            raise ValueError(f"thread.json is missing keys: {', '.join(sorted(missing))}")
        values = {key: value for key, value in data.items() if key in known}
        values.setdefault("thread_id", None)
        values.setdefault("native_policy", [])
        return cls(**values, path=path)

    @classmethod
    def load(cls, path: Path) -> ThreadState:
        """Read ``thread.json``; a torn read is retried once before failing loudly."""
        for attempt in range(2):
            try:
                return cls.from_dict(json.loads(path.read_text(encoding="utf-8")), path)
            except json.JSONDecodeError as exc:
                if attempt:
                    raise ValueError(f"unreadable {path}: {exc}") from exc
                time.sleep(0.05)
        raise AssertionError("unreachable")

    def save(self, path: Path | None = None) -> None:
        target = path or self.path
        if target is None:
            raise ValueError("ThreadState has no path to save to")
        self.path = target
        atomic_write(target, json.dumps(self.to_dict(), indent=1, ensure_ascii=False) + "\n")

    @property
    def last_round(self) -> dict | None:
        return self.rounds[-1] if self.rounds else None

    @property
    def run_status(self) -> str:
        """``running`` while the last round is open, else that round's outcome."""
        last = self.last_round
        if last is None or not last.get("finished_at"):
            return "running"
        return str(last.get("outcome") or "unknown")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def peer_short_id(peer: str, thread_id: str) -> str:
    """The filename short id: Codex head8+tail4 (as its exporter), Claude first 8."""
    if peer == "codex":
        head = thread_id.replace("-", "")[:8]
        tail = thread_id.split("-")[-1][:4]
        return f"{head}{tail}"
    return thread_id[:8]


def exchange_document_path(anchor: Path, peer: str, thread_id: str) -> Path:
    return exchange_path(anchor, peer, peer_short_id(peer, thread_id))


def _anchor(state: ThreadState) -> tuple[Path, str | None, bool] | None:
    """``(folder, anchor_session, final)``. A peer-folder anchor is provisional while a
    known caller has no exported document yet: the document moves to the caller's
    folder once the caller export lands."""
    if state.anchor:
        return Path(state.anchor), state.anchor_session, True
    if state.caller_session_doc:
        return session_dir_of(Path(state.caller_session_doc)), state.caller_session, True
    if state.peer_session_doc and state.thread_id:
        final = not state.caller_session
        peer_ref = f"{state.peer}:{state.thread_id}"
        return session_dir_of(Path(state.peer_session_doc)), peer_ref, final
    return None


def resolve_anchor(config: CairnConfig, state: ThreadState) -> Path | None:
    """The folder the exchange document lives in: the recorded anchor, else the
    caller session's folder, else the peer session's folder, else ``None``."""
    found = _anchor(state)
    return found[0] if found else None


def document_owner(path: Path) -> str | None:
    """The ``thread_id`` of an existing exchange document (``""`` when it has none),
    or ``None`` when no file is there. An unparseable file raises ``ValueError``."""
    if not path.exists():
        return None
    try:
        fm = read_frontmatter_head(path)
    except (FrontmatterParseError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot parse frontmatter in {path}: {exc}") from exc
    return str(fm.get("thread_id") or "")


def exchange_destination(folder: Path, peer: str, thread_id: str) -> Path:
    """A collision-safe document path: the short-id name, or, when another thread
    already owns it, the name suffixed with ``sha256(thread_id)[:16]``."""
    base = exchange_document_path(folder, peer, thread_id)
    if document_owner(base) in (None, thread_id):
        return base
    digest = hashlib.sha256(thread_id.encode()).hexdigest()[:16]
    alternative = base.with_name(f"{base.stem}-{digest}.md")
    if document_owner(alternative) in (None, thread_id):
        return alternative
    raise ValueError(f"{alternative} belongs to another thread; refusing to overwrite it")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _quoted(value: object) -> str:
    text = " ".join(str(value).split())
    return '"' + text.replace('"', '\\"') + '"'


def _plain(value: object) -> str:
    if value is None or value == "":
        return "null"
    text = " ".join(str(value).split())
    if re.fullmatch(r"[A-Za-z0-9._:/@+-]+", text) and not text.startswith(("[", "{", ">")):
        return text
    return _quoted(text)


def validate_head(lines: list[str]) -> None:
    """Reject a head that could corrupt the document or leak it into a session index.

    Raises ``ValueError`` when a head key starts with a preserved-tail key (so
    ``rewrite_preserved_tail`` would cut there), or is ``source`` / ``native_id``.
    """
    for line in lines:
        if line == "---" or line.startswith(" ") or ":" not in line:
            continue
        key = line.split(":", 1)[0].strip()
        if key in _FORBIDDEN_HEAD_KEYS:
            raise ValueError(f"exchange head must not carry {key!r}")
        if key.startswith(_TAIL_NAMES):
            raise ValueError(f"exchange head key {key!r} collides with a preserved-tail key")


def _fence(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _title_from(state: ThreadState) -> str:
    first = state.rounds[0].get("prompt", "") if state.rounds else ""
    words = " ".join(str(first).split())
    return f"{state.peer} round 1: {words[:TITLE_PROMPT_CHARS]}".rstrip()


def _usage_text(usage: object) -> str:
    if not isinstance(usage, dict) or not usage:
        return "n/a"
    return ", ".join(f"{key}={value}" for key, value in usage.items() if value is not None)


def _round_lines(round_: dict) -> list[str]:
    number = round_.get("round", "?")
    lines = [f"### Round {number} — {round_.get('started_at') or 'unknown'}", ""]
    prompt = str(round_.get("prompt") or "")
    fence = _fence(prompt)
    lines += ["**Prompt**", "", f"{fence}text", prompt, fence, ""]
    lines += ["**Progress**", ""]
    progress = [str(item) for item in round_.get("progress") or []]
    lines += [f"- {item}" for item in progress] or ["- (none yet)"]
    lines.append("")
    answer = round_.get("last_message")
    lines += ["**Answer**", "", str(answer) if answer else "(no answer yet)", ""]
    status = round_.get("outcome") if round_.get("finished_at") else "running"
    duration = round_.get("duration_s")
    lines += [
        "**Result**",
        "",
        f"- status: {status}",
        f"- duration: {f'{duration:.1f}s' if isinstance(duration, int | float) else 'n/a'}",
        f"- exit code: {round_.get('exit_code')}",
        f"- caller: {round_.get('caller_session') or 'an operator'}",
        f"- events: {round_.get('events_path') or 'n/a'}",
        f"- transcript: {round_.get('transcript_path') or 'n/a'}",
        f"- peer session: {round_.get('peer_session_doc') or 'n/a'}",
        f"- usage: {_usage_text(round_.get('usage'))}",
    ]
    billing = round_.get("billing")
    if isinstance(billing, dict):
        from .invoke import billing_text

        lines.append(f"- billing: {billing_text(billing)}")
    files = round_.get("files_written")
    if isinstance(files, list):
        lines.append(f"- files written: {', '.join(str(f) for f in files) if files else 'none'}")
    denied = round_.get("denied")
    if isinstance(denied, list) and denied:
        shown = [f"{d.get('tool', 'tool')} {d.get('target', '')}".strip() for d in denied]
        lines.append(f"- denied: {'; '.join(shown)[:500]}")
    if round_.get("error"):
        lines.append(f"- error: {' '.join(str(round_['error']).split())[:500]}")
    lines.append("")
    return lines


def render_exchange(state: ThreadState, existing_fm: dict | None, my_notes: str | None) -> str:
    """The full exchange document for ``state`` (pure; validated before returning)."""
    if not state.thread_id:
        raise ValueError("an exchange document needs a thread id")
    created = state.created_at or now_iso()
    first = state.rounds[0] if state.rounds else {}
    last = state.last_round or {}
    caller_ref = state.caller_session
    title = resolve_title({"title": _title_from(state)}, existing_fm)
    head = head_open(EXCHANGE_TYPE, created[:10], "thread_id", state.thread_id)
    head += [
        f"peer: {state.peer}",
        f"caller: {caller_ref.split(':', 1)[0] if caller_ref else 'operator'}",
        f"caller_session: {_plain(caller_ref)}",
        f"peer_session: {state.peer}:{state.thread_id}",
        f"anchor_session: {_plain(state.anchor_session)}",
        f"model: {_plain(state.model)}",
        f"effort: {_plain(state.effort)}",
        f"profile: {_plain(state.profile)}",
        f"native_policy: {_quoted(' '.join(last.get('native_policy') or state.native_policy))}",
        f"cwd: {_quoted(state.cwd)}",
        f"run_status: {state.run_status}",
        f"rounds: {len(state.rounds)}",
        f"started_at: {_plain(first.get('started_at') or created)}",
    ]
    if state.forked_from:
        head.append(f"forked_from: {_plain(state.forked_from)}")
    head.append(title_line(title))
    head += head_close(last.get("finished_at") or last.get("heartbeat_at") or created)
    validate_head(head)
    tail_fm = {**(existing_fm or {}), "owner": state.owner} if state.owner else existing_fm
    lines = [*head, *preserved_tail(tail_fm, "active"), "---", "", f"# {title}", ""]
    lines += notes_block(my_notes, "exports")
    lines += [ROUNDS_HEADING, ""]
    for round_ in state.rounds:
        lines += _round_lines(round_)
    text = "\n".join(lines).rstrip("\n") + "\n"
    try:
        parsed = parse_frontmatter(text)
    except FrontmatterParseError as exc:
        raise ValueError(f"exchange frontmatter does not round-trip: {exc}") from exc
    if parsed.get("type") != EXCHANGE_TYPE or parsed.get("thread_id") != state.thread_id:
        raise ValueError("exchange frontmatter does not round-trip its type and thread id")
    return text


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _qmd_folder(config: CairnConfig, folder: Path) -> str | None:
    for name in config.collections:
        try:
            rel = folder.resolve().relative_to(config.output_dir(name).resolve())
        except ValueError:
            continue
        return f"qmd://{name}/{rel.as_posix()}"
    return None


def _describe_folder(config: CairnConfig, folder: Path, anchor_session: str | None) -> None:
    """Best-effort ``qmd context add`` for a newly created session folder."""
    uri = _qmd_folder(config, folder)
    if uri is None:
        return
    from .qmd import add_context

    description = (
        f"Agent exchanges and subagent documents spawned by {anchor_session or 'a session'}"
    )
    try:
        with redirect_stdout(sys.stderr):
            ok = add_context(uri, description, config.qmd_binary)
    except OSError as exc:
        print(f"Warning: qmd context add failed for {uri}: {exc}", file=sys.stderr)
        return
    if not ok:
        print(f"Warning: qmd context add failed for {uri}", file=sys.stderr)


def _read_existing(path: Path, thread_id: str) -> tuple[dict | None, str | None]:
    owner = document_owner(path)
    if owner is None:
        return None, None
    if owner != thread_id:
        raise ValueError(f"{path} belongs to thread {owner!r}; refusing to overwrite it")
    content = path.read_text(encoding="utf-8")
    try:
        existing_fm = parse_frontmatter(content)
    except FrontmatterParseError as exc:
        raise ValueError(f"cannot parse frontmatter in {path}: {exc}") from exc
    return existing_fm, extract_my_notes_section(content, conversation_heading=ROUNDS_HEADING)


def write_exchange(
    config: CairnConfig, state: ThreadState, *, describe_folder: bool = True
) -> Path | None:
    """Render and write the exchange document; ``None`` while no anchor exists.

    The document never overwrites another thread's file (see
    :func:`exchange_destination`). A provisional document in the peer's folder
    moves to the caller's folder, notes and preserved fields included, once the
    caller's document exists. When the write creates the session folder and
    ``describe_folder`` is set, a best-effort ``qmd context add`` describes it
    (``--no-index`` runs skip that so they never touch QMD). Records the anchor and
    document path in ``state`` (the caller saves it).
    """
    if not state.thread_id:
        return None
    found = _anchor(state)
    if found is None:
        return None
    folder, anchor_session, final = found
    recorded = Path(state.exchange_doc) if state.exchange_doc else None
    if state.anchor and recorded is not None:
        path = recorded
    else:
        path = exchange_destination(folder, state.peer, state.thread_id)
    previous = recorded if recorded is not None and recorded != path else None
    created_folder = not path.parent.exists()
    # The document records the anchor it is written under, so set it before rendering
    # (and put the old value back if the write fails).
    prior_session = state.anchor_session
    state.anchor_session = anchor_session
    try:
        with locked(document_lock(path)):
            existing_fm, my_notes = _read_existing(path, state.thread_id)
            if existing_fm is None and previous is not None:
                existing_fm, my_notes = _read_existing(previous, state.thread_id)
            text = render_exchange(state, existing_fm, my_notes)
            atomic_write(path, text)
    except BaseException:
        state.anchor_session = prior_session
        raise
    if previous is not None and document_owner(previous) == state.thread_id:
        previous.unlink()
    state.anchor = str(folder) if final else None
    state.exchange_doc = str(path)
    if created_folder and describe_folder:
        _describe_folder(config, path.parent, anchor_session)
    return path


__all__ = [
    "EXCHANGE_TYPE",
    "ROUNDS_HEADING",
    "ThreadState",
    "document_owner",
    "exchange_destination",
    "exchange_document_path",
    "now_iso",
    "peer_short_id",
    "render_exchange",
    "resolve_anchor",
    "validate_head",
    "write_exchange",
]
