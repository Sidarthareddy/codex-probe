# CodexProbe write-up

This covers two real runs of the same task through the recorder: one against
OpenAI (`gpt-5.4-mini`) and one against a local Qwen model served by Ollama.

## Summary

* The recorder works: with the proxy in the path, every Codex LLM call is
  forwarded unchanged and logged in full, including streamed responses.
* The backend swap works: changing only the recorder config sent Codex's
  calls to OpenAI or to a local `qwen2.5-coder:7b`. The logs prove which.
* On OpenAI, Codex completed the task in 5 LLM calls for about $0.015.
* On Qwen, Codex did **not** complete the task. It made one LLM call and
  stopped, and `hello.py` was unchanged. The logs show two causes: Ollama
  drops Codex's `developer` messages, and Qwen writes tool calls as plain
  text. The spec lists this kind of result as an acceptable finding.

## Setup used

Common to both runs:

* MacBook Pro, Apple Silicon, 32 GB RAM
* Codex CLI 0.169.0, `codex exec`, sandbox `workspace-write`
* Task: "Add a one line docstring to every function in hello.py. Do not change anything else."
* `hello.py` has three small functions (`add`, `greet`, `is_even`) and is reset before each run
* Command: `python examples/quickstart.py --config <config> --model <model>`

| | OpenAI run | Qwen run |
|---|---|---|
| Date | 2026-11-14 | 2026-11-13 |
| Recorder config | `examples/config.openai.json` | `examples/config.qwen.json` |
| Backend | `https://api.openai.com/v1` | Ollama 0.40.16 at `http://localhost:11434/v1`, started with `OLLAMA_CONTEXT_LENGTH=32768 ollama serve` |
| Model | `gpt-5.4-mini` (served as `gpt-5.4-mini-2026-03-17`) | `qwen2.5-coder:7b` (4.7 GB) |
| Session log | `docs/sample_session/2026-11-14T14-34-26_aad9.jsonl` | `docs/sample_session/2026-11-13T15-28-24_71bb.jsonl` |

The Codex config fragment was the same for both runs except for the `model` line.

## OpenAI vs Qwen

| | OpenAI `gpt-5.4-mini` | Qwen `qwen2.5-coder:7b` |
|---|---:|---:|
| Task completed | yes, all 3 docstrings added, nothing else changed | no, file unchanged |
| LLM calls | 5 | 1 |
| Calls that returned a structured tool call | 4 | 0 |
| Input tokens, first call | 11,873 | 4,008 |
| Input tokens, whole session | 60,998 | 4,008 |
| of which cached | 48,256 (79%) | 0 |
| Output tokens, whole session | 489 | 23 |
| API cost | $0.0154 | $0 |
| Total time spent in LLM calls | 8.1 s | 7.8 s |
| Average latency per call | 1.6 s | 7.8 s |
| Time to first chunk | 0.4 s to 1.5 s | 7.0 s |

What the comparison shows:

* **Success.** The OpenAI model used tools correctly on every turn that
  needed one. Qwen never produced a tool call Codex could run.
* **Same request, very different token counts.** The first request is the
  same size in both runs (about 41,500 characters). OpenAI counted 11,873
  input tokens and Ollama 4,008. Part of the gap is that Ollama dropped the
  two `developer` messages (see below). The rest was not investigated. The
  two backends use different tokenizers, and Ollama may also handle the
  `namespace` and `web_search` tools differently.
* **Cost.** $0.0154 for a completed task versus $0 for no result. The local
  model is free per call but did no useful work here.
* **Latency.** One local call took about as long as all five OpenAI calls
  together. Almost all of the local time (7.0 of 7.8 seconds) was spent
  reading the prompt before the first token appeared.

Generation speed (output tokens per second) is in the session summaries but
is left out of the comparison. For Qwen it is about 30 tokens/s. For OpenAI
the events often arrive in a burst, so dividing tokens by the time between
the first and last chunk gives numbers that are not meaningful.

## The OpenAI session, call by call

