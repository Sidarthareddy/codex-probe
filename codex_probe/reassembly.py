"""Rebuild a full response from a stream of SSE chunks.

SSE (Server-Sent Events) is how the backend streams its answer. Instead of
one JSON body at the end, it sends many small text "events" over one open
HTTP response, each one a little piece of the answer:

    event: response.output_text.delta
    data: {"type":"response.output_text.delta","delta":"Hel"}

    event: response.output_text.delta
    data: {"type":"response.output_text.delta","delta":"lo"}

Events are separated by a blank line. The proxy relays these bytes to Codex
untouched. This module runs afterwards, on a copy, to glue the pieces back
into one readable response for the log.

The two wire formats stream differently:
  * Responses API: named events ("response.output_text.delta", ...) and a
    final "response.completed" event that carries the whole response.
  * Chat Completions: unnamed "chat.completion.chunk" objects holding a
    "delta", ended by a literal "data: [DONE]" line.
"""

from __future__ import annotations

import json


def parse_sse(raw: bytes) -> list[dict]:
    """Split raw SSE bytes into a list of events.

    Each event becomes {"event": <name or None>, "data": <parsed JSON or text>}.
    Parsing happens on the complete byte string, so it does not matter how
    the network happened to split the stream into chunks.
    """
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    events = []
    for block in text.split("\n\n"):
        name = None
        data_lines = []
        for line in block.split("\n"):
            if line.startswith(":"):
                continue  # a comment line, used by servers as a keep-alive
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].lstrip(" "))
        if not data_lines:
            continue
        data = "\n".join(data_lines)
        try:
            data = json.loads(data)
        except ValueError:
            pass  # not JSON, for example the "[DONE]" marker
        events.append({"event": name, "data": data})
    return events


def reassemble_responses(events: list[dict]) -> dict:
    """Rebuild a Responses API response object from its stream events.

    The output is a list of "items". An item is either an assistant message
    or a tool call (type "function_call"). Each item is announced with
    "response.output_item.added", filled in by delta events, and finished
    with "response.output_item.done", which carries the complete item.
    """
    items: dict[int, dict] = {}       # output_index -> item
    finished: set[int] = set()        # items we received in complete form
    text_parts: dict[tuple[int, int], str] = {}   # (output_index, content_index) -> text
    argument_parts: dict[int, str] = {}           # output_index -> tool call arguments
    first_snapshot = None             # from response.created
    final_snapshot = None             # from response.completed (or failed / incomplete)

    for event in events:
        data = event["data"]
        if not isinstance(data, dict):
            continue
        kind = data.get("type", "")
        index = data.get("output_index", 0)

        if kind == "response.created":
            first_snapshot = data.get("response")
        elif kind == "response.output_item.added":
            items[index] = data.get("item", {})
        elif kind == "response.output_text.delta":
            key = (index, data.get("content_index", 0))
            text_parts[key] = text_parts.get(key, "") + data.get("delta", "")
        elif kind == "response.function_call_arguments.delta":
            argument_parts[index] = argument_parts.get(index, "") + data.get("delta", "")
        elif kind == "response.output_item.done":
            items[index] = data.get("item", {})
            finished.add(index)
        elif kind in ("response.completed", "response.incomplete", "response.failed"):
            final_snapshot = data.get("response")

    # For items that never got their "done" event (the stream was cut off),
    # fall back to the text and arguments collected from the deltas.
    for index, item in items.items():
        if index in finished:
            continue
        if index in argument_parts:
            item["arguments"] = argument_parts[index]
        texts = [text for (i, _), text in sorted(text_parts.items()) if i == index]
        if texts:
            item["content"] = [{"type": "output_text", "text": text} for text in texts]

    output = [items[index] for index in sorted(items)]

    # Start from the final snapshot because it has the status and the usage.
    body = dict(final_snapshot or first_snapshot or {})
    if not body.get("output"):
        body["output"] = output
    return body


def reassemble_chat(events: list[dict]) -> dict:
    """Rebuild a Chat Completions response from its stream chunks.

    Every chunk has choices[].delta holding a small piece: a bit of content,
    or a bit of a tool call. Tool call arguments arrive as string fragments
    that must be joined in order, matched up by the tool call's "index".
    """
    body: dict = {"object": "chat.completion"}
    choices: dict[int, dict] = {}

    for event in events:
        chunk = event["data"]
        if not isinstance(chunk, dict):
            continue  # the "[DONE]" marker
        for key in ("id", "created", "model", "system_fingerprint"):
            if key in chunk and key not in body:
                body[key] = chunk[key]
        if chunk.get("usage"):
            body["usage"] = chunk["usage"]

        for choice in chunk.get("choices", []):
            slot = choices.setdefault(
                choice.get("index", 0),
                {"role": "assistant", "content": "", "tool_calls": {}, "finish_reason": None},
            )
            delta = choice.get("delta") or {}
            if delta.get("role"):
                slot["role"] = delta["role"]
            if delta.get("content"):
                slot["content"] += delta["content"]
            for call_delta in delta.get("tool_calls") or []:
                call = slot["tool_calls"].setdefault(
                    call_delta.get("index", 0),
                    {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if call_delta.get("id"):
                    call["id"] = call_delta["id"]
                function = call_delta.get("function") or {}
                if function.get("name"):
                    call["function"]["name"] += function["name"]
                if function.get("arguments"):
                    call["function"]["arguments"] += function["arguments"]
            if choice.get("finish_reason"):
                slot["finish_reason"] = choice["finish_reason"]

    body["choices"] = []
    for index in sorted(choices):
        slot = choices[index]
        message = {"role": slot["role"], "content": slot["content"] or None}
        if slot["tool_calls"]:
            message["tool_calls"] = [slot["tool_calls"][i] for i in sorted(slot["tool_calls"])]
        body["choices"].append(
            {"index": index, "message": message, "finish_reason": slot["finish_reason"]}
        )
    return body


def reassemble(raw: bytes) -> dict:
    """Parse an SSE byte stream and rebuild the full response.

    The format is detected from the events themselves, not from the backend
    config, so this works for any backend.
    """
    events = parse_sse(raw)
    for event in events:
        data = event["data"]
        if not isinstance(data, dict):
            continue
        if str(data.get("type", "")).startswith("response."):
            return reassemble_responses(events)
        if data.get("object") == "chat.completion.chunk":
            return reassemble_chat(events)
    # Unknown format: keep every event so nothing is lost.
    return {"events": events}
