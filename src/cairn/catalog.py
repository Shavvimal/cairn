"""Derived, stat-validated catalog of one collection's exported Markdown.

The Markdown files are the only truth. This catalog caches each file's parsed
frontmatter, keyed by relative path, and trusts an entry only while the file's live
stat identity ``(mtime_ns, ctime_ns, size, ino)`` matches what was recorded, exactly
as git's index does. Readers refresh with one directory walk plus one ``stat`` per
file (tens of milliseconds for ten thousand files) and re-parse only what changed.
Writers update it in place after their own writes. A missing or corrupt catalog is
just a cold cache and is rebuilt from the files.

Limits, stated honestly: a modification that preserves all four identity fields is
undetectable (the limit git accepts too). The racy guard re-parses any file whose
mtime falls within one second of the snapshot, because a same-size rewrite inside
the same clock tick would otherwise look unchanged. The catalog lives under the
XDG cache directory, never inside the corpus, so a git-tracked data root does not
see it churn. Directory symlinks are not traversed (parity with ``Path.rglob``);
symlinked files are identified by their target's stat so a target change is seen.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat as stat_mod
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .frontmatter import FrontmatterParseError, read_frontmatter_head
from .storage import atomic_write

CATALOG_VERSION = 1
RACY_GUARD_NS = 1_000_000_000


def catalog_path(output_dir: Path) -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    key = hashlib.sha256(str(output_dir.resolve()).encode()).hexdigest()[:16]
    return cache / "cairn" / "catalogs" / f"{key}.json"


@dataclass(slots=True)
class Entry:
    mtime_ns: int
    ctime_ns: int
    size: int
    ino: int
    frontmatter: dict

    def matches(self, st: os.stat_result) -> bool:
        return (
            self.mtime_ns == st.st_mtime_ns
            and self.ctime_ns == st.st_ctime_ns
            and self.size == st.st_size
            and self.ino == st.st_ino
        )


class Catalog:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.entries: dict[str, Entry] = {}
        self.errors: dict[str, str] = {}  # in memory only; such files are retried
        self.snapshot_ns = 0
        self.dirty = False
        self.generation = 0  # bumps on any change to ``entries``; callers key indexes on it
        self.stats = {"walks": 0, "parsed": 0, "reused": 0}

    # ---- paths ----

    def _rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.output_dir.resolve()).as_posix()

    def path_of(self, rel: str) -> Path:
        return self.output_dir / rel

    # ---- refresh ----

    def _walk(self) -> dict[str, os.stat_result]:
        """Stat every ``*.md`` under the collection before anything is read."""
        found: dict[str, os.stat_result] = {}
        root = self.output_dir
        for directory, _dirs, files in os.walk(root, followlinks=False):
            for name in files:
                if not name.endswith(".md"):
                    continue
                full = Path(directory) / name
                try:
                    st = os.stat(full)  # follows a file symlink: identity is the target's
                except OSError:
                    continue  # vanished between listing and stat
                if not stat_mod.S_ISREG(st.st_mode):
                    continue
                found[full.relative_to(root).as_posix()] = st
        return found

    def _load(self) -> tuple[int, dict[str, Entry]]:
        try:
            data = json.loads(catalog_path(self.output_dir).read_text(encoding="utf-8"))
            if data.get("version") != CATALOG_VERSION:
                return 0, {}
            return int(data["snapshot_ns"]), {
                rel: Entry(**entry) for rel, entry in data["entries"].items()
            }
        except (OSError, ValueError, TypeError, KeyError):
            return 0, {}

    def _parse(self, rel: str, st: os.stat_result) -> Entry | None:
        """Build an entry from ``st`` (taken before the read) and the file's frontmatter."""
        self.stats["parsed"] += 1
        try:
            fm = read_frontmatter_head(self.path_of(rel))
        except (OSError, UnicodeError, FrontmatterParseError) as exc:
            self.errors[rel] = str(exc)
            return None
        self.errors.pop(rel, None)
        return Entry(st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino, fm)

    def refresh(self) -> None:
        snapshot_ns = time.time_ns()  # before the walk: anything written later is re-checked
        self.stats["walks"] += 1
        if not self.output_dir.is_dir():
            self.entries, self.errors, self.snapshot_ns = {}, {}, snapshot_ns
            self.generation += 1
            return
        found = self._walk()
        loaded_snapshot, loaded = self._load()
        entries: dict[str, Entry] = {}
        dirty = loaded_snapshot == 0 and bool(found)  # cold cache: worth persisting
        for rel, st in found.items():
            previous = loaded.get(rel)
            if (
                previous is not None
                and previous.matches(st)
                and st.st_mtime_ns < loaded_snapshot - RACY_GUARD_NS
            ):
                entries[rel] = previous
                self.stats["reused"] += 1
                continue
            dirty = True
            entry = self._parse(rel, st)
            if entry is not None:
                entries[rel] = entry
        if set(loaded) - set(entries):
            dirty = True  # files vanished (or became unparseable)
        self.errors = {rel: msg for rel, msg in self.errors.items() if rel in found}
        self.entries = entries
        self.snapshot_ns = snapshot_ns
        self.dirty = self.dirty or dirty
        self.generation += 1

    # ---- lookups ----

    def get(self, rel: str) -> Entry | None:
        """Return the entry for ``rel`` after re-validating it against the live file.

        A vanished file drops the entry. A changed identity re-parses the file (stat
        first, then read) so the returned frontmatter always belongs to the file as it
        is now. ``None`` means there is no readable session file at ``rel``.
        """
        try:
            st = os.stat(self.path_of(rel))
        except OSError:
            if self.entries.pop(rel, None) is not None:
                self.dirty = True
                self.generation += 1
            return None
        entry = self.entries.get(rel)
        if entry is not None and entry.matches(st):
            return entry
        entry = self._parse(rel, st)
        if entry is None:
            self.entries.pop(rel, None)
        else:
            self.entries[rel] = entry
        self.dirty = True
        self.generation += 1
        return entry

    def put(self, path: Path, frontmatter: dict) -> Entry:
        """Write-through after the caller's own atomic write of ``path``.

        Call it while still holding the document lock, so the recorded identity is
        the one produced by that write and not by a concurrent annotation.
        """
        rel = self._rel(path)
        st = os.stat(path)
        entry = Entry(st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino, frontmatter)
        self.entries[rel] = entry
        self.errors.pop(rel, None)
        self.dirty = True
        self.generation += 1
        return entry

    def items(self) -> list[tuple[Path, dict]]:
        """``(path, frontmatter)`` for every catalogued file, sorted by path."""
        return [(self.path_of(rel), self.entries[rel].frontmatter) for rel in sorted(self.entries)]

    # ---- persistence ----

    def save(self) -> None:
        if not self.dirty:
            return
        payload = {
            "version": CATALOG_VERSION,
            "snapshot_ns": self.snapshot_ns,
            "entries": {rel: asdict(entry) for rel, entry in self.entries.items()},
        }
        try:
            atomic_write(catalog_path(self.output_dir), json.dumps(payload))
        except OSError as exc:
            # A cache that cannot be written only costs the next reader a rebuild.
            print(f"Warning: could not save catalog for {self.output_dir}: {exc}", file=sys.stderr)
            return
        self.dirty = False


def load_catalog(output_dir: Path) -> Catalog:
    catalog = Catalog(output_dir)
    catalog.refresh()
    return catalog


__all__ = ["CATALOG_VERSION", "RACY_GUARD_NS", "Catalog", "Entry", "catalog_path", "load_catalog"]
