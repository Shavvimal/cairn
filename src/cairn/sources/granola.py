#!/usr/bin/env python3
"""Export Granola meetings to markdown.

Thin adapter over :mod:`cairn`. Granola is the meeting source: its body is
a speaker-tagged transcript (not a chat conversation), and it carries an intricate
auth/decryption chain to recover the WorkOS API token. ALL of that auth/crypto/API/
ProseMirror logic is preserved verbatim below - only the shared plumbing
(frontmatter, preserved-fields, My Notes, output paths, mutations) comes from
cairn.

Auth chain (do not regress - see session 41b8c5c7 for the full investigation):
  macOS Keychain "Granola Safe Storage"
    -> PBKDF2-HMAC-SHA1(salt="saltysalt", iter=1003, dklen=16)
    -> AES-128-CBC (IV = 0x20*16) unwrap storage.dek (Electron safeStorage)
    -> AES-256-GCM (nonce[12] + ct + tag[16]) decrypt stored-accounts.json.enc
    -> first account's access_token (WorkOS).
  Fallbacks: supabase.json.enc, then legacy plaintext supabase.json.
  Locked Keychain / missing files / expired token all degrade gracefully (no hard fail).

Usage:
    granola-sessions export (--today | --all | --since DATE) [-q]
    granola-sessions list [--all] [--json]
    granola-sessions note/close/log ...   (shared)
    granola-sessions context
"""

import argparse
import gzip
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import cast

from .. import cli, qmd
from ..config import get_config
from ..frontmatter import (
    head_close,
    head_open,
    title_line,
)
from ..schema import SessionData
from ..timeutil import parse_iso, today_str
from .base import ConversationSource

COLLECTION = "granola-sessions"
_config = get_config()
_collection_config = _config.collection(COLLECTION)
OUTPUT_DIR = _config.output_dir(COLLECTION)
GRANOLA_DIR = _collection_config.require_store()
# Newer Granola encrypts its data. The current WorkOS token lives in these
# Electron-safeStorage + AES-GCM encrypted files; the legacy plaintext
# supabase.json is kept as a fallback. Documents now come from the API (the
# local cache only holds UI state).
ENC_ACCOUNTS_PATH = GRANOLA_DIR / "stored-accounts.json.enc"
ENC_SUPABASE_PATH = GRANOLA_DIR / "supabase.json.enc"
DEK_PATH = GRANOLA_DIR / "storage.dek"
SUPABASE_PATH = GRANOLA_DIR / "supabase.json"  # legacy plaintext fallback
API_BASE = _collection_config.extra.get("api_base", "https://api.granola.ai")


# =============================================================================
# API Client / Auth (verbatim - intricate, do not refactor)
# =============================================================================


def _find_access_token(obj) -> str | None:
    """Recursively locate an ``access_token``, parsing JSON-string values.

    Granola nests the token inside JSON-encoded string fields (``workos_tokens``,
    ``accounts[].tokens``), so plain dict traversal misses it.
    """
    if isinstance(obj, str):
        s = obj.strip()
        if s[:1] in ("{", "["):
            try:
                return _find_access_token(json.loads(s))
            except (json.JSONDecodeError, ValueError):
                return None
        return None
    if isinstance(obj, dict):
        if isinstance(obj.get("access_token"), str):
            return obj["access_token"]
        for v in obj.values():
            tok = _find_access_token(v)
            if tok:
                return tok
    elif isinstance(obj, list):
        for item in obj:
            tok = _find_access_token(item)
            if tok:
                return tok
    return None


def _cryptography_available() -> bool:
    """Whether the optional ``granola`` extra (``cryptography``) is importable."""
    try:
        import cryptography  # noqa: F401

        return True
    except ImportError:
        return False


