"""POSIX advisory locks and atomic replacement shared by all store writers.

Lock files are permanent inodes: never unlink them (a waiter may hold the old
inode). The kernel releases flock after a crash, so stale files are harmless.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def lock_path(resource: Path, purpose: str) -> Path:
    """Locate a permanent per-user lock outside the resource's directory.

    Resolve aliases before hashing. Deliberately independent of XDG config/cache
    homes: writers using different profiles must still lock the same resource.
    """
    key = hashlib.sha256(f"{purpose}:{resource.resolve()}".encode()).hexdigest()
    return Path.home() / ".local/state/cairn/locks" / f"{key}.lock"


@contextmanager
def locked(path: Path, *, blocking: bool = True) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def document_lock(path: Path) -> Path:
    return lock_path(path, "document")


class ConcurrentChangeError(RuntimeError):
    """The file changed between the caller's read and the replace; nothing was written."""


def _current_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def atomic_write(
    path: Path,
    text: str,
    *,
    mode: int | None = None,
    follow_symlinks: bool = False,
    check_unchanged: bool = False,
    expected: bytes | None = None,
) -> None:
    """Replace ``path`` with ``text`` (UTF-8) through a temporary file and ``os.replace``.

    The defaults keep the original behaviour: a symlink at ``path`` is replaced by a
    regular file, missing parents are created, and the file mode is the temporary
    file's 0600. ``mode`` sets the final mode before the replace. ``follow_symlinks``
    writes through the resolved target, so a symlink stays a symlink; the target's
    parent must exist. ``check_unchanged`` reads the file again just before the
    replace and raises :class:`ConcurrentChangeError` unless its bytes equal
    ``expected`` (``None`` means the file must not exist).
    """
    if follow_symlinks:
        path = Path(os.path.realpath(path))
        if not path.parent.is_dir():
            raise FileNotFoundError(f"{path.parent} does not exist")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            if mode is not None:
                os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        if check_unchanged and _current_bytes(path) != expected:
            raise ConcurrentChangeError(f"{path} changed while it was being updated")
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
