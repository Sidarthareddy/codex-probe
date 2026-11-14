"""Forward one request to the real backend and relay the answer live.

This is the passthrough path. The rules:
  * the request body is sent on exactly as received (same bytes)
  * the response is relayed chunk by chunk, as it arrives (same bytes)
  * a copy of the chunks is kept, and after the response ends the full
    call is written to the session log

Usage and cost are computed from the logged copy only. Nothing here changes
what Codex or the backend sees.
"""

from __future__ import annotations

import gzip
import json
import os
import time
import zlib
from typing import Optional

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import ProbeConfig
from .logger import (
    BackendInfo,
    CallRecord,
    RequestLog,
    ResponseLog,
    Session,
    find_client_session_id,
    now_iso,
    redact_headers,
)
from .pricing import compute_cost
from .reassembly import reassemble, stream_is_complete
from .usage import extract_usage

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


CLIENT_DISCONNECTED = "Client disconnected before the response finished"


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


def decompress_for_log(raw: bytes, content_encoding: str) -> bytes:
    """Undo gzip or deflate compression on a copy of the body, for the log.

    A backend may compress its response. Codex gets the compressed bytes
    as they are. Only the log copy is decompressed so it can be read.
    """
    encoding = (content_encoding or "").lower().strip()
    try:
        if encoding == "gzip":
            return gzip.decompress(raw)
        if encoding == "deflate":
            return zlib.decompress(raw)
    except (OSError, zlib.error):
        pass
    return raw


def decode_body_for_log(raw: bytes):
    """Store JSON as a real object when it parses, otherwise as text."""
    if not raw:
        return None
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
    pricing: dict,
):
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

    def elapsed_ms() -> int:
        return round((time.perf_counter() - started) * 1000)

    def record_call(
        status: Optional[int],
        response_headers: dict,
        raw_response: bytes,
        first_chunk_ms: Optional[int],
        error: Optional[str],
    ) -> None:
        """Build the log record for this call. Runs after the response ended."""
        if not is_llm_call:
            return
        request_json = decode_body_for_log(
            decompress_for_log(body, request.headers.get("content-encoding", ""))
        )
        model = request_json.get("model") if isinstance(request_json, dict) else None

        response_log = None
        usage = None
        if status is not None:
            readable = decompress_for_log(raw_response, response_headers.get("content-encoding", ""))
            streamed = "text/event-stream" in response_headers.get("content-type", "")
            # A streamed response is many SSE events. Glue them back together.
            response_body = reassemble(readable) if streamed else decode_body_for_log(readable)
            response_log = ResponseLog(status=status, streamed=streamed, body=response_body)
            # Codex closes the connection as soon as it has read the final
            # event, often before the backend has closed its side. That is a
            # normal end, not a failure, so it is not logged as an error.
            if error == CLIENT_DISCONNECTED and streamed and stream_is_complete(readable):
                error = None
            usage = extract_usage(response_body)

        session.add_call(
            CallRecord(
                session_id=session.session_id,
                call_index=call_index,
                timestamp_start=timestamp_start,
                latency_ms=elapsed_ms(),
                first_chunk_ms=first_chunk_ms,
                backend=BackendInfo(name=config.backend.name, base_url=config.backend.base_url),
                model=model,
                path="/v1/" + path,
                client_session_id=find_client_session_id(request.headers.items()),
                request=RequestLog(headers=redact_headers(request.headers.items()), body=request_json),
                translated_request=None,
                response=response_log,
                usage=usage,
                cost_usd=compute_cost(model, usage, pricing),
                error=error,
            )
        )

    try:
        upstream_request = client.build_request(request.method, url, headers=headers, content=body)
        # stream=True means: return as soon as the response headers arrive,
        # and let us read the body piece by piece afterwards.
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        # The backend could not be reached at all.
        error = f"{type(exc).__name__}: {exc}"
        record_call(None, {}, b"", None, error)
        # 502 Bad Gateway is the standard status for "the proxy could not get
        # an answer from the server behind it".
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "message": f"CodexProbe could not reach the backend: {error}",
                    "type": "codex_probe_upstream_error",
                }
            },
        )

    response_headers = {k.lower(): v for k, v in upstream.headers.items()}

    async def relay():
        """Pass response bytes to Codex as they arrive, keeping a copy.

        This is an async generator. Each `yield chunk` hands one piece to the
        web server, which sends it to Codex immediately. The function then
        pauses until the backend produces the next piece. Nothing is held
        back, so Codex sees tokens appear at the same pace as without the
        proxy. The copy in `chunks` is only used after the stream ends.
        """
        chunks: list[bytes] = []
        first_chunk_ms = None
        error = None
        try:
            # aiter_raw() gives the bytes exactly as the backend sent them
            # (still compressed, if the backend compressed them).
            async for chunk in upstream.aiter_raw():
                if first_chunk_ms is None:
                    first_chunk_ms = elapsed_ms()
                chunks.append(chunk)
                yield chunk
        except httpx.HTTPError as exc:
            error = f"Stream from backend broke: {type(exc).__name__}: {exc}"
            raise
        except BaseException:
            # Raised into the generator when Codex hangs up early.
            error = CLIENT_DISCONNECTED
            raise
        finally:
            # Runs on normal end, on error, and on disconnect, so every call
            # gets logged, with whatever part of the response was received.
            record_call(upstream.status_code, response_headers, b"".join(chunks), first_chunk_ms, error)
            await upstream.aclose()

    relay_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP}
    return StreamingResponse(relay(), status_code=upstream.status_code, headers=relay_headers)
