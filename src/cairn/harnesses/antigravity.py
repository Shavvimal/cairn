"""Google Antigravity CLI (``agy``) as a ``cairn invoke`` peer.

Verified live against agy 1.4.6 (2026-10-23). Headless mode is ``-p=<prompt>`` with
``--output-format stream-json``; the prompt must be attached to the flag with ``=``,
because a bare ``-p`` takes the next argument as its value. The stream is NDJSON
with an ``event`` field: ``init`` (carries the ``conversation_id``), ``step_update``
(one line per state change of a ``user_input``, ``agent_response``, ``tool``,
``system_message`` or ``checkpoint`` step; an ``agent_response`` arrives as many
``ACTIVE`` ``text_delta`` fragments and one ``DONE``) and ``result`` (``status``
``SUCCESS`` | ``ERROR`` | ``CANCELED`` | ``INTERRUPTED`` | ``INVALID`` | ``WAITING``
| ``RUNNING`` plus the full ``response``). A rejected request (unknown model, not
logged in) prints only a ``result`` with ``status: ERROR`` and exits 1.

A conversation is continued with ``--conversation <id>``; agy has no fork. A prompt
too large for the command line goes through ``--input-format stream-json``: one
NDJSON ``user`` message on stdin, then EOF, which ends the session after the turn.

Conversations are exported by :mod:`cairn.sources.antigravity` from the JSONL
transcript agy writes per conversation under
``<app data>/brain/<id>/.system_generated/logs/`` (``transcript_full.jsonl``, with
``transcript.jsonl`` as the compact copy). The hook side is agy's ``Stop`` event
(``~/.gemini/config/hooks.json``), whose camelCase payload :meth:`Antigravity.decode`
maps onto :class:`SessionEvent`; see :mod:`cairn.harnesses.antigravity_registration`.

Billing (:class:`cairn.config.BillingPolicy`): agy bills either the consumer plan
quota or, with ``AGY_ADC_AUTH=true`` and Application Default Credentials, Vertex AI
on a GCP quota project (``GOOGLE_CLOUD_QUOTA_PROJECT``, else the ADC file's
``quota_project_id``). The route is decided by the environment the process starts
with, so cairn sets it per run, refuses a ``gcp`` run whose ADC file or quota
project is missing (agy would fail with ``authentication required`` after the
spawn), and reads the route agy actually took from its ``--log-file``:
``applyAuthResult: ... authMethod=adc, quotaProject=<id>`` and the
``aiplatform.googleapis.com/v1/projects/<id>/`` request URLs (verified live).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from ..config import BillingPolicy
from . import Event, InvokeRequest, SessionEvent

# Shared profile -> agy flags. ``plan`` keeps workspace writes and shell commands
# deferred until a human approves the plan (verified live: the file was not created),
# so it is the read-only review mode. ``accept-edits`` applies file writes without
# asking; shell commands stay soft-denied unless ``permissions.allow`` grants them.
# ``full`` auto-approves every tool call.
MODE_BY_PROFILE: dict[str, tuple[str, ...]] = {
    "review": ("--mode", "plan"),
    "edit": ("--mode", "accept-edits"),
    "full": ("--dangerously-skip-permissions",),
}
ANTIGRAVITY_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
ANTIGRAVITY_MODES: tuple[str, ...] = ("plan", "accept-edits", "auto")
_SUCCESS = "SUCCESS"

ADC_AUTH_ENV = "AGY_ADC_AUTH"
ADC_FILE_ENV = "GOOGLE_APPLICATION_CREDENTIALS"
QUOTA_PROJECT_ENV = "GOOGLE_CLOUD_QUOTA_PROJECT"
LOCATION_ENV = "GOOGLE_CLOUD_LOCATION"
ADC_LOGIN_HINT = (
    "run 'gcloud auth application-default login --project <PROJECT>' and tick the "
    "cloud-platform scope on the consent page"
)
_AUTH_LINE = re.compile(r"applyAuthResult: .*?authMethod=(\w+), quotaProject=(\S*)")
_VERTEX_URL = re.compile(r"https://aiplatform\.googleapis\.com/v1/projects/([^/\s]+)/")


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


TRANSCRIPT_NAMES = ("transcript_full.jsonl", "transcript.jsonl")
SETTINGS_NAME = "settings.json"
# Headless agy cannot prompt, so a shell command without an allow rule in
# ``<app data>/settings.json`` is soft-denied and the run goes on without it; a
# review that cannot grep or read the diff ends early with nothing. These are the
# read-only commands a review profile needs, then the test runners a test gate needs.
# A compound command (``a && b``, pipes) runs only when every segment is allowed, so
# the plain helpers agents chain in (echo, head, wc) are listed too.
READ_ONLY_COMMANDS: tuple[str, ...] = (
    "rg",
    "grep",
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "find",
    "tree",
    "stat",
    "file",
    "diff",
    "which",
    "pwd",
    "sort",
    "uniq",
    "cut",
    "tr",
    "echo",
    "printf",
    "basename",
    "dirname",
    "realpath",
    "jq",
    "regex:sed -n .*",
    "git status",
    "git diff",
    "git log",
    "git show",
    "git grep",
    "git blame",
    "git branch",
    "git rev-parse",
    "git ls-files",
)
TEST_COMMANDS: tuple[str, ...] = (
    "pytest",
    "ruff",
    "mypy",
    "uv run pytest",
    "uv run ruff",
    "uv run mypy",
    "make test",
    "make lint",
    "make typecheck",
    "make check",
    "npm test",
    "pnpm test",
)
DEFAULT_APP_DATA = Path.home() / ".gemini" / "antigravity-cli"
STOP_EVENT = "Stop"


def transcript_logs_dir(store: Path, conversation_id: str) -> Path:
    """Where agy keeps a conversation's transcripts under its app-data ``store``."""
    return store / "brain" / conversation_id / ".system_generated" / "logs"


