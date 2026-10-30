"""The withdrawal ledger: claims withdrawn with ``forget``, kept as fingerprints.

``<data_root>/withdrawn.jsonl`` holds one JSON object per line. A record names a
claim by the SHA-256 of its normalised text, never by the text itself, plus a reason
from :data:`REASONS`, the session and row it came from, and a UTC time. The state of
a fingerprint is the last event for it in file order: ``withdrawn`` or
``unwithdrawn``.

Writers append under the ledger lock with one ``O_APPEND`` write and an fsync. A crash
mid-append leaves an unterminated last line: readers ignore it with a warning, and
the next append truncates it. Any other line that is not a valid record fails loudly
with its line number, so a hand-edited ledger never lifts a withdrawal in silence.

Lock order: a caller that also writes a session document takes the document lock
first and this ledger lock second. Nothing takes them the other way round. A forced
``decide`` keeps the ledger lock until its row is written and only then appends
``unwithdrawn``, so a failed or interrupted write leaves the claim withdrawn.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import unicodedata
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from .storage import lock_path, locked

if TYPE_CHECKING:
    from .config import CairnConfig

# The closed reason vocabulary. No free text: free text could carry the claim into
# the ledger, which keeps fingerprints only.
REASONS: Final[tuple[str, ...]] = (
    "stale",
    "wrong_source",
    "hallucination",
    "cross_attribution",
    "private",
    "other",
)
EVENTS: Final[tuple[str, ...]] = ("withdrawn", "unwithdrawn")
NORM_VERSION: Final = 1
RECORD_VERSION: Final = 1
LEDGER_NAME: Final = "withdrawn.jsonl"
FIELDS: Final[tuple[str, ...]] = (
    "v",
    "ts",
    "event",
    "source",
    "session_id",
    "row",
    "fingerprint",
    "norm",
    "reason",
)
# Both are used with ``fullmatch``: ``$`` would also accept a trailing newline.
_TS_RE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_FINGERPRINT_RE: Final = re.compile(r"[0-9a-f]{64}")
_TS_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"


class LedgerError(ValueError):
    """The ledger holds a line that is not a valid record; printed after ``Error: ``."""


class EmptyClaim(ValueError):
    """The claim has no characters left after normalisation."""


# --------------------------------------------------------------------------
# Normalisation and fingerprint
# --------------------------------------------------------------------------


def normalise_claim(claim: str) -> str:
    """NFKC, casefold, drop every Unicode punctuation character plus the backtick and
    the tilde (Markdown code and strike marks), then collapse whitespace.

    Punctuation is deleted, not replaced, so ``rate-limit`` and ``ratelimit`` match.
    Symbols stay (``C++``, ``<``, ``|``); ``%`` is punctuation in Unicode (Po)
    and goes. Version :data:`NORM_VERSION`."""
    text = unicodedata.normalize("NFKC", claim).casefold()
    text = "".join(
        ch for ch in text if not unicodedata.category(ch).startswith("P") and ch not in "`~"
    )
    return " ".join(text.split())


def fingerprint(claim: str) -> str:
    """The SHA-256 hex digest of the normalised claim; :class:`EmptyClaim` if empty."""
    norm = normalise_claim(claim)
    if not norm:
        raise EmptyClaim("empty claim after normalisation")
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Withdrawal:
    """One ledger record."""

    ts: str
    event: str
    source: str
    session_id: str
    row: int
    fingerprint: str
    reason: str | None

    @property
    def withdrawn(self) -> bool:
        return self.event == "withdrawn"

    @property
    def date(self) -> str:
        return self.ts[:10]

    @property
    def ref(self) -> str:
        return f"{self.source}:{self.session_id[:8]}"


def ledger_path(config: CairnConfig) -> Path:
    return config.data_root / LEDGER_NAME


def ledger_lock(path: Path) -> Path:
    return lock_path(path, "withdrawals")


def _now() -> str:
    return datetime.now(UTC).strftime(_TS_FORMAT)


def record(
    event: str, *, fingerprint: str, source: str, session_id: str, row: int, reason: str | None
) -> dict:
    """A ledger record, keys in :data:`FIELDS` order."""
    return {
        "v": RECORD_VERSION,
        "ts": _now(),
        "event": event,
        "source": source,
        "session_id": session_id,
        "row": row,
        "fingerprint": fingerprint,
        "norm": NORM_VERSION,
        "reason": reason,
    }


def _is_version(value: object, version: int) -> bool:
    """``value`` is the integer ``version`` (not ``True``, not ``1.0``)."""
    return type(value) is int and value == version


def _real_ts(value: object) -> bool:
    if not isinstance(value, str) or not _TS_RE.fullmatch(value):
        return False
    try:
        datetime.strptime(value, _TS_FORMAT)
    except ValueError:
        return False
    return True


def _problem(data: object) -> str | None:
    """Why ``data`` is not a valid record, or ``None``."""
    if not isinstance(data, dict):
        return "not a JSON object"
    missing = [key for key in FIELDS if key not in data]
    if missing:
        return f"missing {', '.join(missing)}"
    if not _is_version(data["v"], RECORD_VERSION):
        return f"unknown record version {data['v']!r}"
    if not _real_ts(data["ts"]):
        return f"ts must be YYYY-MM-DDTHH:MM:SSZ, got {data['ts']!r}"
    if data["event"] not in EVENTS:
        return f"unknown event {data['event']!r}"
    for key in ("source", "session_id"):
        if not isinstance(data[key], str) or not data[key]:
            return f"{key} must be a non-empty string"
    row = data["row"]
    if not isinstance(row, int) or isinstance(row, bool) or row < 1:
        return f"row must be an integer of 1 or greater, got {row!r}"
    if not isinstance(data["fingerprint"], str) or not _FINGERPRINT_RE.fullmatch(
        data["fingerprint"]
    ):
        return "fingerprint must be 64 lowercase hex characters"
    if not _is_version(data["norm"], NORM_VERSION):
        return f"unknown normalisation version {data['norm']!r}"
    reason = data["reason"]
    if data["event"] == "withdrawn" and reason not in REASONS:
        return f"unknown reason {reason!r}"
    if data["event"] == "unwithdrawn" and reason is not None:
        return f"reason must be null on an unwithdrawn record, got {reason!r}"
    return None


# --------------------------------------------------------------------------
# Reading and appending (the caller holds the ledger lock)
# --------------------------------------------------------------------------


def _read(path: Path) -> dict[str, Withdrawal]:
    """The last record per fingerprint. A missing ledger is an empty state."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {}
    lines = data.split(b"\n")
    if lines.pop():
        print(f"Warning: {path} ends with an unterminated line; ignored", file=sys.stderr)
    state: dict[str, Withdrawal] = {}
    for number, raw in enumerate(lines, start=1):
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            problem: str | None = f"not valid JSON ({exc})"
        else:
            problem = _problem(parsed)
        if problem:
            raise LedgerError(
                f"{path} line {number} is not a valid withdrawal record: {problem}; "
                "fix or remove the line"
            )
        entry = Withdrawal(
            parsed["ts"],
            parsed["event"],
            parsed["source"],
            parsed["session_id"],
            parsed["row"],
            parsed["fingerprint"],
            parsed["reason"],
        )
        state[entry.fingerprint] = entry
    return state


