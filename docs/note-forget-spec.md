# Withdrawal that sticks (`forget`): implementation spec

Status: implemented in the PR that adds this file. Section 11 lists where the
implementation differs from the text above it and records the answers to the open
questions; where section 11 differs from an earlier section, section 11 wins.

Claims about behaviour before this change cite `path:line` on `main` at 2.9.0, or the
decisions table change (docs/decisions-table-spec.md) where they say so.

**Dependencies.** The `## Decisions` table (docs/decisions-table-spec.md). This change
uses its parser and writer in `src/cairn/decisions.py`: `DecisionRow` (`number`, `claim`
without the strike or the marker, `struck`, `suffix`), `decisions.parse`,
`decisions.apply` / `decisions.mutate_table` (an `edit(rows) -> rows` callback that runs
under the document lock and writes with `atomic_write`), the strike grammar
`~~<claim>~~[ <marker>]` in which any marker the reader does not know is kept verbatim
and classed `struck`, the row id `<source>:<native_id>#<n>`, the verbs `decide` and
`supersede` with `--session-id`, and `cairn recall decisions` with `--include-struck`.

## 1. Purpose and non-goals

A decision row that is wrong must stay gone. Today a user can edit the row away, but the
next `decide` from an agent that read the old transcript adds it back. This change adds:

1. `cairn <source> forget N --reason R` strikes decision row N in place as
   `~~claim~~ withdrawn: R` and appends a fingerprint of the claim to
   `<data_root>/withdrawn.jsonl`.
2. A guard: `decide` and `supersede` refuse a claim whose fingerprint is withdrawn,
   unless `--force`, which records an `unwithdrawn` event.
3. Readers (`cairn recall decisions`, the digest `overdue` list) hide withdrawn rows.

Non-goals:

- Erasing text. The struck claim stays in the session document, in the QMD index, in any
  backup and in the exported transcript. Transcripts are the harness's data, and cairn
  re-exports them from the native store (`src/cairn/sources/base.py:269-328`).
- Purge of history, tombstones or any database.
- Secrecy of the ledger. A SHA-256 of a short claim is open to a dictionary guess. The
  fingerprint keeps claim text out of a file that is easy to share; it does not protect
  a claim from a reader who can guess it.
- `forget` on `## My Notes` prose or on `comments:` (see 4.6).

## 2. Current behaviour (main at 2.9.0)

- Per-source verbs `note`, `close`, `log` are registered in one place
  (`src/cairn/cli.py:98-115`) and dispatch to `ConversationSource.cmd_*`
  (`src/cairn/sources/base.py:476-539`). Each takes `--session-id`; the current session is
  resolved by `_require_session_id` (`base.py:385-394`, `336-383`).
- Usage errors exit 2 (`base.py:481-483`, `504-509`); a failed mutation exits 1
  (`base.py:484`, `536-537`).
- Locks: `lock_path(resource, purpose)` hashes `purpose:resolved-path` into
  `~/.local/state/cairn/locks/<sha256>.lock` (`src/cairn/storage.py:18-25`). `locked()`
  takes an exclusive `flock` on that permanent file (`storage.py:28-36`).
  `atomic_write` writes a temporary file, fsyncs it, renames it and fsyncs the directory
  (`storage.py:43-59`). There is no shared append helper. The routine log appends with a
  plain `open("a")` and no lock (`src/cairn/routine_run.py:130-135`).
- `data_root` is a required config key (`src/cairn/config.py:42-43`) and each collection
  lives at `data_root/<collection>` (`config.py:293-294`). QMD indexes only collection
  directories that hold Markdown (`src/cairn/qmd.py:172-182`), so a JSONL file directly
  under `data_root` is not indexed.
- `comments:` is one frontmatter string of `[YYYY-MM-DD HH:MM] text` lines
  (`mutations.py:107-118`).
- The decisions table change adds `decide`, `supersede` and `recall decisions`. It writes
  only the marker `superseded by #M`. A row struck with any other marker is kept verbatim,
  classed `struck`, and hidden by `recall decisions` unless `--include-struck`. Only an
  active row can be overdue, so the digest `overdue` list never holds a struck row.

