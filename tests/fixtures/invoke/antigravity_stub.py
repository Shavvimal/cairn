# Stub `agy` for tests/test_invoke.py, mimicking agy 1.4.6
# `agy --output-format stream-json ... -p=<prompt>`: init, step_update (user_input,
# agent_response deltas, one tool step), result. `--conversation ID` resumes that id;
# otherwise a fresh uuid. `--input-format stream-json` reads one NDJSON user message
# from stdin. STUB_MODE=badmodel prints only an ERROR result and exits 1 (verified
# live for an unknown --model). STUB_ANSWER sets the final text. `--log-file PATH`
# gets the two log lines cairn reads for the billing route: authMethod is `adc` when
# AGY_ADC_AUTH=true is in the environment (else `gcp`, the login's own method), and
# the quota project is $GOOGLE_CLOUD_QUOTA_PROJECT or `stub-project`.
import json
import os
import sys
import uuid

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
            "prompt_head": (prompt or "")[:400],
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
step(3, "ACTIVE", "agent_response", text_delta=answer)
step(3, "DONE", "agent_response", text_delta="\n", duration_seconds=1.0)
emit(
    {
        "event": "result",
        "result": {
            "conversation_id": conversation,
            "status": "SUCCESS",
            "response": answer + "\n",
            "duration_seconds": 1.5,
            "num_turns": 1,
            "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        },
    }
)