def _granola_dek() -> bytes | None:
    """Unwrap Granola's 32-byte data-encryption key (DEK).

    storage.dek is an Electron safeStorage blob: the AES key is derived from the
    macOS Keychain item "Granola Safe Storage" (Chromium OSCrypt PBKDF2 scheme),
    used to AES-128-CBC decrypt it; the result is base64 of the real DEK.
    Returns None if the Keychain is locked/denied or crypto is unavailable.
    """
    if not DEK_PATH.exists():
        return None
    # Surface a missing 'cryptography' loudly: without it we silently fall back to the
    # legacy plaintext token, which is usually stale and yields a confusing 401 rather
    # than an obvious "install the extra" signal.
    try:
        import base64
        import hashlib

        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        print(
            "Granola: 'cryptography' is required to decrypt the token store but is not "
            "installed; cannot read the current token. Install the extra "
            "('uv sync --extra granola' or 'pip install cairn[granola]') and re-run.",
            file=sys.stderr,
        )
        return None
    try:
        kc = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", "Granola Safe Storage"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if kc.returncode != 0 or not kc.stdout.strip():
        return None
    try:
        aes_key = hashlib.pbkdf2_hmac("sha1", kc.stdout.strip().encode(), b"saltysalt", 1003, 16)
        blob = DEK_PATH.read_bytes()
        if blob[:3] not in (b"v10", b"v11"):
            return None
        dec = Cipher(algorithms.AES(aes_key), modes.CBC(b"\x20" * 16)).decryptor()
        out = dec.update(blob[3:]) + dec.finalize()
        pad = out[-1]
        raw = out[:-pad] if 1 <= pad <= 16 else out
        return raw if len(raw) in (16, 24, 32) else base64.b64decode(raw)
    except Exception:
        return None