## 3. Surface

### 3.1 CLI

| Command | Flags | Exit |
|---|---|---|
| `cairn <source> forget N` | `--reason {stale,wrong_source,hallucination,cross_attribution,private,other}` (required), `--session-id ID`, `--json`, `-q` | 0 struck or already withdrawn, 1 not found, ledger or I/O error, 2 usage |
| `cairn <source> decide CLAIM ...` | adds `--force` | adds 3: refused, claim withdrawn |
| `cairn <source> supersede N CLAIM ...` | adds `--force` | adds 3: refused, replacement claim withdrawn |
| `cairn recall decisions` | `--include-struck` also lists withdrawn rows | unchanged |
| `cairn recall digest` | none | unchanged |

`forget` is registered for every source that has `note`. `--session-id` matches the
existing verbs.

### 3.2 Reason vocabulary

| reason | Use when |
|---|---|
| `stale` | It was true and is no longer. Prefer `supersede` when a replacement exists. |
| `wrong_source` | The source column points at the wrong session, file or person. |
| `hallucination` | No source supports it. |
| `cross_attribution` | True of a different project, repo or person. |
| `private` | It must not be recalled. Text stays on disk (see 4.1 step 8). |
| `other` | None of the above. |

Declared as `REASONS: Final[tuple[str, ...]]` in `src/cairn/withdrawals.py`, the way
`VALID_STATUSES` is declared at the mutation boundary (`mutations.py:27-29`). No free-text
reason: free text could carry the claim into the ledger.

### 3.3 Files

- Ledger: `<data_root>/withdrawn.jsonl`, mode `0600`, created on first append.
- Ledger lock: `lock_path(<data_root>/withdrawn.jsonl, "withdrawals")`.
- No config key, no frontmatter key.

### 3.4 Exact messages

| Case | Stream | Text |
|---|---|---|
| struck | stdout | `Withdrew row #3 in claude:3f2a9c1e (reason: stale)` |
| already struck as withdrawn | stdout | `Row #3 in claude:3f2a9c1e is already withdrawn (reason: stale)` |
| reason `private` | stderr | `Note: withdrawn, not erased: the claim text stays in <path> and in the QMD index until you edit the file.` |
| no table | stderr | `Error: no decisions table in claude:3f2a9c1e` |
| no row | stderr | `Error: no decision row #9 in claude:3f2a9c1e (rows: 1-5)` |
| N < 1 | stderr | `Error: row number must be 1 or greater, got 0` (exit 2) |
| empty claim | stderr | `Error: row #3 has an empty claim after normalisation; edit it by hand` |
| guard refusal | stderr | `Error: claim withdrawn on 2026-10-30 (reason: hallucination, claude:3f2a9c1e row #3); pass --force to add it again` |
| force | stderr | `Warning: adding a withdrawn claim (reason was: hallucination); recorded as unwithdrawn` |
| bad ledger line | stderr | `Error: <path> line 7 is not a valid withdrawal record: <detail>; fix or remove the line` |
| torn tail | stderr | `Warning: <path> ends with an unterminated line; ignored` |

Session references in messages use `<source>:<first 8 of native id>`, as `base.py:497`
does for `close`.

## 4. Behaviour

### 4.1 `forget N --reason R`

1. Validate `N >= 1` (exit 2). argparse validates `--reason` against `REASONS` (exit 2).
2. Resolve the session with `_require_session_id`. Exit 1 if none.
3. Resolve the file with `find_file`; ambiguous or missing exits 1 with the messages of
   `mutations.locate`.
4. Take `locked(document_lock(path))`. Inside it, parse the table.
5. Find the row whose `number == N`. If absent, exit 1.
6. Compute `fp = fingerprint(row.claim)` (section 5.1).
7. If the row is already struck with a `withdrawn:` marker, print the "already
   withdrawn" line and exit 0 with no append. Otherwise take `locked(ledger_lock)`, read
   the ledger state, and append one `withdrawn` record unless `fp` is withdrawn already.
   Release the ledger lock.
