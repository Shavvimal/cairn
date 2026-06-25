#!/usr/bin/env python3
"""Sync service documentation from repo docs/ folders into a single collection.

Copies markdown files from configured source directories into service-docs/,
organized by service name with preserved directory structure. Designed for
QMD indexing so all service documentation is searchable from one place.

Usage:
    service-docs export [--clean]
    service-docs list
    service-docs context
"""

import argparse
import shutil
import sys
from pathlib import Path

from .. import __version__
from ..config import get_config
from ..qmd import register_descriptions

# Configuration comes from cairn.config (data, not code).
COLLECTION = "service-docs"
_config = get_config()
_collection_config = _config.collection(COLLECTION)
OUTPUT_DIR = _config.output_dir(COLLECTION)

# Source mapping: service name → docs directory (mirrored into OUTPUT_DIR/<name>/).
SOURCES = {src.name: src.path for src in _collection_config.service_sources}
SERVICE_DESCRIPTIONS = {src.name: src.description for src in _collection_config.service_sources}


# =============================================================================
# Sync Logic
# =============================================================================


def sync_service(name: str, source_dir: Path, quiet: bool = False) -> tuple[int, int, int]:
    """Sync markdown files from a source directory into service-docs/{name}/.

    Returns (copied, unchanged, removed) counts.
    """
    dest_dir = OUTPUT_DIR / name
    dest_dir.mkdir(parents=True, exist_ok=True)

    if not source_dir.is_dir():
        if not quiet:
            print(f"  Warning: source not found: {source_dir}", file=sys.stderr)
        return 0, 0, 0

    # Collect source files (relative paths). rglob("*.md") also matches
    # directories whose name ends in .md (e.g. a "ui-prompt.md/" folder), so
    # skip non-files - only markdown files should sync.
    source_files = {}
    for f in source_dir.rglob("*.md"):
        if not f.is_file():
            continue
        rel = f.relative_to(source_dir)
        source_files[rel] = f

    # Collect existing dest files
    existing_files = {}
    for f in dest_dir.rglob("*.md"):
        if not f.is_file():
            continue
        rel = f.relative_to(dest_dir)
        existing_files[rel] = f

    copied = 0
    unchanged = 0

    # Copy new or changed files
    for rel, src in source_files.items():
        dst = dest_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)

        # Skip if unchanged (same size and content hash via mtime)
        if dst.exists():
            try:
                if (
                    src.stat().st_size == dst.stat().st_size
                    and src.read_bytes() == dst.read_bytes()
                ):
                    unchanged += 1
                    continue
            except OSError:
                pass

        shutil.copy2(src, dst)
        copied += 1

    # Remove files that no longer exist in source
    removed = 0
    for rel, dst in existing_files.items():
        if rel not in source_files:
            dst.unlink()
            removed += 1
            # Clean up empty parent dirs
            parent = dst.parent
            while parent != dest_dir and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent

    return copied, unchanged, removed


# =============================================================================
# Commands
# =============================================================================


def cmd_export(args):
    """Export command - copy docs from all configured sources into the collection."""
    if args.clean and OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
        OUTPUT_DIR.mkdir(parents=True)
        print("Cleaned service-docs/")

    total_copied = 0
    total_unchanged = 0
    total_removed = 0

    for name, source_dir in SOURCES.items():
        print(f"\n  [{name}] {source_dir}")
        copied, unchanged, removed = sync_service(name, source_dir, quiet=args.quiet)
        total_copied += copied
        total_unchanged += unchanged
        total_removed += removed
        if not args.quiet:
            print(f"    {copied} copied, {unchanged} unchanged, {removed} removed")

    print(
        f"\nSynced {total_copied + total_unchanged} files ({total_copied} new/updated) to {OUTPUT_DIR}"
    )
    return 0


def cmd_list(args):
    """List command - show synced documentation files."""
    if not OUTPUT_DIR.exists():
        print("No service docs synced yet. Run: cairn service-docs export")
        return 0

    for name in sorted(SOURCES.keys()):
        service_dir = OUTPUT_DIR / name
        if not service_dir.exists():
            continue
        files = sorted(service_dir.rglob("*.md"))
        print(f"\n  {name}/ ({len(files)} files)")
        for f in files[:10]:
            rel = f.relative_to(service_dir)
            size_kb = f.stat().st_size / 1024
            print(f"    {rel} ({size_kb:.1f} KB)")
        if len(files) > 10:
            print(f"    ... and {len(files) - 10} more")

    total = sum(1 for _ in OUTPUT_DIR.rglob("*.md"))
    print(f"\n  Total: {total} files")
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    """Context command - register QMD context descriptions for service docs.

    Returns 0 only if every registration succeeded, 1 otherwise.
    """
    descriptions = {}
    collection_desc = _collection_config.extra.get("description")
    if collection_desc:
        descriptions[f"qmd://{COLLECTION}"] = collection_desc
    for name, desc in SERVICE_DESCRIPTIONS.items():
        descriptions[f"qmd://{COLLECTION}/{name}"] = desc
    return register_descriptions(descriptions, _config.qmd_binary)


# =============================================================================
# Main
# =============================================================================


def main():
    parser = argparse.ArgumentParser(
        prog="service-docs",
        description="Sync service documentation into a single QMD collection",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # export
    p_export = subparsers.add_parser("export", help="Export docs from all sources")
    p_export.add_argument("--clean", action="store_true", help="Remove all files before exporting")
    p_export.add_argument("-q", "--quiet", action="store_true", help="Suppress per-file output")
    p_export.set_defaults(func=cmd_export)

    # list
    p_list = subparsers.add_parser("list", help="List synced documentation")
    p_list.set_defaults(func=cmd_list)

    # context
    p_context = subparsers.add_parser("context", help="Register QMD context descriptions")
    p_context.set_defaults(func=cmd_context)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
