"""Session bookkeeping and header redaction.

A "session" is one start() to stop() cycle of the recorder. It holds the
ordered list of recorded LLM calls. Writing the session to a JSONL file is
added in a later milestone. For now the calls are kept in memory.
"""

from __future__ import annotations

import secrets
import threading
from datetime import datetime, timezone

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


class Session:
    """Holds the calls recorded between start() and stop()."""

    def __init__(self) -> None:
        self.session_id = new_session_id()
        self.calls: list[dict] = []
        self._next_index = 0
        # The server runs in a background thread while the user's code reads
        # the calls from the main thread, so guard the list with a lock.
        self._lock = threading.Lock()

    def next_call_index(self) -> int:
        """Reserve the position of a call at the moment the request arrives.

        The record itself is added later, when the response has finished,
        but the index is taken now so calls stay in request order.
        """
        with self._lock:
            index = self._next_index
            self._next_index += 1
            return index

    def add_call(self, record: dict) -> None:
        with self._lock:
            self.calls.append(record)
            self.calls.sort(key=lambda call: call["call_index"])

    def snapshot(self) -> list[dict]:
        """A copy of the calls recorded so far, in order."""
        with self._lock:
            return list(self.calls)
