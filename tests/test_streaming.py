"""Streaming: chunks are relayed live and unchanged, then reassembled for the log."""

import json

import httpx

from codex_probe.reassembly import (
    parse_sse,
    reassemble,
    reassemble_chat,
    reassemble_responses,
    stream_is_complete,
)
from tests.mock_backend import (
    CHAT_USAGE,
    MESSAGE_ITEM,
    RESPONSES_USAGE,
    TOOL_ITEM,
    chat_stream_events,
    responses_stream_events,
    sse,
)

STREAM_REQUEST = b'{"model":"gpt-test","stream":true,"input":"list files"}'
HEADERS = {"content-type": "application/json"}


# --- relay through the proxy -------------------------------------------------


def test_stream_bytes_are_relayed_unchanged(backend, make_recorder):
    recorder = make_recorder()
    endpoint = recorder.start()
    response = httpx.post(endpoint + "/responses", content=STREAM_REQUEST, headers=HEADERS)
    recorder.stop()

    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.content == b"".join(responses_stream_events())


def test_chunks_arrive_live_not_buffered(backend, make_recorder):
    """The backend pauses mid stream. The first events must already have
    reached the client while the backend is still paused. A proxy that
    buffered the whole stream would hang here and hit the timeout."""
    backend.gate.clear()  # backend will pause after its first two events
    recorder = make_recorder()
    endpoint = recorder.start()

    with httpx.stream(
        "POST", endpoint + "/responses", content=STREAM_REQUEST, headers=HEADERS, timeout=5
    ) as response:
        chunks = response.iter_raw()
        early = next(chunks)
        assert b"response.created" in early       # received while backend is paused
        assert b"response.completed" not in early
        backend.gate.set()                        # let the backend finish
        rest = b"".join(chunks)

    calls = recorder.stop()
    assert b"response.completed" in rest
    assert early + rest == b"".join(responses_stream_events())
    assert calls[0]["first_chunk_ms"] <= calls[0]["latency_ms"]


def test_streamed_responses_call_is_reassembled_in_the_log(backend, make_recorder):
    recorder = make_recorder()
    endpoint = recorder.start()
    httpx.post(endpoint + "/responses", content=STREAM_REQUEST, headers=HEADERS)
    calls = recorder.stop()

    logged = calls[0]["response"]
    assert logged["streamed"] is True
    assert logged["status"] == 200
    assert logged["body"]["output"] == [MESSAGE_ITEM, TOOL_ITEM]
    assert logged["body"]["usage"] == RESPONSES_USAGE


def test_streamed_chat_call_is_reassembled_in_the_log(backend, make_recorder):
    recorder = make_recorder(wire_api="chat")
    endpoint = recorder.start()
    response = httpx.post(endpoint + "/chat/completions", content=STREAM_REQUEST, headers=HEADERS)
    calls = recorder.stop()

    assert response.content == b"".join(chat_stream_events())
    message = calls[0]["response"]["body"]["choices"][0]["message"]
    assert message["content"] == "Let me look."
    assert message["tool_calls"][0]["function"] == {"name": "shell", "arguments": '{"command":["pwd"]}'}


def test_client_disconnect_mid_stream_is_still_logged(backend, make_recorder):
    backend.gate.clear()
    recorder = make_recorder()
    endpoint = recorder.start()
    with httpx.stream(
        "POST", endpoint + "/responses", content=STREAM_REQUEST, headers=HEADERS, timeout=5
    ) as response:
        next(response.iter_raw())
        # leaving the block here closes the connection early
    backend.gate.set()
    calls = recorder.stop()

    assert len(calls) == 1
    assert "disconnected" in calls[0]["error"]
    # The part that did arrive is kept.
    assert calls[0]["response"]["body"]["output"][0]["type"] == "message"


def test_client_closing_after_the_final_event_is_not_an_error(backend, make_recorder):
    """Codex hangs up right after response.completed, while the backend
    still holds the connection open. The call is complete, so no error."""
    backend.may_close.clear()  # backend keeps the stream open after the last event
    recorder = make_recorder()
    endpoint = recorder.start()
    received = b""
    with httpx.stream(
        "POST", endpoint + "/responses", content=STREAM_REQUEST, headers=HEADERS, timeout=5
    ) as response:
        for chunk in response.iter_raw():
            received += chunk
            if b"response.completed" in received:
                break  # close early, like Codex does
    backend.may_close.set()
    calls = recorder.stop()

    assert calls[0]["error"] is None
    assert calls[0]["response"]["body"]["output"] == [MESSAGE_ITEM, TOOL_ITEM]
    assert calls[0]["usage"]["input_tokens"] == 1200


# --- reassembly unit tests ---------------------------------------------------


def test_parse_sse_handles_event_names_comments_and_done():
    raw = b": keep-alive\n\n" + sse({"a": 1}, event="thing") + sse("[DONE]")
    assert parse_sse(raw) == [
        {"event": "thing", "data": {"a": 1}},
        {"event": None, "data": "[DONE]"},
    ]


def test_parse_sse_handles_crlf_line_endings():
    raw = b'event: x\r\ndata: {"a": 1}\r\n\r\ndata: {"b": 2}\r\n\r\n'
    assert [e["data"] for e in parse_sse(raw)] == [{"a": 1}, {"b": 2}]


def test_responses_reassembly_matches_non_streamed_shape():
    body = reassemble(b"".join(responses_stream_events()))
    assert body["status"] == "completed"
    assert body["output"][0]["content"][0]["text"] == "I will list the files ✓"
    assert json.loads(body["output"][1]["arguments"]) == {"command": ["ls", "-la"]}
    assert body["output"][1]["name"] == "shell"


def test_responses_reassembly_falls_back_to_deltas_when_stream_is_cut_off():
    """Drop every "done" and "completed" event, as if the stream died early.
    Text and tool arguments must still be rebuilt from the deltas."""
    events = [
        e for e in parse_sse(b"".join(responses_stream_events()))
        if e["event"] not in ("response.output_item.done", "response.completed")
    ]
    body = reassemble_responses(events)
    assert body["output"][0]["content"] == [{"type": "output_text", "text": "I will list the files ✓"}]
    assert body["output"][1]["arguments"] == '{"command":["ls","-la"]}'
    assert "usage" not in body


def test_responses_reassembly_fills_output_when_completed_event_has_none():
    events = parse_sse(b"".join(responses_stream_events()))
    events[-1]["data"]["response"]["output"] = []
    body = reassemble_responses(events)
    assert body["output"] == [MESSAGE_ITEM, TOOL_ITEM]
    assert body["usage"] == RESPONSES_USAGE


def test_chat_reassembly_joins_content_tool_calls_and_usage():
    body = reassemble_chat(parse_sse(b"".join(chat_stream_events())))
    choice = body["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] == "Let me look."
    assert choice["message"]["tool_calls"] == [
        {"id": "call_xyz", "type": "function",
         "function": {"name": "shell", "arguments": '{"command":["pwd"]}'}}
    ]
    assert body["usage"] == CHAT_USAGE
    assert body["model"] == "qwen-test"


def test_unknown_stream_format_keeps_all_events():
    body = reassemble(sse({"something": "else"}))
    assert body == {"events": [{"event": None, "data": {"something": "else"}}]}


def test_stream_is_complete_detects_the_final_event():
    full = b"".join(responses_stream_events())
    cut = b"".join(responses_stream_events()[:-1])
    assert stream_is_complete(full) is True
    assert stream_is_complete(cut) is False
    assert stream_is_complete(b"".join(chat_stream_events())) is True
    assert stream_is_complete(b"".join(chat_stream_events()[:-1])) is False