8. Strike the row: the claim cell becomes `~~<claim>~~ withdrawn: R`. A row that is
   already struck keeps its marker in front:
   `~~<claim>~~ superseded by #4, withdrawn: R`. Write with `atomic_write`. If
   `R == private`, print the stderr note.
9. Release the document lock and print the result (or the JSON in 5.3).

The ledger record is written before the document, so a crash between steps 7 and 8
leaves the claim withdrawn and the row unstruck. Running `forget` again strikes the row
and appends nothing, because step 7 finds `fp` already withdrawn.

### 4.2 Lock order

Always document lock, then ledger lock. `decide --force` follows the same order. No code
path takes the ledger lock and then a document lock, so the two cannot deadlock.

### 4.3 Ledger append and read

Append, under the ledger lock:

1. Open with `os.open(path, O_RDWR | O_APPEND | O_CREAT, 0o600)`.
2. If the file is non-empty and its last byte is not `\n`, truncate to the byte after the
   last `\n`. Only a crash mid-append leaves such a tail, and the read that precedes every
   append has already printed the torn-tail warning.
3. Write the whole record plus `\n` in one `os.write`, then `os.fsync`. A new file also
   gets an fsync of its directory.

Read, under the ledger lock: parse each terminated line. A line that is not a JSON object,
lacks a field from 5.2, or has an unknown `v`, `event`, `reason` or `norm`, a bad `ts`,
`row` or `fingerprint`, fails loudly (exit 1). An unterminated last line is ignored with
the warning. The state per fingerprint is the last event in file order: `withdrawn`
means withdrawn, `unwithdrawn` means not. A missing ledger is an empty state.

### 4.4 The guard

`withdrawals.guard(config, claim, *, force, source, session_id, row) -> bool`, called by
`decide` and `supersede` under the document lock, before the new row is written:

1. `fp = fingerprint(claim)`. A claim that normalises to nothing (`"!!!"`) skips the guard:
   no record can hold its fingerprint.
2. Read the state. If `fp` is not withdrawn, return True.
3. If withdrawn and not `force`: print the refusal and return False; the verb exits 3 and
   writes nothing (a refused `supersede` does not strike row N either).
4. If withdrawn and `force`: print the warning, append an `unwithdrawn` record that names
   the new row (`source`, `session_id`, `row`, `reason: null`), return True.

`decide --force` on a claim that is not withdrawn adds the row and appends nothing.

The guard is global across sources and sessions: a claim withdrawn in a Claude session is
refused in a Codex session too. The claim describes the user's work, not the harness.

### 4.5 Reading

`cairn recall decisions` and the digest mark each row withdrawn when either holds:

- `row` evidence: the claim cell is struck and its marker ends in `withdrawn: <reason>`.
- `ledger` evidence: the row is not struck, but its fingerprint is withdrawn in the
  ledger (the claim was added in another session before the withdrawal, or by hand, or
  the strike was edited away).

`recall decisions` drops withdrawn rows by default, as it drops struck rows.
`--include-struck` keeps both. In the text output a row with `ledger` evidence ends in
`  [withdrawn: <reason>]`; a struck row already shows `~~claim~~ [<marker>]`. A withdrawn
row is never overdue. `--json` rows always carry the three fields in 5.4. The ledger is
read once per command.

The digest `overdue` list leaves out every withdrawn row. Superseded rows follow the
decisions table rule; this change does not alter it.

### 4.6 My Notes prose and comments: out of scope

`forget` acts on decision rows only. Reasons:

- A `comments:` entry is a timestamped prose line in one frontmatter string
  (`mutations.py:107-118`). It has no stable row number, and the next comment changes the
  positions.
- `## My Notes` is free Markdown that cairn copies as one block and never parses
  (`frontmatter.py:189-236`).
- A fingerprint guards an exact claim. Prose is rarely re-added word for word, so a guard
  on prose would refuse almost nothing and give false assurance.
- The user can already edit a note by hand. A wrong claim that matters belongs in the
  decisions table, where `forget` can withdraw it.

## 5. Data formats

### 5.1 Normalisation and fingerprint

