# Pasted images: save the images of a Claude session beside its document

Status: implemented in this PR (2.23.0). Section 8 lists what is left out.

Shapes and counts were read on 2026-11-27 from one developer's live Claude Code store
(transcripts written by Claude Code 2.1.331 and 2.1.332). Every count below is from
that one machine and its last 30 days.

## 1. Purpose and non-goals

### Purpose

A user pastes a screenshot into a Claude Code prompt and later wants it back, or wants
an agent that recalls the session to see it. Before this change the export kept the
text `[Image #4]` and dropped the picture. Claude Code deletes a transcript after its
retention period, and then the picture is gone.

This change makes the Claude exporter write each image to a file under `data_root` and
link it from the exported turn. An agent that finds the session through QMD, rg or
`cairn recall` follows the link and reads the file.

### Non-goals

- No hook. Section 3 says why.
- No search inside images. QMD registers a collection with the pattern `**/*.md`, so
  it does not index the files, and nothing here describes or transcribes them.
- No other harness. No Codex rollout on the machine holds an image, and the
  Antigravity store was not examined.
- No removal. cairn never deletes an image file.

## 2. What Claude Code writes

| Fact | Evidence |
|---|---|
| A pasted image is an Anthropic image block, `{"type": "image", "source": {"type": "base64", "media_type": ..., "data": ...}}`, in the `content` list of the `user` record, after the text block. | 32 of 32 human `user` records with an image |
| The record carries `imagePasteIds`, one integer per image block, in block order. The text holds `[Image #N]` where the user pasted. | In all 32 records the ids equal the placeholders, in number and in value |
| Media types are `image/png` and `image/jpeg` only. | 26 png, 28 jpeg in prompts; 30 png, 217 jpeg in tool results |
| A tool result holds an image as the same block inside the `content` list of its `tool_result`. | 247 blocks |
| Claude Code also writes the picture to a session temporary folder and tells the model its path in a hidden `isMeta` record. The folder is outside the store and does not last. | one session examined |
| `api-request-blob` records repeat images. The parent parse reads them as non-messages; the child parse skips them before decoding. | 13 records |
| An `isMeta` record can hold an image (a skill invocation with a paste). It has no `imagePasteIds`. | 1 record |
| A prompt sent while Claude works is an `attachment` record of type `queued_command`, with the same `prompt` list and `imagePasteIds`. | 76 human prompts, 5 with an image |

## 3. Why the exporter and not a hook

A `UserPromptSubmit` hook could copy the picture from the temporary folder at paste
time. It was rejected:

- The hook input has the session id, the working directory and the prompt text, but not
  the image. The hook would have to build the temporary path, which Claude Code does
  not document.
- Whether Claude Code writes the temporary file before or after the hook runs was not
  established.
- The transcript already holds the bytes, and three paths already parse it: the
  SessionEnd hook worker, the scheduled sync and `cairn claude export`.

The cost: the image reaches `data_root` when the session exports, not at the paste.

## 4. Design

### 4.1 Files

```text
<data_root>/claude-code-sessions/<project>/YYYY-MM-DD-<short>.md            session document
<data_root>/claude-code-sessions/<project>/images/<hash>.<ext>              its images
<data_root>/claude-code-sessions/<project>/YYYY-MM-DD-<short>/agent-<id>.md subagent document
<data_root>/claude-code-sessions/<project>/YYYY-MM-DD-<short>/images/<hash>.<ext>
```

The rule is one line: the images of a document are in the `images` folder beside it.
`<hash>` is the first 16 hex characters of the SHA-256 of the decoded bytes. `<ext>` is
`png`, `jpg`, `gif` or `webp`, from the media type.

Rejected: a folder per session (`<short>/images/`). The exporter can rename a document
when two session ids share their first 8 characters on one day, and the existing
subagent index is rendered twice for that case. A link that does not contain the
document's name needs no second render. The content hash also makes one picture
pasted into two sessions one file.

Rejected: the paste number in the file name. It is unique in a session only, and the
link text already carries it.

### 4.2 The exported turn

`[Image #4]` becomes `![Image #4](./images/<hash>.png)` at the same place in the text.
The link is relative to the document. When `imagePasteIds` is absent or does not match
the image blocks, or the text has no placeholder, the image is still saved and its
link, `![Image](...)` or `![Image #N](...)`, follows the text after a blank line. A
prompt that is only a picture now renders a `### User` turn. The title and
`user_messages` keep the text as typed.

With `images: all`, the `[image]` line of a tool result becomes
`![image](./images/<hash>.jpg)`.

A block that is not base64, has another media type, does not decode or is empty keeps
its placeholder and writes no file. The export does not fail.

An `isMeta` record renders no text today, and its images are not saved.

### 4.3 Order of writes

The parse collects the decoded images in an `ImageSet` (`src/cairn/images.py`). The
adapter writes the set before it writes the document, so a document never links a
missing file. A file that exists is not written again and keeps its mtime. Each new
file goes through a temporary file and `os.replace`.

### 4.4 Configuration

`collections.claude-code-sessions.images`: `off`, `pasted` or `all`. Absent means
`pasted`. Another value is a `ConfigError`. The key on any other collection is a
`ConfigError` with code `CONFIG_UNKNOWN_KEY`, so the config cannot claim a setting
that the exporter does not read. `cairn config set claude.images all` writes it, and
`cairn config show` prints it for the collection.

Default `pasted`, not `all`: 36 prompt images against 80 unique tool-result images in
30 days, and a tool-result image is a copy of a file or a screen that the agent
produced, not something the user gave.

### 4.5 When a document re-renders

A Claude document is current when its `source_revision` equals the revision of the
transcript. The revision now also hashes `images=<mode>`, except for `off`. So:

- With `off`, every revision is the one cairn wrote before this change, and no document
  re-renders.
- The first sync after the upgrade (default `pasted`) renders the Claude sessions in
  its window once more.
- A changed mode re-renders each session at its next export.
- `cairn claude export --all` adds the images of every session whose transcript still
  exists.

## 5. Size

116 unique images in 30 days decode to 31.7 MB; the 36 pasted ones are a part of that.
The `data_root` on that machine holds 3.7 GB.

## 6. Skills

The recall and search skills gain one step each: a session document can link an image,
the link is relative to the document, QMD does not index the file, and the agent reads
it with the host's file tool only when the answer needs the picture. Neither skill adds
`Read` to `allowed-tools`: the grant would approve every file read while the skill is
active, so the host asks once instead. `skills/cairn/reference/claude.md` gains an
`Images` section with the layout and the config key.

## 7. Tests

`tests/test_claude_images.py`: the `ImageSet` (name, modes, blocks that cannot be
saved, idempotent write), the export through the real CLI (placeholder replaced, block
order, images without a placeholder, meta records, the three modes, a re-render on a
changed mode, a subagent's images beside its document), and the config key (default,
refusals, `config set`, `config show`).

## 8. Left out

- **Prompts sent while Claude works.** The exporter does not render `queued_command`
  attachments, so those prompts, with or without an image, are absent from every export.
  This is older than this change: 76 human prompts in 30 days on the machine, 5 with an
  image. It needs its own change, because it adds user turns to documents and moves
  `attachment` out of the ignored record types.
- **Codex, Antigravity, Cursor.** Each needs its own shape check.
- **Cleanup.** A file stays when its session document is deleted or the mode goes to
  `off`.
- **Spec target.** `SPEC_TARGETS["claude"]` keeps its date: only the image shapes were
  checked on 2026-11-27, not every record type.
