# Stub `agy` for tests/test_invoke.py, mimicking agy 1.4.6
# `agy --output-format stream-json ... -p=<prompt>`: init, step_update (user_input,
# agent_response deltas, one tool step), result. `--conversation ID` resumes that id;
# otherwise a fresh uuid. `--input-format stream-json` reads one NDJSON user message
# from stdin. STUB_MODE=badmodel prints only an ERROR result and exits 1 (verified
# live for an unknown --model). STUB_ANSWER sets the final text. `--log-file PATH`
# gets the two log lines cairn reads for the billing route: authMethod is `adc` when
# AGY_ADC_AUTH=true is in the environment (else `gcp`, the login's own method), and
# the quota project is $GOOGLE_CLOUD_QUOTA_PROJECT or `stub-project`. With
# STUB_AGY_STORE set, the run is recorded the way agy records it: transcript JSONL
# under $STUB_AGY_STORE/brain/<id>/.system_generated/logs/ and a row in
# $STUB_AGY_STORE/conversation_summaries.db. Extra steps, all verified live on 1.4.6:
# STUB_CMD=<line> runs a run_command step (DONE); STUB_WRITE=<path> a write_to_file step;
# STUB_DENY=<line> a run_command step agy refused (state ERROR, "permission check
# failed") plus denied_actions on the result, and with STUB_DENY_ANSWER unset the
# final response is empty, exactly as the real denied round ended; STUB_SLEEP=<s>
# pauses before the answer so a watcher can follow the stream mid-run.
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

args = sys.argv[1:]
VALUED = {
    "--output-format",
    "--input-format",
    "--print-timeout",
    "--conversation",
    "--mode",
    "--model",
    "--effort",
    "--json-schema",
    "--log-file",
}
options: dict[str, str] = {}
prompt = None
index = 0
while index < len(args):
    arg = args[index]
    if arg.startswith("-p="):
        prompt = arg[3:]
        index += 1
    elif arg in VALUED:
        options[arg] = args[index + 1]
        index += 2
    else:
        options[arg] = ""
        index += 1
if prompt is None:
    print("flag needs an argument: -p", file=sys.stderr)
    sys.exit(2)
used_stdin = options.get("--input-format") == "stream-json"
if used_stdin:
    messages = [json.loads(line) for line in sys.stdin.read().splitlines() if line.strip()]
    prompt = "\n".join(str(m["message"]["content"]) for m in messages)
conversation = options.get("--conversation") or str(uuid.uuid4())
log = os.environ.get("STUB_LOG")
if log:
    with open(log, "a") as handle:
        entry = {
            "argv": sys.argv,
            "env": dict(os.environ),
            "cwd": os.getcwd(),
            "stdin": used_stdin,
            "prompt_len": len(prompt or ""),
            "prompt_head": (prompt or "")[:1600],
            "thread": conversation,
        }
        handle.write(json.dumps(entry) + "\n")


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


if options.get("--log-file"):
    method = "adc" if os.environ.get("AGY_ADC_AUTH") == "true" else "gcp"
    project = os.environ.get("GOOGLE_CLOUD_QUOTA_PROJECT", "stub-project")
    with open(options["--log-file"], "w") as handle:
        handle.write(
            "I1023 10:53:39.862890       1 server_oauth.go:224] applyAuthResult: "
            f"email=x@y.z, authMethod={method}, quotaProject={project}\n"
            "I1023 10:53:43.468255      63 http_helpers.go:349] URL: https://aiplatform."
            f"googleapis.com/v1/projects/{project}/locations/global/publishers/google/"
            "models/gemini-4-argon:streamGenerateContent?alt=sse ResponseID: x\n"
        )

if os.environ.get("STUB_MODE") == "badmodel":
    message = (
        f'invalid model selection (--model "{options.get("--model")}" --effort ""): '
        f"model {options.get('--model')} is not recognized as a known model"
    )
    emit(
        {
            "event": "result",
            "result": {
                "conversation_id": "",
                "status": "ERROR",
                "response": "",
                "error": message,
                "duration_seconds": 0,
                "num_turns": 0,
            },
        }
    )
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)

if not prompt:
    print("error: empty prompt", file=sys.stderr)
    sys.exit(1)

answer = os.environ.get("STUB_ANSWER", "PONG")


