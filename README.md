# CodexProbe

LLM call recorder, cost tracker, and backend swap for the OpenAI Codex CLI.

## Problem

Coding agents like Codex CLI look like they do one thing when you ask them something, but behind the scenes they run a loop of many LLM calls you never see. When an agent behaves strangely, stalls, or costs more than expected, there is no way to inspect the exact requests and responses that produced it.

CodexProbe is a streaming reverse proxy that sits between Codex and its model. It records every call in full, including the system prompt, the running conversation, the tool definitions and the model's replies, while relaying the stream live so the agent's behavior is unchanged. It also tracks tokens and cost per call, and lets you point the same agent at a locally hosted open model by changing config alone.

## Results: OpenAI vs Qwen

The same small task (add a docstring to every function in `hello.py`) run
through the recorder against two backends, with the same Codex config apart
from the model name:

| | OpenAI `gpt-5.4-mini` | Qwen `qwen2.5-coder:7b` |
|---|---:|---:|
| Task completed | yes | no |
| LLM calls | 5 | 1 |
| Input tokens (cached) | 60,998 (48,256) | 4,008 (0) |
| Output tokens | 489 | 23 |
| API cost | $0.0154 | $0 |
| Time spent in LLM calls | 8.1 s | 7.8 s |

The OpenAI session also shows the agent loop clearly: input grows from
11,873 to 12,578 tokens over five calls because the full history is resent
each time, and prompt caching cuts the session cost by 68%. The call by
call table is in the write-up, and both session logs are in
`docs/sample_session/`.

## How it works

```
Codex CLI  ->  CodexProbe (http://127.0.0.1:8135/v1)  ->  real backend (OpenAI, or local Qwen via Ollama)
                       |
                       v
     logs/<session_id>.jsonl          one line per LLM call: full request, full response
     logs/<session_id>.summary.json   token totals, cost, latency
```

It does three things:

1. **Record.** Forward every LLM call unchanged and save the complete request and response.
2. **Measure.** Read token usage from each response and compute cost per call and per session.
3. **Swap.** Send Codex to a different backend (a local Qwen model) by changing config only.

## Status

