# Stub `codex` for tests/test_invoke.py. The test writes it into a temp bin/ with a
# `#!<python>` line. It mimics `codex exec [resume|fork] --json ...` from codex-cli
# 0.160: the verified event sequence on stdout, a live rollout under
# $CODEX_HOME/sessions/YYYY/MM/DD/, and the -o last-message file.
#
# STUB_MODE: (unset) normal | sleep (one event, a `sleep` child, then normal) |
# hold (one event, touch $STUB_READY, wait for $STUB_RELEASE, then normal) |
# rollout (one event, then only rollout records every 0.3 s for STUB_SLEEP s, then
# normal) | closeout (one event, close stdout, sleep STUB_SLEEP s) | split (the
# final answer "PONG é" written in two chunks split inside the "é", a pause, and a
# last line with no trailing newline) | fail401 | turnfail. In sleep mode SIGTERM
# prints a SHUTDOWN agent_message before exiting. STUB_SLEEP seconds (default 30). STUB_ANSWER final text.
# STUB_LOG: append one JSON line per call (argv, env, cwd, stdin use, prompt).
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

argv = sys.argv[1:]
assert argv[0] == "exec", argv
sub = argv[1] if len(argv) > 1 and argv[1] in ("resume", "fork") else None
rest = argv[2:] if sub else argv[1:]
VALUED = {"-o", "-C", "-c", "-m", "-s", "--enable", "--output-schema", "--sandbox"}
options: dict[str, list[str]] = {}
positionals: list[str] = []
index = 0
while index < len(rest):
    arg = rest[index]
    if arg in VALUED:
        options.setdefault(arg, []).append(rest[index + 1])
        index += 2
    elif arg.startswith("-") and arg != "-":
        options.setdefault(arg, []).append("")
        index += 1
    else:
        positionals.append(arg)
        index += 1
if "-C" in options:
    os.chdir(options["-C"][0])
prompt = positionals[-1] if positionals else "-"
used_stdin = prompt == "-"
if used_stdin:
    prompt = sys.stdin.read()
target = positionals[0] if sub else None
thread = target if sub == "resume" else "019a" + str(uuid.uuid4())[4:]
mode = os.environ.get("STUB_MODE", "")
answer = os.environ.get("STUB_ANSWER", "ANSWER")
log = os.environ.get("STUB_LOG")
if log:
    with open(log, "a") as handle:
        entry = {
            "argv": sys.argv,
            "env": dict(os.environ),
            "cwd": os.getcwd(),
            "stdin": used_stdin,
            "prompt_len": len(prompt),
            "prompt_head": prompt[:400],
            "thread": thread,
            "pid": os.getpid(),
        }
        handle.write(json.dumps(entry) + "\n")

if mode == "fail401":
    print('{"error":{"status":401,"message":"unauthorized"}} "status":401', file=sys.stderr)
    sys.exit(1)

now = datetime.now(UTC)
sessions = Path(os.environ["CODEX_HOME"]) / "sessions"
existing = sorted(sessions.rglob(f"rollout-*-{thread}.jsonl"))
if existing:
    rollout = existing[-1]
else:
    folder = sessions / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}"
    folder.mkdir(parents=True, exist_ok=True)
    rollout = folder / f"rollout-{now:%Y-%m-%dT%H-%M-%S}-{thread}.jsonl"


def stamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def record(rtype: str, payload: dict) -> None:
    with rollout.open("a") as handle:
        handle.write(json.dumps({"timestamp": stamp(), "type": rtype, "payload": payload}) + "\n")


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


if not existing:
    record(
        "session_meta",
        {"id": thread, "cwd": os.getcwd(), "timestamp": stamp(), "originator": "codex_exec"},
    )
record(
    "response_item",
    {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": prompt}],
        "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["user.text"]},
    },
)
emit({"type": "thread.started", "thread_id": thread})
if mode == "hold":
    Path(os.environ["STUB_READY"]).touch()
    release = Path(os.environ["STUB_RELEASE"])
    deadline = time.monotonic() + 120
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
if mode == "rollout":
    until = time.monotonic() + float(os.environ.get("STUB_SLEEP", "30"))
    while time.monotonic() < until:
        record("event_msg", {"type": "token_count", "info": None})
        time.sleep(0.3)
if mode == "closeout":
    sys.stdout.flush()
    os.close(1)
    time.sleep(float(os.environ.get("STUB_SLEEP", "30")))
    sys.exit(0)
if mode == "split":
    out = sys.stdout.buffer
    text = json.dumps(
        {"type": "item.completed", "item": {"id": "i4", "type": "agent_message", "text": "PONG é"}},
        ensure_ascii=False,
    ).encode()
    cut = text.index("é".encode()) + 1  # inside the two-byte character
    out.write(text[:cut])
    out.flush()
    time.sleep(0.5)  # the runner meets a temporary EOF while the peer lives
    out.write(text[cut:] + b"\n")
    out.write(json.dumps({"type": "turn.completed", "usage": {"output_tokens": 3}}).encode())
    out.flush()
    if "-o" in options:
        Path(options["-o"][0]).write_text("PONG é")
    sys.exit(0)
if mode == "sleep":

    def shutdown(signum, frame):
        emit_line = {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "SHUTDOWN"},
        }
        print(json.dumps(emit_line), flush=True)
        sys.exit(143)

    signal.signal(signal.SIGTERM, shutdown)
    child = subprocess.Popen(["sleep", os.environ.get("STUB_SLEEP", "30")])
    if log:
        with open(log, "a") as handle:
            handle.write(json.dumps({"sleep_child": child.pid}) + "\n")
    child.wait()
emit(
    {
        "type": "item.completed",
        "item": {
            "id": "item_0",
            "type": "error",
            "message": "[features].codex_hooks is deprecated",
        },
    }
)
emit(
    {
        "type": "item.completed",
        "item": {"id": "item_1", "type": "error", "message": "loading hooks from both files"},
    }
)
emit({"type": "turn.started"})
emit({"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "Looking"}})
command = {"id": "i3", "type": "command_execution", "command": "cat a.txt", "exit_code": None}
emit({"type": "item.started", "item": command})
emit({"type": "item.completed", "item": {**command, "exit_code": 0}})
if mode == "turnfail":
    emit({"type": "turn.failed", "error": {"message": "model overloaded"}})
    sys.exit(1)
emit({"type": "item.completed", "item": {"id": "i4", "type": "agent_message", "text": answer}})
record(
    "response_item",
    {
        "type": "message",
        "role": "assistant",
        "phase": "final_answer",
        "content": [{"type": "output_text", "text": answer}],
    },
)
emit({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}})
if "-o" in options:
    Path(options["-o"][0]).write_text(answer)
time.sleep(0.05)
