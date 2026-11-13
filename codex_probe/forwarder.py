"""Forward one request to the real backend and relay the answer.

This is the passthrough path. The rules:
  * the request body is sent on exactly as received (same bytes)
  * the response body is sent back exactly as received (same bytes)
  * a record of the call is saved to the session

Streaming (SSE) relay is added in the next milestone. This version waits for
the whole response before relaying it.
"""

from __future__ import annotations

import gzip
import json
import os
import time
import zlib

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from .config import ProbeConfig
from .logger import Session, now_iso, redact_headers

# "Hop by hop" headers describe one single network connection (for example
# "keep this socket open"). They are not meant to be passed along by a proxy.
# Each side of the proxy sets its own.
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

# Also not copied to the backend:
#   host           -> must name the backend, not the proxy. httpx sets it.
#   content-length -> httpx recomputes it from the body (same value).
SKIP_REQUEST_HEADERS = HOP_BY_HOP | {"host", "content-length"}

# Not copied back to Codex: the web server computes the length itself.
SKIP_RESPONSE_HEADERS = HOP_BY_HOP | {"content-length"}


def build_upstream_headers(incoming, config: ProbeConfig) -> list[tuple[str, str]]:
    """Copy the headers Codex sent, then apply the backend's auth rule.

    Auth rule, decided only by config:
      * api_key_env is null        -> the backend needs no key, so drop the
                                      Authorization header. This keeps an
                                      OpenAI key from being sent to some
                                      other server.
      * api_key_env is set and the
        variable exists            -> use that key.
      * api_key_env is set but the
        variable is missing        -> forward whatever Codex sent.
    """
    headers = [(k, v) for k, v in incoming if k.lower() not in SKIP_REQUEST_HEADERS]

    key_env = config.backend.api_key_env
    if key_env is None:
        headers = [(k, v) for k, v in headers if k.lower() != "authorization"]
    else:
        key = os.environ.get(key_env)
        if key:
            headers = [(k, v) for k, v in headers if k.lower() != "authorization"]
            headers.append(("authorization", f"Bearer {key}"))
    return headers


def build_upstream_url(config: ProbeConfig, path: str, query: str) -> str:
    """Join the backend base_url with the path Codex asked for.

    Codex calls  http://127.0.0.1:8135/v1/responses
    We forward to  <backend base_url>/responses
    """
    url = config.backend.base_url.rstrip("/") + "/" + path
    if query:
        url += "?" + query
    return url


def decode_body_for_log(raw: bytes, content_encoding: str = ""):
    """Turn raw body bytes into something readable for the log.

    This is for logging only. The bytes relayed to Codex are never touched.
    A backend may compress its response (gzip), so decompress a copy first.
    Then store JSON as a real object when it parses, otherwise as text.
    """
    if not raw:
        return None
    encoding = content_encoding.lower().strip()
    try:
        if encoding == "gzip":
            raw = gzip.decompress(raw)
        elif encoding == "deflate":
            raw = zlib.decompress(raw)
    except (OSError, zlib.error):
        pass
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


async def forward(
    request: Request,
    path: str,
    config: ProbeConfig,
    session: Session,
    client: httpx.AsyncClient,
) -> Response:
    """Handle one request from Codex."""
    # Every LLM call is a POST. Other requests (like GET /v1/models) are
    # forwarded too, but are not recorded as LLM calls.
    is_llm_call = request.method == "POST"
    call_index = session.next_call_index() if is_llm_call else None

    # request.body() gives the exact bytes Codex sent. We never parse and
    # re-serialize them on the way to the backend, because that could change
    # whitespace or key order.
    body = await request.body()
    url = build_upstream_url(config, path, request.url.query)
    headers = build_upstream_headers(request.headers.items(), config)

    timestamp_start = now_iso()
    started = time.perf_counter()

    status = None
    raw_response = b""
    response_headers: list[tuple[str, str]] = []
    error = None
    try:
        upstream_request = client.build_request(
            request.method, url, headers=headers, content=body
        )
        # stream=True plus aiter_raw() gives us the response bytes exactly as
        # the backend sent them (still compressed, if it compressed them).
        upstream = await client.send(upstream_request, stream=True)
        try:
            status = upstream.status_code
            response_headers = list(upstream.headers.items())
            chunks = [chunk async for chunk in upstream.aiter_raw()]
            raw_response = b"".join(chunks)
        finally:
            await upstream.aclose()
    except httpx.HTTPError as exc:
        # The backend could not be reached or the connection broke.
        error = f"{type(exc).__name__}: {exc}"

    latency_ms = round((time.perf_counter() - started) * 1000)

    if is_llm_call:
        request_json = decode_body_for_log(body, request.headers.get("content-encoding", ""))
        content_encoding = dict(
            (k.lower(), v) for k, v in response_headers
        ).get("content-encoding", "")
        session.add_call(
            {
                "session_id": session.session_id,
                "call_index": call_index,
                "timestamp_start": timestamp_start,
                "latency_ms": latency_ms,
                "backend": {
                    "name": config.backend.name,
                    "base_url": config.backend.base_url,
                },
                "model": request_json.get("model") if isinstance(request_json, dict) else None,
                "path": "/v1/" + path,
                "request": {
                    "headers": redact_headers(request.headers.items()),
                    "body": request_json,
                },
                "translated_request": None,
                "response": None
                if error
                else {
                    "status": status,
                    "streamed": False,
                    "body": decode_body_for_log(raw_response, content_encoding),
                },
                "usage": None,
                "cost_usd": None,
                "error": error,
            }
        )

    if error:
        # 502 Bad Gateway is the standard status for "the proxy could not get
        # an answer from the server behind it".
        return JSONResponse(
            status_code=502,
            content={"error": {"message": f"CodexProbe could not reach the backend: {error}",
                               "type": "codex_probe_upstream_error"}},
        )

    relay_headers = {
        k: v for k, v in response_headers if k.lower() not in SKIP_RESPONSE_HEADERS
    }
    return Response(content=raw_response, status_code=status, headers=relay_headers)
