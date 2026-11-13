# CodexProbe write-up

This covers the local Qwen run. The OpenAI run has not been done yet, so
there is no OpenAI vs Qwen comparison here.

## Summary

* The recorder works: with the proxy in the path, every Codex LLM call is
  forwarded unchanged and logged in full, including streamed responses.
* The backend swap works: changing only the recorder config sent Codex's
  calls to a local `qwen2.5-coder:7b` served by Ollama. The logs prove it.
* Codex did **not** complete the task on Qwen. It made one LLM call and
  stopped, and `hello.py` was unchanged. The logs show two causes: Ollama
  drops Codex's `developer` messages, and Qwen writes tool calls as plain
  text. The spec lists this kind of result as an acceptable finding.

## Setup used (2026-11-13)

* MacBook Pro, Apple Silicon, 32 GB RAM
* Codex CLI 0.169.0, `codex exec`, sandbox `workspace-write`
* Ollama 0.40.16 serving `qwen2.5-coder:7b` (4.7 GB), started with
  `OLLAMA_CONTEXT_LENGTH=32768 ollama serve`
* Recorder config: `examples/config.qwen.json`
* Task: "Add a one line docstring to every function in hello.py. Do not change anything else."
* Command: `python examples/quickstart.py --config examples/config.qwen.json --model qwen2.5-coder:7b`
* The recorded session is saved in `docs/sample_session/`

## How the proxy relays a stream without changing Codex's behavior

Codex asks for a streamed answer (`"stream": true`). The backend replies
with SSE (Server-Sent Events): one long HTTP response made of many small
text events, each a piece of the answer.

The proxy handles this in `forwarder.py`:

1. It sends Codex's request body to the backend as the exact bytes it
   received. It never parses and rewrites the JSON on the way through.
2. It reads the backend's response in raw chunks. Each chunk is handed to
   Codex immediately (`yield chunk` in an async generator) and a copy is
   appended to a list. The proxy never waits for the whole response.
3. When the stream ends, the copied chunks are joined, parsed as SSE, and
   rebuilt into one response object for the log (`reassembly.py`).

So Codex receives the same bytes, in the same order, at the same pace as it
would without the proxy. Everything the proxy adds (parsing, reassembly,
usage, cost) happens on the copy, after the bytes have already been relayed.

The tests check both halves: the bytes that come out of the proxy equal the
bytes the backend sent, and when the mock backend pauses in the middle of a
stream, the first events have already reached the client.

## Responses API vs Chat Completions

Codex speaks the Responses API. Many local servers were built around Chat
Completions. The recorder can log and reassemble both.

| | Responses API | Chat Completions |
|---|---|---|
| Endpoint | `POST /v1/responses` | `POST /v1/chat/completions` |
| System prompt | `instructions` field, or `developer` / `system` messages in `input` | a `system` message in `messages` |
| Conversation | `input`: a list of typed items (messages, tool calls, tool results) | `messages`: a list of role and content pairs |
| Tool call from the model | its own output item: `{"type": "function_call", "name", "arguments", "call_id"}` | `tool_calls` list inside the assistant message |
| Tool result sent back | an input item `{"type": "function_call_output", "call_id", "output"}` | a message with role `tool` |
| Streaming | named events (`response.output_text.delta`, `response.output_item.done`, ...) ending with `response.completed`, which carries the full response and usage | unnamed `chat.completion.chunk` objects with a `delta`, ending with `data: [DONE]` |
| Usage fields | `input_tokens`, `output_tokens`, `input_tokens_details.cached_tokens` | `prompt_tokens`, `completion_tokens`, `prompt_tokens_details.cached_tokens` |

## Does Ollama need a wire format adapter?

No. Ollama 0.40.16 accepts `POST /v1/responses` directly, both streaming and
non streaming, and reports usage in the same fields as OpenAI. So the proxy
forwards Codex's request to Ollama byte for byte and `translated_request`
stays `null` in the log.

## What Codex actually sends

From the logged call in session `2026-11-13T15-28-24_71bb`:

* `POST /v1/responses`, 40,779 bytes, `stream: true`, `store: false`
* No `instructions` field. Codex puts its system prompt in `input`, as two
  messages with role `developer` (about 17,700 and 2,900 characters),
  followed by two `user` messages: an `<environment_context>` block and the
  task prompt.