def _append(path: Path, entry: dict) -> None:
    """Append one record: one ``write`` of the whole line, then ``fsync``.

    An unterminated last line (a crash mid-append, already reported by the read that
    precedes every append) is truncated first, so the new record starts a line.
    """
    payload = (json.dumps(entry) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    created = not path.exists()
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            content = os.pread(fd, size, 0)
            os.ftruncate(fd, content.rfind(b"\n") + 1)
        written = os.write(fd, payload)
        if written != len(payload):
            raise OSError(f"short write to {path}: {written} of {len(payload)} bytes")
        os.fsync(fd)
    finally:
        os.close(fd)
    if created:
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------


def load(path: Path) -> dict[str, Withdrawal]:
    """The ledger state, read once under the ledger lock; empty when there is no file."""
    if not path.exists():
        return {}
    with locked(ledger_lock(path)):
        return _read(path)


def is_withdrawn(state: dict[str, Withdrawal], fp: str) -> Withdrawal | None:
    """The record that withdrew ``fp``, or ``None`` when it is not withdrawn."""
    entry = state.get(fp)
    return entry if entry is not None and entry.withdrawn else None


def withdraw(path: Path, fp: str, *, source: str, session_id: str, row: int, reason: str) -> bool:
    """Append a ``withdrawn`` record unless ``fp`` is already withdrawn.

    Returns whether a record was appended. Runs under the ledger lock.
    """
    with locked(ledger_lock(path)):
        if is_withdrawn(_read(path), fp):
            return False
        _append(
            path,
            record(
                "withdrawn",
                fingerprint=fp,
                source=source,
                session_id=session_id,
                row=row,
                reason=reason,
            ),
        )
    return True


@dataclass(frozen=True)
class Unwithdrawal:
    """The ``unwithdrawn`` record a forced addition still owes the ledger.

    :func:`guard` returns it with the ledger lock still held. The caller writes the
    row, then calls :meth:`commit`, then releases the lock. A write that fails or is
    interrupted never reaches :meth:`commit`, so the claim stays withdrawn.
    """

    path: Path
    entry: dict
    lifted_reason: str | None

    def commit(self) -> None:
        """Append the record (the caller holds the ledger lock) and print the warning."""
        _append(self.path, self.entry)
        print(
            f"Warning: adding a withdrawn claim (reason was: {self.lifted_reason}); "
            "recorded as unwithdrawn",
            file=sys.stderr,
        )


def guard(
    config: CairnConfig,
    claim: str,
    *,
    force: bool,
    source: str,
    session_id: str,
    row: int,
    hold: ExitStack,
) -> tuple[bool, Unwithdrawal | None]:
    """Whether a new decision row with ``claim`` may be added, and what it owes.

    A withdrawn claim is refused (the refusal is printed and ``(False, None)``
    returned) unless ``force``. With ``force`` the ledger lock stays entered on
    ``hold`` and an :class:`Unwithdrawal` that names the new row is returned: the
    caller commits it after the row is written, then closes ``hold``. A claim that is
    not withdrawn gives ``(True, None)``. The guard is global: a claim withdrawn in
    one source's session is refused in every source. :class:`EmptyClaim` for an
    empty claim.
    """
    fp = fingerprint(claim)
    path = ledger_path(config)
    if not path.exists():
        return True, None
    with ExitStack() as stack:
        stack.enter_context(locked(ledger_lock(path)))
        entry = is_withdrawn(_read(path), fp)
        if entry is None:
            return True, None
        if not force:
            print(
                f"Error: claim withdrawn on {entry.date} (reason: {entry.reason}, "
                f"{entry.ref} row #{entry.row}); pass --force to add it again",
                file=sys.stderr,
            )
            return False, None
        hold.enter_context(stack.pop_all())
    owed = record(
        "unwithdrawn", fingerprint=fp, source=source, session_id=session_id, row=row, reason=None
    )
    return True, Unwithdrawal(path, owed, entry.reason)


__all__ = [
    "EVENTS",
    "FIELDS",
    "LEDGER_NAME",
    "NORM_VERSION",
    "REASONS",
    "EmptyClaim",
    "LedgerError",
    "Unwithdrawal",
    "Withdrawal",
    "fingerprint",
    "guard",
    "is_withdrawn",
    "ledger_lock",
    "ledger_path",
    "load",
    "normalise_claim",
    "record",
    "withdraw",
]
