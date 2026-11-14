"""A fake OpenAI compatible server for tests.

It remembers exactly what it received (raw bytes and headers) so tests can
check that the proxy forwarded the request unchanged. It answers with fixed
bytes so tests can check the response came back unchanged.

If the request body has "stream": true it answers with an SSE stream,
otherwise with a plain JSON body.
"""

from __future__ import annotations

import asyncio
import json
import threading

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from codex_probe.recorder import BackgroundServer

# Odd spacing and a non ASCII character on purpose. If the proxy parsed and
# re-serialized JSON, these bytes would come out different.
RESPONSES_BODY = (
    b'{"id":"resp_mock_1",  "object":"response","status":"completed",'
    b'"output":[{"type":"message","role":"assistant",'
    b'"content":[{"type":"output_text","text":"caf\xc3\xa9 ok"}]}],'
    b'"usage":{"input_tokens":12,"output_tokens":3,"total_tokens":15}}'
)

CHAT_BODY = (
    b'{"id":"chatcmpl_mock_1","object":"chat.completion",'
    b'"choices":[{"index":0,"message":{"role":"assistant","content":"hi"},'
    b'"finish_reason":"stop"}],'
    b'"usage":{"prompt_tokens":5,"completion_tokens":1,"total_tokens":6}}'
)

ERROR_BODY = b'{"error":{"message":"Rate limit reached","type":"rate_limit_error"}}'


def sse(data, event=None) -> bytes:
    """Format one SSE event: optional "event:" line, a "data:" line, blank line."""
    payload = data if isinstance(data, str) else json.dumps(data)
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {payload}\n\n".encode()


# --- a Responses API stream: one text message, then one tool call -------------

MESSAGE_ITEM = {
    "type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
    "content": [{"type": "output_text", "text": "I will list the files ✓"}],
}
TOOL_ITEM = {
    "type": "function_call", "id": "fc_1", "call_id": "call_abc", "name": "shell",
    "status": "completed", "arguments": '{"command":["ls","-la"]}',
}
RESPONSES_USAGE = {
    "input_tokens": 1200, "output_tokens": 45, "total_tokens": 1245,
    "input_tokens_details": {"cached_tokens": 1000},
    "output_tokens_details": {"reasoning_tokens": 20},
}


def responses_stream_events(include_usage=True) -> list[bytes]:
    def ev(kind, **fields):
        return sse({"type": kind, **fields}, event=kind)

    completed = {
        "id": "resp_stream_1", "object": "response", "status": "completed",
        "model": "gpt-test", "output": [MESSAGE_ITEM, TOOL_ITEM],
    }
    if include_usage:
        completed["usage"] = RESPONSES_USAGE
    return [
        ev("response.created", response={"id": "resp_stream_1", "status": "in_progress", "output": []}),
        ev("response.output_item.added", output_index=0,
           item={"type": "message", "id": "msg_1", "role": "assistant", "content": []}),
        ev("response.output_text.delta", output_index=0, content_index=0, delta="I will "),
        ev("response.output_text.delta", output_index=0, content_index=0, delta="list the "),
        ev("response.output_text.delta", output_index=0, content_index=0, delta="files ✓"),
        ev("response.output_item.done", output_index=0, item=MESSAGE_ITEM),
        ev("response.output_item.added", output_index=1,
           item={"type": "function_call", "id": "fc_1", "call_id": "call_abc", "name": "shell", "arguments": ""}),
        ev("response.function_call_arguments.delta", output_index=1, delta='{"command":'),
        ev("response.function_call_arguments.delta", output_index=1, delta='["ls","-la"]}'),
        ev("response.output_item.done", output_index=1, item=TOOL_ITEM),
        ev("response.completed", response=completed),
    ]


# --- a Chat Completions stream: text, then one tool call, then usage ----------

CHAT_USAGE = {
    "prompt_tokens": 800, "completion_tokens": 30, "total_tokens": 830,
    "prompt_tokens_details": {"cached_tokens": 0},
}


def chat_stream_events(include_usage=True) -> list[bytes]:
    def chunk(delta, finish=None, **extra):
        return sse({
            "id": "chatcmpl_stream_1", "object": "chat.completion.chunk", "created": 1,
            "model": "qwen-test",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra,
        })

    events = [
        chunk({"role": "assistant", "content": ""}),
        chunk({"content": "Let me "}),
        chunk({"content": "look."}),
        chunk({"tool_calls": [{"index": 0, "id": "call_xyz", "type": "function",
                               "function": {"name": "shell", "arguments": ""}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"command":'}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": '["pwd"]}'}}]}),
        chunk({}, finish="tool_calls"),
    ]
    if include_usage:
        events.append(sse({"id": "chatcmpl_stream_1", "object": "chat.completion.chunk",
                           "created": 1, "model": "qwen-test", "choices": [], "usage": CHAT_USAGE}))
    events.append(sse("[DONE]"))
    return events


class MockBackend:
    def __init__(self) -> None:
        # Each entry: {"method", "path", "query", "headers", "body"}
        self.received: list[dict] = []
        self.app = FastAPI()
        self._server: BackgroundServer | None = None
        self.base_url = ""
        # While this gate is closed, a stream pauses after its first two
        # events. Tests use it to prove the proxy relays chunks live.
        self.gate = threading.Event()
        self.gate.set()
        # While this is closed, a stream stays open after its last event,
        # like a backend that is slow to close the connection.
        self.may_close = threading.Event()
        self.may_close.set()
        # Set to False to make streams leave out usage data.
        self.include_usage = True

        async def remember(request: Request) -> dict:
            body = await request.body()
            self.received.append(
                {
                    "method": request.method,
                    "path": request.url.path,
                    "query": request.url.query,
                    "headers": dict(request.headers),
                    "body": body,
                }
            )
            try:
                return json.loads(body)
            except ValueError:
                return {}

        def stream(events: list[bytes]) -> StreamingResponse:
            async def generate():
                for position, event in enumerate(events):
                    if position == 2:
                        while not self.gate.is_set():
                            await asyncio.sleep(0.01)
                    yield event
                while not self.may_close.is_set():
                    await asyncio.sleep(0.01)

            return StreamingResponse(generate(), media_type="text/event-stream")

        @self.app.post("/v1/responses")
        async def responses(request: Request):
            payload = await remember(request)
            if payload.get("stream"):
                return stream(responses_stream_events(self.include_usage))
            return Response(
                content=RESPONSES_BODY,
                media_type="application/json",
                headers={"x-request-id": "req_mock_123"},
            )

        @self.app.post("/v1/chat/completions")
        async def chat(request: Request):
            payload = await remember(request)
            if payload.get("stream"):
                return stream(chat_stream_events(self.include_usage))
            return Response(content=CHAT_BODY, media_type="application/json")

        @self.app.post("/v1/always_429")
        async def rate_limited(request: Request):
            await remember(request)
            return Response(content=ERROR_BODY, status_code=429, media_type="application/json")

        @self.app.get("/v1/models")
        async def models(request: Request):
            await remember(request)
            return {"object": "list", "data": [{"id": "mock-model"}]}

    def start(self) -> str:
        self._server = BackgroundServer(self.app, "127.0.0.1", 0)
        self._server.start()
        self.base_url = f"http://127.0.0.1:{self._server.port}/v1"
        return self.base_url

    def stop(self) -> None:
        self.gate.set()
        self.may_close.set()
        if self._server:
            self._server.stop()