def _decrypt_enc(path: Path, dek: bytes) -> dict | None:
    """AES-256-GCM decrypt one of Granola's .enc files (nonce[12] + ct + tag[16])."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        raw = path.read_bytes()
        return json.loads(AESGCM(dek).decrypt(raw[:12], raw[12:], None))
    except Exception:
        return None


def get_auth_token() -> str | None:
    """Return the current Granola WorkOS access token, or None.

    Tries the encrypted store first (decrypted via the macOS Keychain), then the
    legacy plaintext supabase.json. The token may be expired - the API call is
    what ultimately validates it; we degrade gracefully on 401.
    """
    dek = _granola_dek()
    if dek:
        for path in (ENC_ACCOUNTS_PATH, ENC_SUPABASE_PATH):
            if path.exists():
                data = _decrypt_enc(path, dek)
                if data:
                    tok = _find_access_token(data)
                    if tok:
                        return tok
    # Legacy plaintext fallback.
    if SUPABASE_PATH.exists():
        try:
            return _find_access_token(json.loads(SUPABASE_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValueError, OSError):
            return None
    return None


class GranolaAuthError(RuntimeError):
    """The Granola API rejected the token (HTTP 401/403 - expired or invalid auth).

    Raised by :func:`api_post` so the primary document fetch fails loudly with a
    non-zero exit instead of being mistaken for "no documents". Optional enrichment
    (transcripts) catches it and degrades; the primary fetch lets it propagate.
    """


def api_post(endpoint: str, body: dict, token: str) -> dict | list | None:
    """Make a POST request to the Granola API.

    Returns the parsed JSON, or ``None`` on a transient network error. A 401/403
    raises :class:`GranolaAuthError` - an expired token is a real, actionable failure,
    not an empty result, so it must not be silently swallowed into "0 documents".
    """
    url = f"{API_BASE}{endpoint}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-App-Version": "7.0.0",
            "X-Client-Version": "7.0.0",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        raw = resp.read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return json.loads(raw)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise GranolaAuthError(
                f"Granola API returned {e.code} {e.reason}: the token is expired or invalid. "
                "Sign out and back in to the Granola app to refresh it, then re-run."
            ) from e
        return None
    except (urllib.error.URLError, OSError):
        return None


def fetch_all_documents(token: str) -> list[dict]:
    """Fetch all documents from the API with AI summary panels."""
    all_docs = []
    offset = 0
    limit = 100

    while True:
        result = api_post(
            "/v2/get-documents",
            {
                "limit": limit,
                "offset": offset,
                "include_last_viewed_panel": True,
            },
            token,
        )
        if not isinstance(result, dict) or "docs" not in result:
            break

        docs = result["docs"]
        all_docs.extend(docs)

        if len(docs) < limit:
            break
        offset += limit

    return all_docs


def fetch_transcript(token: str, doc_id: str) -> list[dict]:
    """Fetch transcript segments for a document (best-effort enrichment).

    A transcript is an optional add-on to a meeting's notes/summary, so an auth
    failure here is swallowed (the meeting still exports without it) rather than
    aborting the whole run - the primary document fetch is the auth gate that fails
    loud. Justified §8 degradation: failure mode = per-document auth/permission;
    safe because the core meeting still exports; degraded = no transcript section.
    """
    try:
        result = _fetch_transcript_segments(token, doc_id)
    except GranolaAuthError:
        return []
    if isinstance(result, list):
        return result
    return []


def _fetch_transcript_segments(token: str, doc_id: str) -> dict | list | None:
    return api_post("/v1/get-document-transcript", {"document_id": doc_id}, token)


# =============================================================================
# ProseMirror → Markdown Converter (verbatim)
# =============================================================================


def prosemirror_to_markdown(node: dict | str, depth: int = 0) -> str:
    """Convert a ProseMirror/TipTap JSON node to markdown."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    node_type = node.get("type", "")
    content = node.get("content", [])

    if node_type == "doc":
        parts = [prosemirror_to_markdown(child) for child in content]
        return "\n\n".join(p for p in parts if p)

    if node_type == "heading":
        level = node.get("attrs", {}).get("level", 2)
        text = _inline_text(content)
        return f"{'#' * level} {text}"

    if node_type == "paragraph":
        return _inline_text(content)

    if node_type == "bulletList":
        items = []
        for item in content:
            item_text = _list_item_text(item)
            items.append(f"- {item_text}")
        return "\n".join(items)

    if node_type == "orderedList":
        items = []
        start = node.get("attrs", {}).get("start", 1)
        for i, item in enumerate(content):
            item_text = _list_item_text(item)
            items.append(f"{start + i}. {item_text}")
        return "\n".join(items)

    if node_type == "horizontalRule":
        return "---"

    if node_type == "codeBlock":
        lang = node.get("attrs", {}).get("language", "")
        code = _inline_text(content)
        return f"```{lang}\n{code}\n```"

    if node_type == "blockquote":
        inner = "\n\n".join(prosemirror_to_markdown(child) for child in content)
        return "\n".join(f"> {line}" for line in inner.split("\n"))

    # Fallback: extract text
    return _inline_text(content)


def _inline_text(content: list) -> str:
    """Extract inline text from ProseMirror content nodes."""
    parts = []
    for node in content:
        if node.get("type") == "text":
            text = node.get("text", "")
            marks = node.get("marks", [])
            for mark in marks:
                mt = mark.get("type", "")
                if mt == "bold":
                    text = f"**{text}**"
                elif mt == "italic":
                    text = f"*{text}*"
                elif mt == "code":
                    text = f"`{text}`"
                elif mt == "link":
                    href = mark.get("attrs", {}).get("href", "")
                    text = f"[{text}]({href})"
            parts.append(text)
        elif node.get("type") == "hardBreak":
            parts.append("\n")
        else:
            # Nested nodes
            parts.append(_inline_text(node.get("content", [])))
    return "".join(parts)


def _list_item_text(item: dict) -> str:
    """Extract text from a listItem node."""
    parts = []
    for child in item.get("content", []):
        if child.get("type") == "paragraph":
            parts.append(_inline_text(child.get("content", [])))
        elif child.get("type") in ("bulletList", "orderedList"):
            # Nested list - indent
            nested = prosemirror_to_markdown(child)
            parts.append("\n" + "\n".join("  " + line for line in nested.split("\n")))
        else:
            parts.append(prosemirror_to_markdown(child))
    return "".join(parts)