| call | what the model did | input items | input tokens | cached | output tokens | latency | cost |
|-----:|--------------------|------------:|-------------:|-------:|--------------:|--------:|-----:|
| 0 | says what it will do, calls `exec_command` (`rg --files ...`) | 4 | 11,873 | 0 | 111 | 1.8 s | $0.00940 |
| 1 | `rg` is not installed, so it calls `exec_command` (`find ...`) | 7 | 12,039 | 11,776 | 90 | 1.4 s | $0.00149 |
| 2 | calls `exec_command` (`sed -n '1,240p' hello.py`) to read the file | 10 | 12,179 | 12,032 | 62 | 1.4 s | $0.00129 |
| 3 | calls `exec_command` with an `apply_patch` that adds the docstrings | 12 | 12,329 | 12,160 | 200 | 2.4 s | $0.00194 |
| 4 | final text answer, no tool call, so the loop ends | 15 | 12,578 | 12,288 | 26 | 1.1 s | $0.00126 |
| total | | | 60,998 | 48,256 | 489 | 8.1 s | $0.01538 |

Prices used: `gpt-5.4-mini` at $0.75 input, $0.075 cached input, $4.50
output per 1M tokens (OpenAI pricing page, checked 2026-11-14, stored in
`pricing.json`).

### Why input tokens grow every turn

Codex sends `store: false`, so the backend keeps no memory of the
conversation. Every request must carry the whole history. The "input items"
column shows it: call 0 sends 4 items (two developer messages, the
environment block, the task). Each later call resends all of those plus what
happened since: the assistant's message, its `function_call`, and the
`function_call_output` holding the command's result. So the input grows
from 11,873 to 12,578 tokens over five calls, and the session pays for
60,998 input tokens in total even though only about 12,600 distinct tokens
exist.

### What caching does to the cost

The first call has nothing cached and costs $0.0094, which is 61% of the
whole session. From call 1 on, OpenAI recognizes the repeated start of the
prompt (Codex sends a `prompt_cache_key`) and bills about 98% of the input
at the cached rate, one tenth of the normal price. Those calls cost between
$0.0013 and $0.0019 each.

Without caching, the same tokens would have cost $0.0479. Caching cut the
session cost by 68%.

### Run to run variation

The task was run against OpenAI twice. The first run finished in 4 calls
($0.0141), the second in 5 ($0.0154). The model chose slightly different
commands each time. The first run exposed a logging bug (a normal early
close by Codex was recorded as an error), which was fixed before the second
run. Only the second run is kept in `docs/sample_session/` and used above.

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

The first request is the same in both runs apart from the model name. From
the logged call in session `2026-11-13T15-28-24_71bb`:

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

## Proof of which backend served each run

For the OpenAI run, every log line has
`"backend": {"name": "openai", "base_url": "https://api.openai.com/v1"}`,
and the response bodies carry OpenAI's dated model name
`gpt-5.4-mini-2026-03-17`. For the Qwen run:

* The log line has `"backend": {"name": "ollama_qwen", "base_url": "http://localhost:11434/v1"}`.
* The response body is Ollama's: `"model": "qwen2.5-coder:7b"`, with Ollama style ids.
* Ollama's own server log shows the matching request (`task.n_tokens = 4008`,
  the same number as `usage.input_tokens` in the recorder log).
* There was no OpenAI key on the machine during the run.

## Why Codex stopped after one call on Qwen

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

## Limits of this write-up

* One small task, one cheap OpenAI model, one local model, and one or two
  runs each. This is an illustration, not a benchmark.
* Other Qwen models or Ollama versions may behave differently.
* Cost is computed from the token counts in the responses and the listed
  standard prices. It was not checked against the OpenAI billing page.
* The token count gap between the two backends is only partly explained.

## Possible follow-up (bonus)

An opt-in compatibility adapter could rename `developer` to `system` in the
request and convert Qwen's text JSON into real `function_call` items in the
response, logging both the original and the translated request. It would
change what the model and Codex see and would have to buffer the response,
so it must stay out of the default passthrough path.
