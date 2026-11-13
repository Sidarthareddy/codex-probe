"""The FastAPI app that Codex talks to.

It has a single catch-all route: anything under /v1/ is handed to the
forwarder, which sends it to the real backend.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request

from .config import ProbeConfig
from .forwarder import forward
from .logger import Session


def create_app(config: ProbeConfig, session: Session, pricing: dict) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # One shared HTTP client for the life of the server. Reusing it keeps
        # connections to the backend open between calls, which is faster.
        # LLM responses can take minutes, so there is no read timeout. Only
        # connecting has a limit.
        app.state.client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0))
        yield
        await app.state.client.aclose()

    app = FastAPI(title="CodexProbe", lifespan=lifespan)

    @app.api_route(
        "/v1/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def proxy(path: str, request: Request):
        return await forward(request, path, config, session, app.state.client, pricing)

    return app