* `tools`: 9 entries. Seven plain functions (`exec_command`, `write_stdin`,
  `request_user_input`, `view_image`, `get_goal`, `create_goal`,
  `update_goal`), one `namespace` tool (`multi_agent_v1`) and one built in
  `web_search` tool.
* Headers include `session-id` and `thread-id` (the Codex session), which the
  log stores as `client_session_id`. `authorization` is logged as `[REDACTED]`.

## Proof the calls hit the local Qwen server

* The log line has `"backend": {"name": "ollama_qwen", "base_url": "http://localhost:11434/v1"}`.
* The response body is Ollama's: `"model": "qwen2.5-coder:7b"`, with Ollama style ids.
* Ollama's own server log shows the matching request (`task.n_tokens = 4008`,
  the same number as `usage.input_tokens` in the recorder log).
* There was no OpenAI key on the machine during the run.

## Why Codex stopped after one call

### 1. Ollama drops `developer` role messages

Codex sent about 41,500 characters, but Ollama counted only 4,008 input
tokens. To find out what was missing, the logged request was replayed
straight to Ollama with parts removed:

| Variant of the logged request      | input tokens |
|------------------------------------|-------------:|
| exactly as Codex sent it           | 4,008 |
| developer messages removed         | 4,008 |
| tools removed                      |   362 |
| developer role renamed to system   | 8,177 |

Removing the developer messages changes nothing, so they never reached the
model. Qwen saw the tool list and the user messages, but none of Codex's
instructions on how to behave as an agent.

This is not context truncation. Ollama logged `n_ctx = 32768` and
`truncated = 0`. In a small separate test, a short `developer` message was
counted in the tokens but the model did not follow it, while the same text
as a `system` message or in the `instructions` field was followed.

### 2. Qwen writes tool calls as plain text

In the recorded run Qwen answered with this text, inside a normal assistant
`message` item:

    {"name": "exec_command", "arguments": {"cmd": "echo 'Hello, world!'"}}

A real tool call is a separate output item of type `function_call`. Codex
only runs commands for `function_call` items, so it showed this text as the
final answer and ended the loop. (The command is also wrong for the task,
which fits with the model never seeing its instructions.)

This was the same in every direct trial against Ollama:

| Endpoint | Prompt | Structured tool calls |
|----------|--------|----------------------:|
| `/v1/responses` | one tool, "run ls using the tool" | 0 of 3 |
| `/v1/chat/completions` | one tool, "run ls using the tool" | 0 of 3 |
| `/v1/chat/completions` | Codex's full prompt as a system message | 0 of 3 |
| `/v1/chat/completions` | a short system prompt plus the task | 0 of 3 |

In most of these the model did write tool call JSON, only as text. Because
Chat Completions has the same problem, a Responses to Chat adapter would
not fix it.

## Numbers from the Qwen run

| call | input tokens | output tokens | total latency | time to first chunk | generation speed |
|-----:|-------------:|--------------:|--------------:|--------------------:|-----------------:|
| 0    | 4,008        | 23            | 7.8 s         | 7.0 s               | 30 tokens/s      |

* API cost: $0 (local model, listed with zero prices in `pricing.json`).
* 90% of the latency is Ollama reading the 4,008 token prompt before the
  first token comes out. Generating the 23 output tokens took under a second.
* Cached input tokens: 0 on this call.

## Why input tokens grow every turn (not observed yet)

An agent loop works like this: the model asks for a tool call, Codex runs
it, then Codex sends a new request that contains the whole conversation so
far plus the tool result. Each request is therefore longer than the one
before, and the same early tokens are paid for again on every call. Prompt
caching exists to make those repeated tokens cheaper.

The summary's per call table is built to show this growth. The Qwen run
could not show it because the loop ended after one call. It should be
visible in the OpenAI run.

## Limits of this write-up

* One model (`qwen2.5-coder:7b`), one task, and a small number of trials.
  Other Qwen models or Ollama versions may behave differently.
* The OpenAI run is not done, so the claims about OpenAI's side of the wire
  format are tested only against the mock backend, not a real capture.
* `pricing.json` has no OpenAI prices yet.

## Possible follow-up (bonus)

An opt-in compatibility adapter could rename `developer` to `system` in the
request and convert Qwen's text JSON into real `function_call` items in the
response, logging both the original and the translated request. It would
change what the model and Codex see and would have to buffer the response,
so it must stay out of the default passthrough path.