```
NORM_VERSION = 1
def normalise_claim(claim: str) -> str:
    text = unicodedata.normalize("NFKC", claim)
    text = text.casefold()
    text = "".join(
        ch for ch in text
        if not unicodedata.category(ch).startswith("P")   # all Unicode punctuation
        and ch not in "`~"                                 # Markdown code and strike marks
    )
    return " ".join(text.split())                          # collapse and strip whitespace

def fingerprint(claim: str) -> str:
    norm = normalise_claim(claim)
    if not norm: raise EmptyClaim("empty claim after normalisation")   # a ValueError
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()
```

Punctuation is deleted, not replaced with a space, so `rate-limit` and `ratelimit` match.
Symbols stay (`C++`, `<`, `|`). `%` is punctuation in Unicode (category Po), so it is
deleted. Input is the logical claim (`DecisionRow.claim`), never the raw cell.

| Input | Normalised | sha256 |
|---|---|---|
| `Use Postgres 16 for the queue.` | `use postgres 16 for the queue` | `36e5348b0c921d29e9974b20311175686f38dba3b3bc0890f976264f2a20ece5` |
| `  use  POSTGRES 16, for the queue!! ` | `use postgres 16 for the queue` | `36e5348b0c921d29e9974b20311175686f38dba3b3bc0890f976264f2a20ece5` |
| `The API's rate-limit is 100 req/s` | `the apis ratelimit is 100 reqs` | `e84d7d5a98c5d452bdd0e621c85c5c8f93205a375b7679cb12f5337aa51ff067` |
| `` `cairn sync` runs every 15 minutes `` | `cairn sync runs every 15 minutes` | `185bfafd13790869e0095dd0a230e4dfbe847eb098c44a50c4a22cd4509d44a1` |

Full-width `Ｕｓｅ` folds to `use` under NFKC. The tests pin these four hashes.

### 5.2 Ledger row (`withdrawn.jsonl`, one JSON object per line, keys in this order)

```json
{"v": 1, "ts": "2026-10-30T14:02:11Z", "event": "withdrawn", "source": "claude",
 "session_id": "3f2a9c1e-0000-4000-8000-000000000001", "row": 3,
 "fingerprint": "36e5348b...20ece5", "norm": 1, "reason": "stale"}
```

| Key | Type | Notes |
|---|---|---|
| `v` | int | record schema, 1 |
| `ts` | str | UTC, second precision, `Z` suffix |
| `event` | `"withdrawn"` or `"unwithdrawn"` | |
| `source` | str | registry source verb |
| `session_id` | str | full native id |
| `row` | int | `#` value in that session's table |
| `fingerprint` | str | 64 lowercase hex |
| `norm` | int | `NORM_VERSION`; a later normalisation change can read old rows |
| `reason` | str or null | one of `REASONS`; null on `unwithdrawn` |

No claim text, title, path or cwd is stored.

### 5.3 `forget --json` (stdout)

```json
{"ref": "claude:3f2a9c1e-...", "row": 3, "reason": "stale", "fingerprint": "36e5...",
 "path": "~/.../2026-10-30-3f2a9c1e.md", "ledger": "<data_root>/withdrawn.jsonl",
 "already_withdrawn": false, "appended": true}
```

### 5.4 Fields added to each `recall decisions --json` and digest `overdue` row

`"withdrawn": bool`, `"withdrawn_reason": str | null`, `"withdrawn_via": "row" | "ledger" | null`.
`overdue` is false for a withdrawn row.

## 6. Failure modes

| Failure | Result |
|---|---|
| Crash after ledger append, before document write | Claim withdrawn, row unstruck; re-run `forget` repairs it with no second append |
| Crash mid-append | Torn tail; next reader warns and ignores it; next appender truncates it |
| Hand-edited ledger with a bad line | Every guard and reader exits 1 with the line number; nothing is silently allowed |
| Ledger deleted | All withdrawals lifted; struck rows stay hidden through `row` evidence |
| User edits the strike away by hand | Row shows again only if the ledger has an `unwithdrawn` for it; otherwise `ledger` evidence hides it |
| Two `forget` calls on one document | Serialised by the document lock |
| `forget` in two documents at once | Serialised at the append by the ledger lock; each line is whole |
| `data_root` not writable | `Error: <OSError text>`, exit 1, document unchanged (ledger first) |
| Claim edited after withdrawal | New text, new fingerprint, not refused. Documented limit |