def conversation_transcript(store: Path, conversation_id: str) -> Path | None:
    """The untruncated transcript, else the compact one, else ``None``."""
    logs = transcript_logs_dir(store, conversation_id)
    for name in TRANSCRIPT_NAMES:
        candidate = logs / name
        if candidate.is_file():
            return candidate
    return None


def transcript_conversation_id(transcript: Path) -> str | None:
    """The conversation a transcript path belongs to (its ``brain/<id>/`` folder)."""
    parts = transcript.parts
    if (
        len(parts) >= 5
        and parts[-1] in TRANSCRIPT_NAMES
        and parts[-2] == "logs"
        and parts[-3] == ".system_generated"
        and parts[-5] == "brain"
    ):
        return parts[-4]
    return None


class Antigravity:
    """The ``agy`` adapter. One instance parses one stream: ``agent_response`` deltas
    are buffered per step until that step is ``DONE`` and emitted as one message."""

    name = "antigravity"
    executable = "agy"
    manifest_paths: tuple[str, ...] = ()  # hooks are registered by cairn, not a plugin
    update_command = "agy update"

    def __init__(self) -> None:
        self._text: dict[int, list[str]] = {}
        self._tools_started: set[int] = set()

    # ---- host side (not integrated yet) ---------------------------------------

    def decode(self, payload: object) -> SessionEvent:
        """Map agy's ``Stop`` hook payload (camelCase protojson) onto a session event.

        ``conversationId`` is the session, ``transcriptPath`` the compact transcript,
        and the first ``workspacePaths`` entry the cwd. agy fires ``Stop`` at the end
        of every turn, so one conversation produces many events; the export is
        idempotent, so that is cheap.
        """
        if not isinstance(payload, dict):
            raise ValueError("hook input must be an object")
        conversation_id = payload.get("conversationId")
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError("hook requires a nonempty conversationId")
        if any(ord(c) < 32 for c in conversation_id) or "/" in conversation_id:
            raise ValueError("invalid conversationId")
        workspaces = payload.get("workspacePaths")
        if workspaces is None:
            workspaces = []
        if not isinstance(workspaces, list) or not all(
            isinstance(p, str) and Path(p).is_absolute() for p in workspaces
        ):
            raise ValueError("workspacePaths must be a list of absolute paths")
        transcript = payload.get("transcriptPath")
        if transcript is not None and (
            not isinstance(transcript, str) or not Path(transcript).is_absolute()
        ):
            raise ValueError("transcriptPath must be an absolute path or null")
        if workspaces:
            cwd = workspaces[0]
        elif transcript and transcript_conversation_id(Path(transcript)):
            cwd = str(Path(transcript).parents[4])  # the agy app-data directory
        else:
            cwd = str(Path.home())
        return SessionEvent(self.name, self.name, conversation_id, transcript, cwd, STOP_EVENT)

    def current_id(self) -> str | None:
        """Antigravity exports no session hint to child processes that cairn reads."""
        return None

    def resume(self, session_id: str, fork: bool = False) -> list[str]:
        if fork:
            raise ValueError("antigravity cannot fork a conversation")
        return [self.executable, "--conversation", session_id]

    # ---- invoke --------------------------------------------------------------

    def policy_argv(self, profile: str, native: tuple[str, ...] = ()) -> tuple[str, ...]:
        """The exact native permission flags: ``native`` when given, else the profile's."""
        if native:
            return native
        try:
            return MODE_BY_PROFILE[profile]
        except KeyError:
            known = ", ".join(MODE_BY_PROFILE)
            raise ValueError(f"unknown profile {profile!r}; choose {known}") from None

    def exec_argv(self, request: InvokeRequest) -> list[str]:
        """The headless ``agy`` argv: ``--conversation`` on a resume, a fresh conversation
        otherwise (agy picks the id; the ``init`` event reports it).

        ``--print-timeout 0`` hands the clock to cairn's watchdogs. Slash-command
        expansion stays on: ``--disable-slash-commands`` also disables ``--mode``
        (agy 1.4.6 warns "--mode plan has no effect while slash command expansion is
        disabled"), which would silently drop the review profile's read-only mode.
        The prompt always starts with cairn's preamble, so it cannot expand as a
        command. A resume re-sends the permission flags: agy does not keep them with
        the conversation.
        """
        argv = [
            self.executable,
            "--output-format",
            "stream-json",
            "--print-timeout",
            "0",
            *self.policy_argv(request.profile, request.native_policy),
        ]
        if request.resume:
            argv += ["--conversation", request.resume]
        if request.model:
            argv += ["--model", request.model]
        if request.effort:
            argv += ["--effort", request.effort]
        if request.output_schema is not None:
            argv += ["--json-schema", str(request.output_schema)]
        if request.log_path is not None:
            argv += ["--log-file", str(request.log_path)]
        argv += request.extra
        if request.prompt_via_stdin:
            # ``-p=`` (empty) enters print mode; the prompt arrives as one NDJSON line.
            argv += ["--input-format", "stream-json", "-p="]
        else:
            argv.append(f"-p={request.prompt}")
        return argv

    def stdin_payload(self, prompt: str) -> bytes:
        """The ``--input-format stream-json`` message for ``prompt``."""
        message = {"event": "user", "message": {"content": prompt}}
        return (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")

    def parse_event(self, line: str) -> list[Event]:
        """Map one ``agy --output-format stream-json`` line to zero or more events."""
        line = line.strip()
        if not line:
            return []
        try:
            data = json.loads(line)
        except ValueError:
            return [Event("notice", text=f"non-JSON stdout: {_clip(line, 500)}")]
        if not isinstance(data, dict):
            return [Event("notice", text=f"non-JSON stdout: {_clip(line, 500)}")]
        event = str(data.get("event") or "")
        if event == "init":
            raw_init = data.get("init")
            init: dict = raw_init if isinstance(raw_init, dict) else {}
            return [
                Event(
                    "thread_started",
                    thread_id=data.get("conversation_id") or None,
                    text=init.get("model") or None,
                    raw_type="init",
                )
            ]
        if event == "step_update":
            step = data.get("step_update")
            return self._step_events(step) if isinstance(step, dict) else []
        if event == "result":
            result = data.get("result")
            return self._result_events(result) if isinstance(result, dict) else []
        return []

    def _step_events(self, step: dict) -> list[Event]:
        step_type = str(step.get("step_type") or "")
        state = str(step.get("state") or "")
        index = step.get("step_index")
        key = index if isinstance(index, int) else -1
        raw = f"step_update:{step_type}:{state}"
        if step_type == "agent_response":
            delta = step.get("text_delta")
            if isinstance(delta, str) and delta:
                self._text.setdefault(key, []).append(delta)
            if state != "DONE":
                return []
            text = "".join(self._text.pop(key, [])).strip()
            return [Event("message", text=text, raw_type=raw)] if text else []
        if step_type == "tool":
            raw_info = step.get("tool_info")
            info: dict = raw_info if isinstance(raw_info, dict) else {}
            name = step.get("tool_name") or info.get("name") or "?"
            arguments = json.dumps(info.get("parameters") or {}, ensure_ascii=False)
            error = info.get("error")
            events: list[Event] = []
            if key not in self._tools_started:
                self._tools_started.add(key)
                events.append(
                    Event("progress", text=f"tool: {name} {_clip(arguments, 120)}", raw_type=raw)
                )
            if state == "DONE":
                self._tools_started.discard(key)
                if isinstance(error, dict) and (error.get("message") or error.get("type")):
                    detail = error.get("message") or error.get("type")
                    events.append(
                        Event(
                            "progress",
                            text=f"tool failed: {name}: {_clip(str(detail), 200)}",
                            raw_type=raw,
                        )
                    )
            subagents = (step.get("subagent_info") or {}).get("subagents")
            if state == "DONE" and isinstance(subagents, list) and subagents:
                names = [
                    f"{sub.get('type_name', '?')} ({sub.get('role', '?')})"
                    for sub in subagents
                    if isinstance(sub, dict)
                ]
                events.append(
                    Event(
                        "progress",
                        text=f"subagents: {_clip(', '.join(names), 200)}",
                        raw_type=raw,
                    )
                )
            return events
        return []

    def _result_events(self, result: dict) -> list[Event]:
        status = str(result.get("status") or "")
        response = result.get("response")
        text = response.strip() if isinstance(response, str) and response.strip() else None
        usage = {
            key: result.get(key) for key in ("duration_seconds", "num_turns", "structured_output")
        }
        tokens = result.get("usage")
        if isinstance(tokens, dict):
            usage.update(tokens)
        # Headless agy soft-denies a tool it could not ask about and still reports
        # SUCCESS; the result names what was denied.
        denied = result.get("denied_actions")
        if isinstance(denied, list) and denied:
            usage["denied_actions"] = [
                str(item.get("action") or item.get("display_name") or "tool")
                for item in denied
                if isinstance(item, dict)
            ]
        if status == _SUCCESS:
            return [Event("done", text=text, status="succeeded", usage=usage, raw_type="result")]
        error = result.get("error")
        detail = str(error) if error else f"agy ended with status {status or 'unknown'}"
        return [
            Event(
                "done",
                text=text,
                status="failed",
                error=f"{status or 'unknown'}: {detail[:500]}",
                usage=usage,
                raw_type=f"result:{status or 'unknown'}",
            )
        ]

    def transcript_path(self, store: Path, thread_id: str, started: datetime | None) -> Path | None:
        """The conversation's JSONL transcript under the agy app-data ``store``
        (``started`` is unused)."""
        return conversation_transcript(store, thread_id)

    def self_check(self, model: str | None, effort: str | None, fast: bool) -> list[str]:
        """Problems with an effort/fast choice. Any model slug is passed through; agy
        itself rejects an unknown one with ``invalid model selection`` and exit 1."""
        problems: list[str] = []
        if effort is not None and effort not in ANTIGRAVITY_EFFORTS:
            problems.append(
                f"effort {effort!r} is not supported by antigravity; supported: "
                f"{', '.join(ANTIGRAVITY_EFFORTS)}"
            )
        if fast:
            problems.append("fast mode is Codex-only")
        return problems


# ---------------------------------------------------------------------------
# Billing route
# ---------------------------------------------------------------------------


def adc_path(env: Mapping[str, str]) -> Path:
    """Where gcloud's Application Default Credentials live for this environment."""
    explicit = env.get(ADC_FILE_ENV)
    if explicit:
        return Path(explicit).expanduser()
    base = env.get("CLOUDSDK_CONFIG")
    home = Path(env.get("HOME") or Path.home())
    root = Path(base).expanduser() if base else home / ".config" / "gcloud"
    return root / "application_default_credentials.json"


def adc_quota_project(path: Path) -> str | None:
    """The ``quota_project_id`` recorded in an ADC file; ``ValueError`` when unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    project = data.get("quota_project_id")
    return project if isinstance(project, str) and project.strip() else None


def resolved_project(billing: BillingPolicy, env: Mapping[str, str]) -> str | None:
    """The GCP quota project a ``gcp`` run bills: the policy's, else the environment's,
    else the ADC file's. ``None`` for the other routes or when nothing names one."""
    if billing.route != "gcp":
        return None
    if billing.project:
        return billing.project
    if env.get(QUOTA_PROJECT_ENV):
        return env[QUOTA_PROJECT_ENV]
    path = adc_path(env)
    if not path.is_file():
        return None
    try:
        return adc_quota_project(path)
    except ValueError:
        return None


def settings_path(store: Path | None) -> Path:
    """agy's user settings (permission rules live under ``permissions.allow``)."""
    return (store or DEFAULT_APP_DATA) / SETTINGS_NAME


def permission_rules(path: Path) -> tuple[list[str], list[str]]:
    """``(allow, deny)`` rules from an agy settings file; empty when there is none."""
    if not path.is_file():
        return [], []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must hold a JSON object")
    permissions = data.get("permissions") or {}
    if not isinstance(permissions, dict):
        raise ValueError(f"{path}: permissions must be an object")
    allow = [r for r in permissions.get("allow") or [] if isinstance(r, str)]
    deny = [r for r in permissions.get("deny") or [] if isinstance(r, str)]
    return allow, deny


def shell_rules_present(allow: list[str]) -> bool:
    """Whether any shell command at all may run headlessly."""
    return any(rule.startswith("command(") for rule in allow)


def command_rules(commands: tuple[str, ...]) -> list[str]:
    return [f"command({command})" for command in commands]


def grant_commands(path: Path, commands: tuple[str, ...]) -> list[str]:
    """Add ``command(...)`` allow rules to an agy settings file, keeping every other
    setting and rule (deny rules included) as they are. Returns the rules added."""
    data: dict = {}
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"{path} must hold a JSON object")
        data = loaded
    permissions = data.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        raise ValueError(f"{path}: permissions must be an object")
    allow = permissions.setdefault("allow", [])
    if not isinstance(allow, list):
        raise ValueError(f"{path}: permissions.allow must be a list")
    added = [rule for rule in command_rules(commands) if rule not in allow]
    if added:
        allow.extend(added)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return added