| Part | State |
|------|-------|
| Proxy, streaming relay, logging, usage and cost | Done, 54 tests against a mock backend |
| Real run through local Qwen (Ollama) | Done. Calls reach Qwen and are fully logged. Codex does **not** complete the task, see [Qwen findings](#qwen-findings) |
| Real run through OpenAI (`gpt-5.4-mini`) | Done. Codex completes the task in 5 calls for about $0.015 |

## Install

Needs Python 3.11 or newer.

```bash
git clone https://github.com/Sidarthareddy/codex-probe.git && cd codex-probe
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

For real runs you also need:

* **Codex CLI** (tested with 0.169.0): https://github.com/openai/codex
* **Ollama** (tested with 0.40.16) for the local model: https://ollama.com
* For the OpenAI run, a `.env` file in the project folder with `OPENAI_API_KEY=...`
  (the file is ignored by git)

## Usage

```python
from codex_probe import ProxyRecorder

recorder = ProxyRecorder("examples/config.qwen.json")   # a path or a dict
endpoint = recorder.start()        # "http://127.0.0.1:8135/v1", does not block
# point Codex at `endpoint` (see below), then run: codex exec "<task>"
calls = recorder.stop()            # list[dict], one per LLM call, in order
print(recorder.summary)            # token totals, cost, latency
```

`start()` runs the proxy in a background thread. `stop()` shuts it down,
writes the summary file, and returns the calls of that session.

Or let the quickstart script do all of it (start the recorder, write the
Codex config, run Codex on a small demo task, print the summary):

```bash
python examples/quickstart.py --config examples/config.openai.json --model gpt-5.4-mini
python examples/quickstart.py --config examples/config.qwen.json --model qwen2.5-coder:7b
```

The quickstart never touches your real `~/.codex/config.toml`. It writes a
private one to `.codex_home/` and points Codex at it with the `CODEX_HOME`
environment variable.

## Recorder config

```json
{
  "listen_host": "127.0.0.1",
  "listen_port": 8135,
  "backend": {
    "name": "ollama_qwen",
    "base_url": "http://localhost:11434/v1",
    "wire_api": "responses",
    "api_key_env": null
  },
  "log_dir": "logs",
  "pricing_file": "pricing.json",
  "seed": 0
}
```

Swapping the backend is a change to the `backend` block and nothing else.
`examples/config.openai.json` and `examples/config.qwen.json` differ only there.

`api_key_env` decides what happens to the `Authorization` header:

| `api_key_env` | What the proxy sends to the backend |
|---------------|-------------------------------------|
| `null` | No `Authorization` header. A key meant for OpenAI is never sent to another server |
| set, and the variable exists | `Bearer <that key>` (read from the environment or a `.env` file) |
| set, but the variable is missing | Whatever Codex sent |

Keys are never written to the logs. Secret headers are logged as `[REDACTED]`.

`wire_api` and `seed` are stored in the config but not used by the proxy yet.

## Codex config changes

Add this to `~/.codex/config.toml` (back the file up first). `model_provider`
must be at the top level, above the table:

```toml
model_provider = "probe"
model = "qwen2.5-coder:7b"      # or an OpenAI model name for the OpenAI run

[model_providers.probe]
name = "CodexProbe"
base_url = "http://127.0.0.1:8135/v1"
wire_api = "responses"
env_key = "OPENAI_API_KEY"
```

This fragment is the same for every backend. Codex always talks to the proxy,
and the proxy config picks the real backend. Only `model` changes.

Then run Codex as usual:

```bash
codex exec --sandbox workspace-write "Add a one line docstring to every function in hello.py"
```

## Serving the local Qwen model

```bash
ollama pull qwen2.5-coder:7b                       # 4.7 GB download
OLLAMA_CONTEXT_LENGTH=32768 ollama serve           # serves http://localhost:11434/v1
```

Hardware: tested on a MacBook Pro with Apple Silicon and 32 GB RAM. With a
32k context the loaded model uses about 6.6 GB of memory and runs fully on
the GPU. Any Apple Silicon Mac with 16 GB should be enough. vLLM needs an
NVIDIA GPU, so it was not used here.

Ollama accepts `POST /v1/responses` directly, the same format Codex sends,
so no format translation is needed.

## What a captured call looks like

Each line of `logs/<session_id>.jsonl` is one LLM call. This is a real one
from the Qwen run, with the long parts shortened here (the log itself is
never shortened). The full file is in `docs/sample_session/`, next to the
five call OpenAI session.

```json
{
  "session_id": "2026-11-13T15-28-24_71bb",
  "call_index": 0,
  "timestamp_start": "2026-11-13T...",
  "latency_ms": 7776,
  "first_chunk_ms": 7019,
  "backend": {"name": "ollama_qwen", "base_url": "http://localhost:11434/v1"},
  "model": "qwen2.5-coder:7b",
  "path": "/v1/responses",
  "client_session_id": "01a1d698-dc0d-7381-914c-1fb96b3dbabd",
  "request": {
    "headers": {"authorization": "[REDACTED]", "originator": "codex_exec", "...": "..."},
    "body": {
      "model": "qwen2.5-coder:7b",
      "stream": true,
      "input": [
        {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "You are a coding agent... (17,000+ characters)"}]},
        {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "<skills_instructions>..."}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>..."}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Add a one line docstring to every function in hello.py. ..."}]}
      ],
      "tools": [{"type": "function", "name": "exec_command", "...": "..."}, "... 8 more"],
      "tool_choice": "auto",
      "parallel_tool_calls": true,
      "store": false
    }
  },
  "translated_request": null,
  "response": {
    "status": 200,
    "streamed": true,
    "body": {
      "status": "completed",
      "model": "qwen2.5-coder:7b",
      "output": [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "{\"name\": \"exec_command\", \"arguments\": {\"cmd\": \"echo 'Hello, world!'\"}}"}]}
      ],
      "usage": {"input_tokens": 4008, "output_tokens": 23, "total_tokens": 4031}
    }
  },
  "usage": {"input_tokens": 4008, "cached_input_tokens": 0, "output_tokens": 23, "reasoning_tokens": 0},
  "cost_usd": 0.0,
  "error": null
}
```

How to read it:

* `request.body.input` is the whole conversation so far. Codex has no
  memory on the server (`store: false`), so it resends everything on every
  call. That is why input tokens grow each turn of an agent loop.
* `request.body.tools` is the list of tools the model may call, each with a
  JSON schema for its arguments.
* `response.body` was streamed as many small SSE events. The proxy relayed
  them live and rebuilt this single object afterwards.
* `usage` and `cost_usd` are read from the response. If a backend sends no
  usage, they are `null` and the summary says so. Nothing is estimated.

## Qwen findings

The swap works at the proxy level: with only the config changed, every
Codex call went to the local Qwen server and was logged in full. But Codex
did not complete the task with `qwen2.5-coder:7b`. It made one call and
stopped, for two reasons the logs make visible:

1. **Ollama drops Codex's instructions.** Codex sends its system prompt as
   `developer` role messages. Ollama does not pass that role to Qwen, so the
   model never sees them.
2. **Qwen writes tool calls as plain text.** It answers with JSON text inside
   a normal message instead of a structured `function_call` item, so Codex
   treats it as a final answer and ends the loop.

The evidence and numbers are in [docs/WRITEUP.md](docs/WRITEUP.md).

## Tests

```bash
pytest
```

| File | What it proves |
|------|----------------|
| `tests/test_passthrough.py` | Request and response bytes are identical with the proxy in the path, errors are relayed, auth rules, order of calls |
| `tests/test_streaming.py` | SSE chunks are relayed live (not buffered) and unchanged, and are reassembled correctly for both wire formats |
| `tests/test_log_schema.py` | One JSONL file per session, every line matches the schema, nothing truncated, secrets redacted |
| `tests/test_usage_and_cost.py` | Usage extraction, cost math, session summary, honest handling of missing usage or prices |

`tests/mock_backend.py` is a fake OpenAI compatible server that remembers
the exact bytes it received.

## Project layout

```
codex_probe/
  config.py       config model and loading
  recorder.py     ProxyRecorder: start and stop the proxy in a background thread
  server.py       FastAPI app with one catch-all /v1/ route
  forwarder.py    forwards the request, relays the response live, builds the log record
  reassembly.py   rebuilds a full response from SSE events
  usage.py        reads token usage from a response
  pricing.py      loads pricing.json and computes cost
  logger.py       log schema, header redaction, JSONL file, session summary
examples/         quickstart.py and the two backend configs
tests/            mock backend and tests
docs/             WRITEUP.md, sample_session/
pricing.json      prices in USD per 1M tokens, keyed by model
```

## Not done yet

* Optional: a compatibility adapter that renames `developer` to `system` and
  turns Qwen's text tool calls into real ones, so Codex can finish a task
  on the local model.
* The token count gap: OpenAI counted 11,873 input tokens on the first call
  while Ollama counted 4,008 for the same request. The dropped `developer`
  messages account for part of the difference but not all of it, and the
  rest was not investigated.
