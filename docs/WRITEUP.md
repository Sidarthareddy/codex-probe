# CodexProbe write-up

Work in progress. The OpenAI section is filled in after the OpenAI run.

## Setup used for the Qwen run (2026-11-13)

* Codex CLI 0.169.0, `codex exec`, sandbox `workspace-write`
* Ollama 0.40.16 serving `qwen2.5-coder:7b` (4.7 GB), started with
  `OLLAMA_CONTEXT_LENGTH=32768 ollama serve`
* Recorder config: `examples/config.qwen.json`
* Task: "Add a one line docstring to every function in hello.py. Do not change anything else."

## Does Ollama need a wire format adapter?

No. Ollama 0.40.16 accepts `POST /v1/responses` directly, both streaming and
non streaming, and reports usage in the same fields as OpenAI. So the proxy
forwards Codex's Responses API request to Ollama byte for byte and
`translated_request` stays `null`.

## What a captured Codex call looks like

One call from session `2026-11-13T15-28-24_71bb`:

* `POST /v1/responses`, 40,779 bytes, `stream: true`, `store: false`
* No `instructions` field. Codex puts its system prompt in `input` instead,
  as two messages with role `developer` (about 17,700 and 2,900 characters),
  followed by two `user` messages: an `<environment_context>` block and the
  task prompt.
* `tools`: 9 entries. Six plain functions (`exec_command`, `write_stdin`,
  `request_user_input`, `view_image`, `get_goal`, `create_goal`,
  `update_goal`), one `namespace` tool (`multi_agent_v1`) and one built in
  `web_search` tool.
* Headers include `session-id` and `thread-id` (the Codex session), which the
  log stores as `client_session_id`. `authorization` is logged as `[REDACTED]`.

## Proof the calls hit the local Qwen server

* Every log line has `"backend": {"name": "ollama_qwen", "base_url": "http://localhost:11434/v1"}`.
* The response body is Ollama's: `"model": "qwen2.5-coder:7b"`, ids like `resp_837943`.
* Ollama's own server log shows the matching request (`task.n_tokens = 4008`).
* No OpenAI key existed on the machine during the run.

## Result: Codex did not complete the task with Qwen 7B

Codex made exactly one LLM call and stopped. `hello.py` was not changed.
The logs show two separate causes.

### 1. Ollama drops `developer` role messages

Codex sent about 41,500 characters, but Ollama counted only 4,008 input
tokens. Replaying the logged request straight to Ollama with parts removed:

| Variant of the logged request            | input tokens |
|------------------------------------------|-------------:|
| exactly as Codex sent it                 | 4,008 |
| developer messages removed               | 4,008 |
| tools removed                            |   362 |
| developer role renamed to system         | 8,177 |

Removing the developer messages changes nothing, so they never reached the
model. Qwen saw the tool list and the user messages, but none of Codex's
instructions on how to behave as an agent. (This is not context truncation:
Ollama logged `n_ctx = 32768` and `truncated = 0`.)

### 2. Qwen writes tool calls as plain text

Qwen answers with text such as

    {"name": "exec_command", "arguments": {"cmd": "echo 'Hello, world!'"}}

inside a normal assistant `message` item. A real tool call must be a
separate output item of type `function_call`. Codex only runs commands for
`function_call` items, so it treats this text as the final answer and ends
the loop.

This happened in every trial: 3 of 3 through `/v1/responses` with a single
simple tool, and 9 of 9 through `/v1/chat/completions` (`tool_calls` was
always `null`). Because Chat Completions has the same problem, a
Responses to Chat adapter would not fix it.

## Numbers from the Qwen run

| call | input tokens | output tokens | latency | first chunk |
|-----:|-------------:|--------------:|--------:|------------:|
| 0    | 4,008        | 23            | 7.8 s   | see log     |

API cost is $0 (local model). Most of the latency is Ollama reading the
4,008 token prompt, not generating the 23 output tokens.
