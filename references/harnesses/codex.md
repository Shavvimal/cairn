# Codex execution

Use the shell execution tool available in this client (for example `exec_command`). For
long-running commands retain the returned session/process handle and poll its exit status
with the matching tool (for example `write_stdin`); inspect bounded log output. Do not use
Claude-only `Bash` background flags or require `Monitor`. Use available file reads/patches.

Collect missing settings with an available user-input tool. Some synchronous input tools
are restricted to Plan mode; use asynchronous input or ordinary conversation otherwise.
Keep required answers pending and continue only independent work; elapsed time is not an
answer or permission.

Use `/skills` or the `$` picker to select the installed `setup`, `cairn`, `recall`, or
`search` skill. Run setup only on explicit request or explicit onboarding. The setup
policy `allow_implicit_invocation: false` is packaged in `agents/openai.yaml`.

Prefer the current thread ID provided by the host. `CODEX_THREAD_ID`/`CODEX_SESSION_ID`
can be inherited from a parent client: do not use them if they conflict with the selected
thread. `CLAUDE_PLUGIN_ROOT` is also supplied by Codex for compatibility and does not
identify Claude as the active harness. For operations, explicitly pass the correct native
ID to `cairn codex note|close|log --session-id ID`.

The authoritative manifest is root `plugin.json`; `.codex-plugin/plugin.json` is a generated
compatibility projection required by the tested CLI 0.160.0. Update with:

```sh
uv tool upgrade cairn
codex plugin marketplace upgrade cairn
codex plugin add cairn@cairn
cairn hook install --harness codex
```

Restart Codex. Review/trust the exact Cairn hook in `/hooks`; installation alone does
not grant trust. `cairn hook status --harness codex` reports the client’s registration/trust state separately from observed events and results. A changed hook may require review again. The explicit hook file is `hooks/codex.json`. `cairn hook install --harness codex`
inspects the actual client: if bundled discovery works it removes any previous fallback;
otherwise it adds only a Cairn-owned user hook and preserves unrelated entries. Tested
0.160.0 and 0.163.0 omit hooks from the portable manifest, so need this fallback.
Run `cairn hook uninstall --harness codex` before removing the Codex plugin; it preserves
the shared launcher/data used by Claude. Disable/removal also gates the fallback at runtime.

SessionEnd runs when the main session ends (normal client shutdown, archive/delete of an
open conversation, or the host's idle timeout). Switching conversations does not immediately
end one. End a real session and check completed sync plus a QMD search for its distinctive
text, without waiting for cron. The three-second callback hands work to a detached process.