# =============================================================================
# Transcript Formatting (verbatim)
# =============================================================================


def format_transcript(segments: list[dict]) -> str:
    """Format transcript segments into readable markdown."""
    if not segments:
        return ""

    lines = []
    for seg in segments:
        source = seg.get("source", "")
        text = seg.get("text", "").strip()
        if not text:
            continue

        ts = seg.get("start_timestamp", "")
        speaker = "You" if source == "microphone" else "Participant"

        # Format timestamp as HH:MM:SS
        time_str = ""
        dt = parse_iso(ts)
        if dt:
            time_str = dt.strftime("%H:%M:%S")

        if time_str:
            lines.append(f"[{time_str}] **{speaker}**: {text}")
        else:
            lines.append(f"**{speaker}**: {text}")

    return "\n\n".join(lines)


# =============================================================================
# Data Loading (verbatim)
# =============================================================================


def find_cache_path() -> Path | None:
    """Return the newest cache-v<N>.json, or None. (.enc variants are ignored.)"""
    candidates = []
    for p in GRANOLA_DIR.glob("cache-v*.json"):
        m = re.match(r"cache-v(\d+)\.json$", p.name)
        if m:
            candidates.append((int(m.group(1)), p))
    if not candidates:
        return None
    return max(candidates, key=lambda c: c[0])[1]


def load_cache() -> dict:
    """Load the Granola cache state dict. Returns {} if absent/unreadable.

    Newer Granola keeps documents server-side (the local cache is UI state only),
    so this is metadata-only and may legitimately contain no ``documents``.
    """
    path = find_cache_path()
    if not path:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    state = data.get("cache", {}).get("state")
    return state if isinstance(state, dict) else {}


# =============================================================================
# Meeting Data Extraction (verbatim)
# =============================================================================


def get_attendees(doc: dict) -> list[dict]:
    """Extract attendees with name and email.

    Primary source: doc['people']['attendees'] - has name + email + company.
    Fallback: doc['google_calendar_event']['attendees'] - has email + responseStatus.
    Merges both: prefer people dict (has names), fill gaps from calendar.
    """
    attendees_by_email = {}

    # Primary: people attendees (have names)
    people = doc.get("people") or {}
    for att in people.get("attendees", []):
        email = att.get("email", "")
        if not email:
            continue
        details = att.get("details", {})
        person = details.get("person", {})
        name_info = person.get("name", {})
        name = name_info.get("fullName", "")
        company_info = details.get("company", {})
        company = company_info.get("name", "")
        attendees_by_email[email] = {"name": name, "email": email, "company": company}

    # Fallback: calendar attendees (fill gaps)
    gce = doc.get("google_calendar_event") or {}
    for att in gce.get("attendees", []):
        email = att.get("email", "")
        if not email or att.get("self"):
            continue
        if email not in attendees_by_email:
            attendees_by_email[email] = {"name": "", "email": email, "company": ""}

    return list(attendees_by_email.values())


def get_creator(doc: dict) -> dict | None:
    """Extract creator name and email."""
    people = doc.get("people") or {}
    creator = people.get("creator")
    if not creator:
        return None
    name = creator.get("name", "")
    email = creator.get("email", "")
    return {"name": name, "email": email}


def get_duration(doc: dict) -> int:
    """Get meeting duration in minutes from calendar event. Returns 0 if unavailable."""
    gce = doc.get("google_calendar_event") or {}
    start = gce.get("start", {})
    end = gce.get("end", {})

    start_dt = parse_iso(start.get("dateTime"))
    end_dt = parse_iso(end.get("dateTime"))
    if start_dt is None or end_dt is None:
        return 0
    return max(0, int((end_dt - start_dt).total_seconds() / 60))


