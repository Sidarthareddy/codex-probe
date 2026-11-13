"""CodexProbe quickstart: record one Codex run from start to finish.

    python examples/quickstart.py --config examples/config.qwen.json --model qwen2.5-coder:7b
    python examples/quickstart.py --config examples/config.openai.json --model <openai model>

What it does:
  1. starts the recorder (the local proxy)
  2. writes a Codex config that points Codex at the proxy
  3. runs `codex exec "<task>"` in a small demo folder
  4. stops the recorder and prints what was captured

Your real ~/.codex/config.toml is never touched. Codex is told to use a
private config folder (.codex_home/ in this repo) through the CODEX_HOME
environment variable.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from codex_probe import ProxyRecorder

DEFAULT_TASK = "Add a one line docstring to every function in hello.py. Do not change anything else."

HELLO_PY = '''def add(a, b):
    return a + b


def greet(name):
    return f"Hello, {name}!"


def is_even(n):
    return n % 2 == 0
'''


def codex_config_toml(endpoint: str, model: str) -> str:
    """The config.toml fragment that makes Codex talk to the proxy.

    model_provider must be at the top level, above the [model_providers.probe]
    table. The same fragment works for every backend: only `model` changes,
    because Codex always talks to the proxy and the proxy picks the backend.
    """
    return (
        f'model_provider = "probe"\n'
        f'model = "{model}"\n'
        f"\n"
        f"[model_providers.probe]\n"
        f'name = "CodexProbe"\n'
        f'base_url = "{endpoint}"\n'
        f'wire_api = "responses"\n'
        # Codex reads the API key from this variable and sends it as the
        # Authorization header. The proxy then applies its own auth rule.
        f'env_key = "OPENAI_API_KEY"\n'
    )


def print_summary(recorder: ProxyRecorder, calls: list[dict]) -> None:
    summary = recorder.summary
    print("\n" + "=" * 72)
    print(f"Session   {summary['session_id']}")
    print(f"Backend   {', '.join(summary['backends']) or '-'}   Model  {', '.join(summary['models']) or '-'}")
    print(f"LLM calls {summary['num_calls']}   errors {summary['num_errors']}")
    print(
        f"Tokens    input {summary['total_input_tokens']}  "
        f"(cached {summary['total_cached_input_tokens']})  output {summary['total_output_tokens']}"
    )
    cost = summary["total_cost_usd"]
    print(f"Cost      {'unknown' if cost is None else f'${cost:.6f}'}")
    print(f"Latency   total {summary['total_latency_ms']} ms, average {summary['average_latency_ms']} ms")

    print("\ncall  input_tok  output_tok  latency_ms  tok/s   what the model returned")
    for call, row in zip(calls, summary["per_call"]):
        print(
            f"{row['call_index']:>4}  {str(row['input_tokens']):>9}  {str(row['output_tokens']):>10}  "
            f"{row['latency_ms']:>10}  {str(row['output_tokens_per_second']):>6}  {describe_output(call)}"
        )
    for note in summary["notes"]:
        print(f"\nNote: {note}")
    print(f"\nFull log  {recorder.session.log_path}")
    print(f"Summary   {recorder.session.summary_path}")


def describe_output(call: dict) -> str:
    """One short line saying what kind of output a call produced."""
    if call["error"]:
        return f"ERROR {call['error'][:60]}"
    body = call["response"]["body"] if call["response"] else None
    if not isinstance(body, dict):
        return "-"
    parts = []
    for item in body.get("output", []):
        if item.get("type") == "function_call":
            parts.append(f"tool call {item.get('name')}({str(item.get('arguments'))[:50]})")
        elif item.get("type") == "message":
            text = "".join(part.get("text", "") for part in item.get("content", []))
            parts.append("text: " + text[:50].replace("\n", " "))
        else:
            parts.append(str(item.get("type")))
    return " | ".join(parts) or "-"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, help="recorder config JSON (see examples/)")
    parser.add_argument("--model", required=True, help='model name Codex should ask for, e.g. "qwen2.5-coder:7b"')
    parser.add_argument("--task", default=DEFAULT_TASK, help="prompt given to codex exec")
    parser.add_argument("--workdir", default="demo_workspace", help="folder Codex works in")
    parser.add_argument("--timeout", type=int, default=900, help="seconds before Codex is stopped")
    args = parser.parse_args()

    # A small, known starting point for the task.
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    if args.task == DEFAULT_TASK:
        (workdir / "hello.py").write_text(HELLO_PY)

    recorder = ProxyRecorder(args.config)
    endpoint = recorder.start()
    print(f"Recorder listening on {endpoint}")
    print(f"Forwarding to {recorder.config.backend.name} at {recorder.config.backend.base_url}")

    codex_home = Path(".codex_home").resolve()
    codex_home.mkdir(exist_ok=True)
    fragment = codex_config_toml(endpoint, args.model)
    (codex_home / "config.toml").write_text(fragment)
    print(f"\nCodex config written to {codex_home / 'config.toml'}:\n")
    print(fragment)

    env = dict(os.environ, CODEX_HOME=str(codex_home))
    # Codex wants env_key to exist even when the backend needs no key.
    env.setdefault("OPENAI_API_KEY", "not-needed-for-local-backend")
    command = [
        "codex", "exec",
        "--skip-git-repo-check",          # the demo folder is not a git repo
        "--sandbox", "workspace-write",   # Codex may edit files only inside workdir
        "-C", str(workdir),
        args.task,
    ]
    print("Running:", " ".join(command[:-1]), f'"{args.task}"\n')
    try:
        # stdin=DEVNULL: otherwise codex exec waits to read extra input from stdin.
        result = subprocess.run(command, env=env, timeout=args.timeout, stdin=subprocess.DEVNULL)
        exit_code = result.returncode
    except subprocess.TimeoutExpired:
        print(f"\nCodex was stopped after {args.timeout} seconds.")
        exit_code = 124
    except FileNotFoundError:
        print("\nThe `codex` command was not found. Install Codex CLI first.")
        exit_code = 127
    finally:
        calls = recorder.stop()

    print_summary(recorder, calls)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
