"""A fake OpenAI compatible server for tests.

It remembers exactly what it received (raw bytes and headers) so tests can
check that the proxy forwarded the request unchanged. It answers with fixed
bytes so tests can check the response came back unchanged.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import Response

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


class MockBackend:
    def __init__(self) -> None:
        # Each entry: {"method", "path", "query", "headers", "body"}
        self.received: list[dict] = []
        self.app = FastAPI()
        self._server: BackgroundServer | None = None
        self.base_url = ""

        async def remember(request: Request) -> None:
            self.received.append(
                {
                    "method": request.method,
                    "path": request.url.path,
                    "query": request.url.query,
                    "headers": dict(request.headers),
                    "body": await request.body(),
                }
            )

        @self.app.post("/v1/responses")
        async def responses(request: Request):
            await remember(request)
            return Response(
                content=RESPONSES_BODY,
                media_type="application/json",
                headers={"x-request-id": "req_mock_123"},
            )

        @self.app.post("/v1/chat/completions")
        async def chat(request: Request):
            await remember(request)
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
        if self._server:
            self._server.stop()