def get_meeting_time(doc: dict) -> tuple[str | None, str | None]:
    """Get meeting start time and timezone from calendar event.

    Returns (time_str like "09:00", last_activity ISO string).
    """
    gce = doc.get("google_calendar_event") or {}
    start = gce.get("start", {})
    end = gce.get("end", {})

    start_dt_str = start.get("dateTime")
    start_dt = parse_iso(start_dt_str)
    if start_dt is None:
        return None, None

    time_str = start_dt.strftime("%H:%M")
    end_dt_str = end.get("dateTime")
    last_activity = end_dt_str or start_dt_str
    return time_str, last_activity


def extract_meeting_data(doc: dict, doc_id: str) -> dict | None:
    """Extract structured meeting data from a Granola document.

    Returns None if the meeting should be skipped (deleted, trashed, wrong type).
    """
    # Skip deleted/trashed
    if doc.get("deleted_at"):
        return None
    if doc.get("was_trashed"):
        return None

    # Skip non-meetings
    if doc.get("type") != "meeting":
        return None

    title = (doc.get("title") or "").strip() or "Untitled Meeting"
    created_at = doc.get("created_at", "")
    notes_markdown = doc.get("notes_markdown", "") or ""

    # Parse date from created_at
    date = None
    dt = parse_iso(created_at)
    if dt:
        date = dt.strftime("%Y-%m-%d")
    if not date:
        date = today_str()

    # Calendar-derived fields
    time_str, last_activity = get_meeting_time(doc)
    duration_min = get_duration(doc)

    # If no last_activity from calendar, use updated_at or created_at
    if not last_activity:
        last_activity = doc.get("updated_at") or created_at

    # Use calendar event date if available (more accurate than created_at)
    gce = doc.get("google_calendar_event") or {}
    start = gce.get("start", {})
    start_dt = parse_iso(start.get("dateTime"))
    if start_dt:
        date = start_dt.strftime("%Y-%m-%d")

    # Attendees and creator
    attendees = get_attendees(doc)
    creator = get_creator(doc)

    return {
        "meeting_id": doc_id,
        "date": date,
        "title": title,
        "time": time_str,
        "duration_min": duration_min,
        "attendees": attendees,
        "creator": creator,
        "notes_markdown": notes_markdown.strip(),
        "last_activity": last_activity,
        "created_at": created_at,
    }


def format_attendee(att: dict) -> str:
    """Format attendee for frontmatter: 'Name <email>' or just email."""
    name = att.get("name", "")
    email = att.get("email", "")
    if name and email:
        return f"{name} <{email}>"
    return email or name


# =============================================================================
# Source adapter
# =============================================================================