def record_native(store: Path) -> None:
    """Append this turn to the conversation's transcript and upsert its index row."""
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    logs = store / "brain" / conversation / ".system_generated" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    full = logs / "transcript_full.jsonl"
    existing = full.read_text().count("\n") if full.exists() else 0
    steps = [
        {
            "step_index": existing,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": now,
            "content": f"<USER_REQUEST>\n{prompt}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\n"
            "The current local time is: now.\n</ADDITIONAL_METADATA>",
        },
        {
            "step_index": existing + 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": now,
            "input_tokens": 10,
            "output_tokens": 2,
            "cache_read_tokens": 0,
            "thinking": "Looking.",
            "tool_calls": [{"name": "read_file", "args": {"Path": "a.py"}}],
        },
        {
            "step_index": existing + 2,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "DONE",
            "created_at": now,
            "content": "Created At: now\nCompleted At: now\n1 lines",
        },
        {
            "step_index": existing + 3,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": now,
            "input_tokens": 5,
            "output_tokens": 1,
            "cache_read_tokens": 0,
            "content": answer,
        },
    ]
    lines = "".join(json.dumps(step) + "\n" for step in steps)
    with full.open("a") as handle:
        handle.write(lines)
    with (logs / "transcript.jsonl").open("a") as handle:
        handle.write(lines)
    db = sqlite3.connect(store / "conversation_summaries.db")
    db.execute(
        "create table if not exists conversation_summaries (conversation_id text primary key, "
        "title text, preview text, step_count integer, last_modified_time text, "
        "workspace_uris text, status text, source text, agent_name text, "
        "parent_conversation_id text, nesting_depth integer, last_user_input_time text)"
    )
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f+00:00")
    db.execute(
        "insert into conversation_summaries values (?,?,?,?,?,?,?,?,?,?,?,?) "
        "on conflict(conversation_id) do update set step_count=excluded.step_count, "
        "last_modified_time=excluded.last_modified_time, last_user_input_time=excluded.last_user_input_time",
        (
            conversation,
            "Stub Conversation Title",
            (prompt or "")[:60],
            existing + len(steps),
            stamp,
            json.dumps([Path(os.getcwd()).as_uri()]),
            "CASCADE_RUN_STATUS_IDLE",
            "",
            "",
            "",
            0,
            stamp,
        ),
    )
    db.commit()
    db.close()


if os.environ.get("STUB_AGY_STORE"):
    record_native(Path(os.environ["STUB_AGY_STORE"]))

emit(
    {
        "event": "init",
        "conversation_id": conversation,
        "init": {"cwd": os.getcwd(), "tools": ["read_file"], "permission_mode": "request-review"},
    }
)


def step(index: int, state: str, step_type: str, **extra: object) -> None:
    emit(
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": conversation,
                "step_index": index,
                "state": state,
                "step_type": step_type,
                **extra,
            },
        }
    )


step(0, "DONE", "user_input")
step(1, "ACTIVE", "agent_response", text_delta="Look")
step(1, "DONE", "agent_response", text_delta="ing.\n", duration_seconds=0.5)
tool = {"name": "read_file", "parameters": {"Path": "a.py"}}
step(2, "ACTIVE", "tool", tool_name="read_file", tool_info=tool)
step(2, "DONE", "tool", tool_name="read_file", tool_info={**tool, "output": "1 lines"})
index = 3
command = os.environ.get("STUB_CMD")
if command:
    info = {"name": "run_command", "parameters": {"CommandLine": command}}
    step(index, "ACTIVE", "tool", tool_name="run_command", tool_info=info)
    step(index, "DONE", "tool", tool_name="run_command", tool_info={**info, "output": "ok"})
    index += 1
target = os.environ.get("STUB_WRITE")
if target:
    info = {"name": "write_to_file", "parameters": {"TargetFile": target}}
    step(index, "ACTIVE", "tool", tool_name="write_to_file", tool_info=info)
    step(index, "DONE", "tool", tool_name="write_to_file", tool_info=info, duration_seconds=0.1)
    index += 1
denied = os.environ.get("STUB_DENY")
if denied:
    info = {"name": "run_command", "parameters": {"CommandLine": denied}}
    step(index, "ACTIVE", "tool", tool_name="run_command", tool_info=info)
    error = {
        "type": "TOOL_ERROR",
        "message": f'permission check failed for command "{denied}": user denied permission '
        f"to run command:\n{denied}\nDo not attempt to circumvent this denial by rephrasing "
        "the command. Proceed without performing this action.",
    }
    step(index, "ERROR", "tool", tool_name="run_command", tool_info={**info, "error": error})
    index += 1
    if not os.environ.get("STUB_DENY_ANSWER"):
        answer = ""
    print(
        'jetski: no output produced — a tool required the "command" permission that headless '
        "mode cannot prompt for, so it was auto-denied.",
        file=sys.stderr,
    )
if os.environ.get("STUB_SLEEP"):
    time.sleep(float(os.environ["STUB_SLEEP"]))
if answer:
    step(index, "ACTIVE", "agent_response", text_delta=answer)
    step(index, "DONE", "agent_response", text_delta="\n", duration_seconds=1.0)
emit(
    {
        "event": "result",
        "result": {
            "conversation_id": conversation,
            "status": "SUCCESS",
            "response": (answer + "\n") if answer else "",
            "duration_seconds": 1.5,
            "num_turns": 1,
            "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            **(
                {"denied_actions": [{"action": "command", "display_name": "RunCommand"}]}
                if denied
                else {}
            ),
        },
    }
)