def billing_problems(billing: BillingPolicy, env: Mapping[str, str]) -> list[str]:
    """Why a run under ``billing`` must not be spawned (empty when it may)."""
    if billing.route != "gcp":
        return []
    path = adc_path(env)
    if not path.is_file():
        return [
            f"the gcp billing route needs Application Default Credentials and {path} "
            f"does not exist; {ADC_LOGIN_HINT}"
        ]
    try:
        in_file = adc_quota_project(path)
    except ValueError as exc:
        return [f"the ADC file {path} is unreadable ({exc}); {ADC_LOGIN_HINT}"]
    if not (billing.project or env.get(QUOTA_PROJECT_ENV) or in_file):
        return [
            "the gcp billing route has no quota project: set "
            "invoke.antigravity.billing.project, export GOOGLE_CLOUD_QUOTA_PROJECT, or run "
            "'gcloud auth application-default set-quota-project <PROJECT>'"
        ]
    return []


def billing_env(billing: BillingPolicy, env: Mapping[str, str]) -> dict[str, str]:
    """The child environment for ``billing``: a copy of ``env`` with the route applied."""
    child = dict(env)
    if billing.route == "gcp":
        child[ADC_AUTH_ENV] = "true"
        if billing.project:
            child[QUOTA_PROJECT_ENV] = billing.project
        if billing.location:
            child[LOCATION_ENV] = billing.location
    elif billing.route == "plan":
        child.pop(ADC_AUTH_ENV, None)
    return child