class GranolaSource(ConversationSource):
    name = "granola-sessions"
    version = "2.0.0"
    output_dir = OUTPUT_DIR
    session_type = "granola-meeting"
    collection = "granola-sessions"
    list_default = "not-done"
    id_field = "meeting_id"
    default_status = "raw"
    preserved_note = "exports"

    # ---- rendering ----
    def render_document(
        self, data: SessionData, existing_fm: dict | None, my_notes: str | None
    ) -> str:
        # Title (granola fallback sentinel differs: "Untitled Meeting")
        title = existing_fm.get("title") if existing_fm else None
        if not title or title == "Untitled Meeting":
            title = data.get("title", "Untitled Meeting")

        head = head_open(self.session_type, data["date"], self.id_field, data["meeting_id"])
        head.append(title_line(title))
        if data.get("time"):
            head.append(f'time: "{data["time"]}"')
        head.append(f"duration_min: {data.get('duration_min', 0)}")

        attendees = data.get("attendees", [])
        if attendees:
            head.append("attendees:")
            for att in attendees:
                head.append(f'  - "{format_attendee(att)}"')
        else:
            head.append("attendees: []")

        creator = data.get("creator")
        if creator:
            head.append(f'creator: "{format_attendee(creator)}"')

        head.extend(head_close(data.get("last_activity")))

        # Body sections between the title and My Notes: Attendees, Summary, Notes.
        body_extra = []
        if attendees:
            body_extra.extend(["## Attendees", ""])
            for att in attendees:
                name = att.get("name", "")
                email = att.get("email", "")
                body_extra.append(f"- {name} ({email})" if name and email else f"- {email or name}")
            body_extra.append("")

        summary_md = data.get("summary_markdown", "")
        if summary_md:
            body_extra.extend(["## Summary", "", summary_md, ""])

        notes = data.get("notes_markdown", "")
        if notes:
            body_extra.extend(["## Notes", "", notes, ""])

        # Transcript comes AFTER My Notes (the shared notes block).
        transcript_md = data.get("transcript_markdown", "")
        body_main = ["## Transcript", "", transcript_md, ""] if transcript_md else []

        return self.assemble_document(
            head, body_extra, title, existing_fm, my_notes, body_main, default_status="raw"
        )

    # ---- export driver ----
    def export_meetings(
        self,
        state: dict,
        token: str | None,
        date_filter: str | None = None,
        quiet: bool = False,
    ) -> int:
        # Documents now come from the API (the local cache is UI-state only on newer
        # Granola). Merge any cache documents with the API documents, API winning.
        docs = dict(state.get("documents", {}))
        api_docs_by_id = {}
        if token:
            if not quiet:
                print("  Fetching documents from API...")
            api_docs = fetch_all_documents(token)
            api_docs_by_id = {d["id"]: d for d in api_docs if d.get("id")}
            docs.update(api_docs_by_id)
            if not quiet:
                print(f"  Got {len(api_docs_by_id)} documents from API")

        if not docs:
            # A 401/403 would have raised GranolaAuthError already, so reaching here
            # with a token means the account genuinely has no documents - a real (if
            # empty) success, not an auth failure.
            if not quiet:
                print("  No Granola documents found - nothing to export.")
            return 0

        # First pass: find meetings that need exporting (date filter + mtime skip)
        meetings_to_export = {}  # doc_id → (doc, data, existing_file)
        skipped = 0
        for doc_id, doc in docs.items():
            data = extract_meeting_data(doc, doc_id)
            if not data:
                skipped += 1
                continue
            if date_filter and data["date"] < date_filter:
                skipped += 1
                continue
            existing_file = self.find_file(doc_id)
            if existing_file and existing_file.exists():
                updated_dt = parse_iso(doc.get("updated_at") or doc.get("created_at", ""))
                if updated_dt:
                    output_mtime = datetime.fromtimestamp(
                        existing_file.stat().st_mtime, tz=updated_dt.tzinfo
                    )
                    if output_mtime >= updated_dt:
                        skipped += 1
                        continue
            meetings_to_export[doc_id] = (doc, data, existing_file)

        if not meetings_to_export:
            if not quiet:
                print(f"  All {skipped} meetings up to date, nothing to export")
            return 0

        exported = 0
        transcript_count = 0

        for doc_id, (_doc, data, existing_file) in meetings_to_export.items():
            # Enrich with API data: AI summary panel
            api_doc = api_docs_by_id.get(doc_id)
            if api_doc:
                panel = api_doc.get("last_viewed_panel")
                if panel and "content" in panel:
                    summary_md = prosemirror_to_markdown(panel["content"])
                    if summary_md.strip():
                        data["summary_markdown"] = summary_md

            # Fetch transcript from API
            if token:
                segments = fetch_transcript(token, doc_id)
                if segments:
                    transcript_md = format_transcript(segments)
                    if transcript_md:
                        data["transcript_markdown"] = transcript_md
                        transcript_count += 1

            output_file = self.export_one(doc_id, cast(SessionData, data), existing_file)
            if output_file is None:
                continue

            exported += 1
            if not quiet:
                has_summary = "summary" if data.get("summary_markdown") else ""
                has_transcript = "transcript" if data.get("transcript_markdown") else ""
                extras = " + ".join(filter(None, [has_summary, has_transcript]))
                extras_str = f" ({extras})" if extras else ""
                print(f"  Exported: {output_file.name}{extras_str}")

        if not quiet and token:
            print(f"  Transcripts fetched: {transcript_count}")

        return exported

    def run_export(self, args: argparse.Namespace) -> int:
        # Fail loud and early on a missing dependency: a modern Granola keeps its token
        # in an encrypted store that needs the optional 'cryptography' extra. Without
        # it we'd silently fall back to the (usually stale) legacy plaintext token and
        # 401 later - so bail up front with an actionable message instead.
        if ENC_ACCOUNTS_PATH.exists() and not _cryptography_available():
            print(
                "Granola export requires the 'cryptography' package to decrypt the token "
                "store. Install the extra: 'uv sync --extra granola' (or "
                "'pip install cairn[granola]'), then re-run.",
                file=sys.stderr,
            )
            return 1

        # Auth token for API access (decrypted from Granola's encrypted store, or
        # legacy plaintext). Documents come from the API.
        token = get_auth_token()

        # Cache is optional now (UI-state only on newer Granola).
        state = load_cache()
        cache_docs = state.get("documents", {})
        if cache_docs:
            print(f"Loaded {len(cache_docs)} documents from local cache")

        # Fail loud and early: an explicit export with no usable token and no cache
        # cannot do its job. A non-zero exit lets `cairn sync` log this source as
        # FAILED (per-source, without blocking the others) instead of a false success.
        if not token and not cache_docs:
            print(
                "Granola export failed: no usable auth token. Decryption needs the "
                "'cryptography' extra (uv sync --extra granola) and an unlocked Keychain; "
                "if both are in place, sign out/in to the Granola app to refresh the token.",
                file=sys.stderr,
            )
            return 1

        # export_meetings filters on a YYYY-MM-DD string; derive it from the shared
        # --since cutoff (a POSIX timestamp). --all disables the filter.
        if args.all:
            date_filter = None
            print("Exporting all meetings...")
        else:
            date_filter = datetime.fromtimestamp(args.since).strftime("%Y-%m-%d")
            print(f"Exporting meetings since {date_filter}...")

        try:
            exported = self.export_meetings(state, token, date_filter, quiet=args.quiet)
        except GranolaAuthError as e:
            print(f"Granola export failed: {e}", file=sys.stderr)
            return 1
        print(f"\nExported {exported} meetings to {OUTPUT_DIR}")
        return 0

    # ---- granola-specific list (meeting format, sorted by date, "Meetings" titles) ----
    def list_sessions(self, show_all: bool) -> tuple[list[tuple[Path, dict]], str]:
        sessions = sorted(self.all_sessions(), key=lambda x: x[1].get("date", ""), reverse=True)
        if show_all:
            return sessions, "All Meetings"
        active = [(f, fm) for f, fm in sessions if fm.get("status") != "done"]
        return active, "Active Meetings"

    def print_listing(self, sessions: list[tuple[Path, dict]], title: str) -> None:
        print(f"\n{title}:")
        print("-" * 80)
        if not sessions:
            print("  No meetings found.")
            return
        for i, (_path, fm) in enumerate(sessions[:20], 1):
            t = fm.get("title", "Untitled")[:40]
            s = fm.get("status", "?")
            d = fm.get("date", "?")
            dur = fm.get("duration_min", "?")
            time_str = fm.get("time", "")
            attendees = fm.get("attendees", [])
            att_count = len(attendees) if isinstance(attendees, list) else 0
            time_part = f" {time_str}" if time_str else ""
            print(f"  {i:2}. [{s:8}] {d}{time_part} ({dur:>3} min, {att_count} people) {t}")
        if len(sessions) > 20:
            print(f"  ... and {len(sessions) - 20} more")

    # ---- granola-specific qmd context (single collection, custom description) ----
    def cmd_context(self, args: argparse.Namespace) -> int:
        # Description is config-overridable (extra.description), else a sensible default.
        description = _collection_config.extra.get(
            "description",
            "Granola meeting transcripts, AI summaries, and notes from recorded meetings",
        )
        return qmd.register_descriptions(
            {f"qmd://{self.collection}": description}, _config.qmd_binary
        )


def main() -> None:
    """Console entry point - drive the Granola source through the shared CLI."""
    cli.run(GranolaSource())


if __name__ == "__main__":
    main()
