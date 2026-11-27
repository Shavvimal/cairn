"""Images a session carried, saved as files beside its exported document.

A harness keeps an image the user pasted, and one a tool returned, inside its
transcript as base64. The exported markdown is text, so the picture is gone once the
harness deletes the transcript. An adapter hands each image block to an
:class:`ImageSet` while it parses. The set decodes the block, names the file by its
content hash and returns the markdown that takes the place of the harness placeholder.
After the parse the adapter writes the set into the ``images`` folder beside the
document, before the document itself, so a document never links a file that is absent.

The name is the hash. One picture pasted into two sessions of a project is one file, a
re-export writes nothing, and the link does not depend on the document's own name
(which the exporter can still change when two session ids collide).

Stdlib only, and no import of the config: ``cairn.config`` reads the mode names here.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import tempfile
from pathlib import Path

# off: no image is saved and the export is as before. pasted: the images in the user's
# own prompts. all: also the images inside tool results (screenshots, files read).
IMAGE_MODES = ("off", "pasted", "all")
DEFAULT_IMAGE_MODE = "pasted"
IMAGES_DIR = "images"
HASH_CHARS = 16

# The media types the Anthropic API takes in an image block. Another type keeps its
# placeholder: a file with a guessed extension would not open.
_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}


class ImageSet:
    """The decoded images of one parse, keyed by file name."""

    def __init__(self, mode: str) -> None:
        if mode not in IMAGE_MODES:
            raise ValueError(f"image mode must be one of {', '.join(IMAGE_MODES)}, got {mode!r}")
        self.mode = mode
        self._files: dict[str, bytes] = {}

    def __len__(self) -> int:
        return len(self._files)

    def pasted(self, block: dict, paste_id: int | None = None) -> str | None:
        """Markdown for an image in the user's prompt, or ``None`` to keep the placeholder."""
        if self.mode == "off":
            return None
        return self._embed(block, f"Image #{paste_id}" if paste_id is not None else "Image")

    def tool(self, block: dict) -> str | None:
        """Markdown for an image a tool returned; ``None`` unless the mode is ``all``."""
        if self.mode != "all":
            return None
        return self._embed(block, "image")

    def _embed(self, block: dict, label: str) -> str | None:
        source = block.get("source")
        if not isinstance(source, dict) or source.get("type") != "base64":
            return None
        extension = _EXTENSIONS.get(str(source.get("media_type")))
        data = source.get("data")
        if extension is None or not isinstance(data, str):
            return None
        try:
            content = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return None
        if not content:
            return None
        name = f"{hashlib.sha256(content).hexdigest()[:HASH_CHARS]}.{extension}"
        self._files[name] = content
        return f"![{label}](./{IMAGES_DIR}/{name})"

    def write(self, folder: Path) -> int:
        """Write every image not yet in ``folder``; return how many files were written.

        A file that exists has the same name, so the same content: it is left alone,
        and its mtime with it. Each new file goes through a temporary file and
        ``os.replace``, so a reader never sees half an image.
        """
        written = 0
        for name, content in self._files.items():
            target = folder / name
            if target.exists():
                continue
            folder.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=folder)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content)
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            written += 1
        return written


def images_dir_of(document: Path) -> Path:
    """The folder an image link in ``document`` points into."""
    return document.parent / IMAGES_DIR


__all__ = ["DEFAULT_IMAGE_MODE", "IMAGES_DIR", "IMAGE_MODES", "ImageSet", "images_dir_of"]