def observe_billing(log_path: Path) -> dict:
    """What agy's own log says the run did: ``auth_method`` and ``quota_project`` from
    the last ``applyAuthResult`` line, and every project named in a Vertex AI URL."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"auth_method": None, "quota_project": None, "vertex_projects": []}
    matches = _AUTH_LINE.findall(text)
    method, project = matches[-1] if matches else (None, None)
    return {
        "auth_method": method,
        "quota_project": project or None,
        "vertex_projects": sorted(set(_VERTEX_URL.findall(text))),
    }


def billing_problem(route: str, project: str | None, observed: dict) -> str | None:
    """A mismatch between the requested route and what the log shows, or ``None``.

    ``gcp`` must have authenticated through ADC on the expected project and sent its
    model requests to that project. ``plan`` must not have used ADC. ``inherit`` is
    whatever the host gave; nothing to verify.
    """
    method = observed.get("auth_method")
    quota = observed.get("quota_project")
    vertex = observed.get("vertex_projects") or []
    if route == "gcp":
        if method is None:
            return "agy's log has no applyAuthResult line; the billing route is unverified"
        if method != "adc":
            return f"gcp route requested but agy authenticated with authMethod={method}"
        if project and quota != project:
            return f"gcp route on project {project} but agy used quotaProject={quota}"
        if project and any(p != project for p in vertex):
            return f"gcp route on project {project} but requests went to {', '.join(vertex)}"
        return None
    if route == "plan" and method == "adc":
        return f"plan route requested but agy authenticated with ADC (quotaProject={quota})"
    return None
