"""Session logging: the log schema, header redaction, JSONL file, summary.

A "session" is one start() to stop() cycle of the recorder. Each LLM call
becomes one JSON object on its own line in logs/<session_id>.jsonl (the
JSONL format: easy to append to, easy to read back line by line). When the
session stops, a summary is written to logs/<session_id>.summary.json.
"""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

# Headers that carry secrets. Their values must never reach the logs.
SECRET_HEADERS = {
    "authorization",
    "proxy-authorization",
    "api-key",
    "x-api-key",
    "openai-api-key",
    "cookie",
    "set-cookie",
}

REDACTED = "[REDACTED]"

# Headers a client may use to identify its own session or conversation.
CLIENT_SESSION_HEADERS = ("session-id", "session_id", "thread-id", "conversation_id", "x-session-id")


# --- log schema ---------------------------------------------------------------
# These pydantic models define exactly what one log line contains. Every
# record is built through CallRecord, so a log line can never be missing a
# field or have the wrong type.


class BackendInfo(BaseModel):
    name: str
    base_url: str


class RequestLog(BaseModel):
    headers: dict[str, str]
    body: Any  # full request JSON: instructions, history, tool schemas


class ResponseLog(BaseModel):
    status: int
    streamed: bool
    body: Any  # full response, reassembled if it was streamed


class UsageLog(BaseModel):
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int


class CallRecord(BaseModel):
    session_id: str
    call_index: int
    timestamp_start: str
    latency_ms: int                      # request received -> response finished
    first_chunk_ms: Optional[int]        # request received -> first response bytes
    backend: BackendInfo
    model: Optional[str]
    path: str
    client_session_id: Optional[str]     # session id header sent by the client, if any
    request: RequestLog
    translated_request: Optional[Any]    # only set when a wire format adapter ran
    response: Optional[ResponseLog]      # None when the backend was unreachable
    usage: Optional[UsageLog]            # None when the backend sent no usage
    cost_usd: Optional[float]            # None when usage or price is unknown
    error: Optional[str]


# --- helpers ------------------------------------------------------------------


def redact_headers(headers) -> dict:
    """Return a copy of the headers that is safe to log.

    Header names are case insensitive in HTTP, so compare in lowercase.
    The header name is kept (so the log shows an auth header was present)
    but the value is replaced.
    """
    safe = {}
    for name, value in headers:
        safe[name] = REDACTED if name.lower() in SECRET_HEADERS else value
    return safe


def find_client_session_id(headers) -> Optional[str]:
    lowered = {name.lower(): value for name, value in headers}
    for name in CLIENT_SESSION_HEADERS:
        if name in lowered:
            return lowered[name]
    return None


def now_iso() -> str:
    """Current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def new_session_id() -> str:
    """Build an id like "2026-11-13T14-05-00_ab12".

    The timestamp makes log files sort by time. The short random suffix
    keeps two sessions started in the same second from colliding.
    """
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    return f"{stamp}_{secrets.token_hex(2)}"


# --- session ------------------------------------------------------------------


class Session:
    """Holds the calls recorded between start() and stop(), and writes them."""

    def __init__(self, log_dir) -> None:
        self.session_id = new_session_id()
        self.calls: list[dict] = []
        self._next_index = 0
        # The server runs in a background thread while the user's code reads
        # the calls from the main thread, so guard shared state with a lock.
        self._lock = threading.Lock()

        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.log_dir / f"{self.session_id}.jsonl"
        self.summary_path = self.log_dir / f"{self.session_id}.summary.json"

    def next_call_index(self) -> int:
        """Reserve the position of a call at the moment the request arrives.

        The record itself is added later, when the response has finished,
        but the index is taken now so calls stay in request order.
        """
        with self._lock:
            index = self._next_index
            self._next_index += 1
            return index

    def add_call(self, record: CallRecord) -> None:
        """Store one finished call and append it to the JSONL file.

        The line is written right away, so a crash later in the session
        does not lose the calls recorded so far.
        """
        data = record.model_dump()
        with self._lock:
            self.calls.append(data)
            self.calls.sort(key=lambda call: call["call_index"])
            with self.log_path.open("a", encoding="utf-8") as file:
                # ensure_ascii=False keeps non English text readable in the log.
                file.write(json.dumps(data, ensure_ascii=False) + "\n")

    def snapshot(self) -> list[dict]:
        """A copy of the calls recorded so far, in order."""
        with self._lock:
            return list(self.calls)

    def write_summary(self) -> dict:
        summary = build_summary(self.session_id, self.snapshot())
        self.summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
        return summary


def build_summary(session_id: str, calls: list[dict]) -> dict:
    """Totals for a session plus a per call table.

    The per call table shows input tokens growing from call to call: an
    agent resends the whole conversation history on every turn.
    """
    with_usage = [call for call in calls if call["usage"] is not None]
    missing_usage = [call["call_index"] for call in calls if call["usage"] is None]
    known_costs = [call["cost_usd"] for call in calls if call["cost_usd"] is not None]
    unpriced = [
        call["call_index"]
        for call in calls
        if call["usage"] is not None and call["cost_usd"] is None
    ]
    total_latency = sum(call["latency_ms"] for call in calls)

    def total(field: str) -> int:
        return sum(call["usage"][field] for call in with_usage)

    per_call = []
    for call in calls:
        usage = call["usage"] or {}
        output_tokens = usage.get("output_tokens")
        # Generation speed: output tokens divided by the time spent streaming
        # them (from the first chunk to the end). The wait before the first
        # chunk is the model reading the prompt, so it is left out.
        streamed = bool(call["response"] and call["response"]["streamed"])
        first_chunk = call["first_chunk_ms"] or 0
        seconds = (call["latency_ms"] - first_chunk) / 1000
        per_call.append(
            {
                "call_index": call["call_index"],
                "model": call["model"],
                "status": call["response"]["status"] if call["response"] else None,
                "input_tokens": usage.get("input_tokens"),
                "cached_input_tokens": usage.get("cached_input_tokens"),
                "output_tokens": output_tokens,
                "latency_ms": call["latency_ms"],
                "first_chunk_ms": call["first_chunk_ms"],
                # Speed matters most for local models, where cost is zero.
                # Only for streamed responses: a one piece response has nothing to time.
                "output_tokens_per_second": round(output_tokens / seconds, 2)
                if output_tokens is not None and seconds > 0 and streamed
                else None,
                "cost_usd": call["cost_usd"],
                "error": call["error"],
            }
        )

    notes = []
    if missing_usage:
        notes.append(
            f"No usage data in the response for calls {missing_usage}. "
            "Token totals and cost do not include them."
        )
    if unpriced:
        notes.append(
            f"No price entry in the pricing file for the model used in calls {unpriced}. "
            "Total cost does not include them."
        )

    return {
        "session_id": session_id,
        "backends": sorted({call["backend"]["name"] for call in calls}),
        "models": sorted({call["model"] for call in calls if call["model"]}),
        "num_calls": len(calls),
        "num_errors": sum(1 for call in calls if call["error"]),
        "total_input_tokens": total("input_tokens"),
        "total_cached_input_tokens": total("cached_input_tokens"),
        "total_output_tokens": total("output_tokens"),
        "total_reasoning_tokens": total("reasoning_tokens"),
        "total_cost_usd": round(sum(known_costs), 8) if known_costs else None,
        "total_latency_ms": total_latency,
        "average_latency_ms": round(total_latency / len(calls)) if calls else None,
        "calls_missing_usage": missing_usage,
        "calls_missing_price": unpriced,
        "notes": notes,
        "per_call": per_call,
    }