## 7. Tests

`tests/test_withdrawals.py`, in a temp home (`StoreFixture`, `tests/test_shared_store.py`):

- `TestFingerprint.test_normalise_claim_examples`: the four rows of 5.1, normalised text and hash, and full-width input.
- `TestFingerprint.test_normalise_keeps_symbols`: `C++` and `C` differ; `<`, `|` stay, `%` goes.
- `TestFingerprint.test_fingerprint_rejects_empty_claim`: `"!!! ..."` raises `ValueError`.
- `TestForget.test_forget_strikes_row_and_appends_one_record`: cell text, exact record, key order.
- `TestForget.test_ledger_record_has_no_claim_text`: the claim is absent from the file bytes.
- `TestForget.test_ledger_mode_is_0600`.
- `TestForget.test_forget_twice_appends_once`: the second run prints "already withdrawn", exit 0.
- `TestForget.test_forget_repairs_unstruck_row_after_crash`: pre-seeded ledger, no new line.
- `TestForget.test_forget_superseded_row_keeps_supersede_suffix`.
- `TestForget.test_forget_unknown_row_exits_1`: also the "no decisions table" message.
- `TestForget.test_forget_row_zero_exits_2` and `test_forget_reason_outside_vocabulary_exits_2`.
- `TestForget.test_forget_private_prints_not_erased_note`.
- `TestForget.test_forget_empty_claim_after_normalisation_exits_1`, `test_forget_json`.
- `TestForget.test_unwritable_data_root_leaves_document_unchanged`.
- `TestGuard.test_decide_refuses_withdrawn_claim_exit_3`: exact message; `supersede` refused too.
- `TestGuard.test_decide_refuses_case_and_punctuation_variant`.
- `TestGuard.test_decide_refuses_across_sources`: withdrawn in claude, refused in codex.
- `TestGuard.test_decide_force_appends_unwithdrawn_and_adds_row`.
- `TestGuard.test_withdraw_after_unwithdraw_refuses_again`: the last event wins.
- `TestReading.test_recall_decisions_hides_withdrawn_by_default`.
- `TestReading.test_recall_decisions_include_withdrawn_marks_rows` (through `--include-struck`).
- `TestReading.test_recall_decisions_json_fields`.
- `TestReading.test_unstruck_row_hidden_by_ledger_evidence`.
- `TestReading.test_digest_hides_withdrawn_rows_by_default`.
- `TestReading.test_bad_ledger_line_fails_loud_with_line_number`.
- `TestReading.test_torn_tail_is_ignored_then_truncated_on_append`.
- `TestProcesses.test_strike_survives_reexport`: export, forget, export again, row still struck.
- `TestProcesses.test_concurrent_forgets_write_whole_lines`: 8 parallel
  `python -m cairn codex forget` across 8 documents; 8 parseable lines.

`tests/test_storage.py::TestExternalLocks.test_export_annotation_and_index_leave_no_lock_sidecars`
also runs `decide` and `forget` and asserts that no lock file lands beside the ledger.

## 8. Docs to update

- `docs/note-forget-spec.md`: this spec.
- `skills/cairn/workflows/log-session.md`: a "Withdraw a claim" section with the reason
  table and the `--force` rule.
- `skills/cairn/SKILL.md`: `forget` in the verb table, the service-docs exclusion and the
  description.
- `skills/cairn/reference/{claude,codex,cursor,granola,antigravity}.md`: `forget` in each
  list of shared verbs.
- `skills/recall/SKILL.md`: withdrawn rows are hidden; the three JSON fields.
- `src/cairn/__main__.py`: `forget` in the shared per-source command list.
- `README.md`: the verb list, an example, and one paragraph on what withdrawal does not erase.

## 9. Implementation plan

