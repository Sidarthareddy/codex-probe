# Sample sessions

Two real recorded sessions of the same task, one per backend.

| File | Backend | Calls |
|------|---------|------:|
| `2026-11-14T14-34-26_aad9.jsonl` | OpenAI `gpt-5.4-mini` | 5 |
| `2026-11-13T15-28-24_71bb.jsonl` | local `qwen2.5-coder:7b` via Ollama | 1 |

Each `.jsonl` file has one LLM call per line. The matching `.summary.json`
has the token totals, cost and latency.

These copies were edited after recording: the local username and folder
paths were replaced with `/path/to/project`, and the Codex `installation_id`
was replaced with zeros. Nothing else was changed. One
side effect is that the logged `content-length` header no longer matches
the length of the logged request body.
