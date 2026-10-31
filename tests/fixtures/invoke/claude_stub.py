# Stub `claude` for tests/test_invoke.py, mimicking
# `claude -p --output-format stream-json --verbose ...`: system/init, assistant
# (text plus a tool_use), result; it appends to $STUB_CLAUDE_STORE/-demo/<id>.jsonl.
# `--` ends the options and the next argument is the prompt; with no `--` the
# prompt comes from stdin. STUB_MODE=notlogged prints "Not logged in", rc 1.
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

args = sys.argv[1:]
VALUED = {
    "--output-format",
    "--session-id",
    "--resume",
    "--permission-mode",
    "--model",
    "--effort",
    "--json-schema",
}
options: dict[str, str] = {}
prompt = None
index = 0
while index < len(args):
    arg = args[index]
    if arg == "--":
        prompt = args[index + 1]
        break
    if arg in VALUED:
        options[arg] = args[index + 1]
        index += 2
    elif arg == "--disallowedTools":
        # variadic: swallows every following non-option argument
        index += 1
        while index < len(args) and not args[index].startswith("-"):
            options.setdefault(arg, "")
            options[arg] += args[index]
            index += 1
    else:
        options[arg] = ""
        index += 1
used_stdin = prompt is None
if used_stdin:
    prompt = sys.stdin.read()
if "--fork-session" in options or not (options.get("--session-id") or options.get("--resume")):
    session = str(uuid.uuid4())
else:
    session = options.get("--session-id") or options["--resume"]
log = os.environ.get("STUB_LOG")
if log:
    with open(log, "a") as handle:
        entry = {
            "argv": sys.argv,
            "env": dict(os.environ),
            "cwd": os.getcwd(),
            "stdin": used_stdin,
            "prompt_len": len(prompt or ""),
            "thread": session,
        }
        handle.write(json.dumps(entry) + "\n")
if not prompt:
    print(
        "Error: Input must be provided either through stdin or as a prompt argument",
        file=sys.stderr,
    )
    sys.exit(1)
if os.environ.get("STUB_MODE") == "notlogged":
    print("Not logged in · Please run /login", file=sys.stderr)
    sys.exit(1)
answer = os.environ.get("STUB_ANSWER", "PONG")
store = Path(os.environ["STUB_CLAUDE_STORE"]) / "-demo"
store.mkdir(parents=True, exist_ok=True)
now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
common = {"sessionId": session, "cwd": os.getcwd(), "timestamp": now}
with (store / f"{session}.jsonl").open("a") as handle:
    handle.write(
        json.dumps({**common, "type": "user", "message": {"role": "user", "content": prompt}})
        + "\n"
    )
    handle.write(
        json.dumps(
            {
                **common,
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": answer}],
                    "stop_reason": "end_turn",
                },
            }
        )
        + "\n"
    )


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


emit({"type": "system", "subtype": "init", "session_id": session, "model": "stub-model"})
emit(
    {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Looking."},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}},
            ]
        },
    }
)
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}})
emit(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": answer,
        "duration_ms": 5,
        "total_cost_usd": 0.0,
        "num_turns": 1,
    }
)