1. `src/cairn/withdrawals.py` (new): `REASONS`, `NORM_VERSION`, `normalise_claim`,
   `fingerprint`, `ledger_path`, `ledger_lock`, `Withdrawal`, `load`, `withdraw`,
   `guard`, and the locked read and append.
2. `src/cairn/decisions.py`: `WITHDRAWN_RE`, `DecisionRow.withdrawn_reason`,
   `strike_withdrawn`, `cmd_forget`, the guard in `cmd_decide`, `withdrawal(row, state)`
   and the reader changes.
3. `src/cairn/sources/base.py`: `cmd_forget`, beside `cmd_decide`.
4. `src/cairn/cli.py`: register `forget` and `--force`.
5. `src/cairn/digest.py`: one ledger read for the `overdue` list.
6. Tests in section 7, then the docs in section 8. Run `make check`.

## 10. Open questions

1. **Guard scope: global or per source?** Recommended default: global (4.4). A per-source
   scope lets a hallucination withdrawn in Claude come back through Codex.
2. **Should `decide --force` need a reason?** Recommended default: no. The `unwithdrawn`
   record names the session and row, which is enough to audit.
3. **Should `forget` accept a claim prefix as well as a row number?** Recommended default:
   no. A row number is exact, and a prefix match can strike the wrong row.

## 11. Implementation notes and answers

Answers to the open questions: all three recommended defaults. The guard is global,
`--force` takes no reason, and `forget` takes a row number only.

Where the implementation differs from the first draft of this spec:

- **One reader flag, not two.** The draft added `--include-withdrawn` to
  `recall decisions` and to `recall digest`. `recall decisions` already had
  `--include-struck`, and a withdrawn row is a struck row (or, with `ledger` evidence, a
  row cairn treats as struck). A second flag would make a reader choose between two kinds
  of hidden row for no gain, because the JSON fields `state`, `marker`, `withdrawn` and
  `withdrawn_via` already tell them apart. So `--include-struck` shows both, and the
  digest gets no flag: its `overdue` list never held a struck row and now never holds a
  withdrawn one.
- **The guard covers `supersede`.** The replacement claim of `supersede` passes the guard
  as `decide`'s claim does, and `supersede` gains `--force` too.
- **Where the guard runs.** The guard runs inside the `edit` callback of
  `decisions.apply`, under the document lock, because the `unwithdrawn` record names the
  new row number and the full native id. `apply` gained an `on_read(native_id)` callback
  for that. A refusal raises `decisions.ClaimWithdrawn`, which is not a `DecisionError`,
  so it leaves `apply` with nothing written and `cmd_decide` exits 3.
- **`forget` in `decisions.py`.** `cmd_forget` lives beside `cmd_decide`, and the
  `withdrawals` module holds only the ledger. So `withdrawals` never imports `decisions`.
- **Already withdrawn first.** Step 7 checks the row marker before it takes the ledger
  lock, so a row struck as withdrawn by hand reports "already withdrawn" even if its claim
  normalises to nothing.
- **A document without `## My Notes`.** `forget` prints the message of the decisions table
  writer (`Error: <file> has no ## My Notes section; re-export it with cairn <source>
  export --all`), not "no decisions table". Such a file is a legacy export.
- **`%` is punctuation.** The draft listed `%` among the symbols that stay. Unicode puts it
  in category Po, so the normalisation above deletes it. The pseudocode is the contract;
  the sentence was wrong.
- **The marker grammar.** `WITHDRAWN_RE` is `(?:^|, )withdrawn: (?P<reason>[a-z_]+)$`,
  searched in the marker. A hand-written `withdrawn: no longer true` does not match, so it
  stays an ordinary struck row with no `withdrawn` evidence.
- **Ledger validation.** Beyond a missing field and an unknown `event` or `reason`, a
  reader also rejects an unknown `v` or `norm`, a `ts` that is not
  `YYYY-MM-DDTHH:MM:SSZ`, a `row` below 1 and a `fingerprint` that is not 64 lowercase hex
  characters. Extra keys are ignored.
- **Digest determinism.** The `overdue` list now depends on the documents and the ledger.
