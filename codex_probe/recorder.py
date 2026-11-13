"""ProxyRecorder: the public entry point.

    recorder = ProxyRecorder(config)
    endpoint = recorder.start()   # "http://127.0.0.1:8135/v1"
    ...run Codex against endpoint...
    calls = recorder.stop()       # list[dict], one per LLM call, in order
"""

from __future__ import annotations

import threading
import time

import uvicorn
from dotenv import load_dotenv

from .config import load_config
from .logger import Session
from .server import create_app


class BackgroundServer:
    """Run a web app with uvicorn in a background thread.

    uvicorn.run() normally blocks forever. Running the server in a thread
    lets start() return right away so the caller can go on to launch Codex.
    The tests reuse this class to run the mock backend.
    """

    def __init__(self, app, host: str, port: int) -> None:
        self._server = uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_level="warning")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.host = host
        self.port = port

    def start(self) -> None:
        self._thread.start()
        # Wait until uvicorn is really accepting connections. Otherwise the
        # caller could send a request before the socket is open.
        deadline = time.time() + 10
        while not self._server.started:
            if not self._thread.is_alive() or time.time() > deadline:
                raise RuntimeError(
                    f"Server failed to start on {self.host}:{self.port} (is the port in use?)"
                )
            time.sleep(0.01)
        # If port 0 was requested, the OS picked a free port. Read back which.
        self.port = self._server.servers[0].sockets[0].getsockname()[1]

    def stop(self) -> None:
        # uvicorn checks this flag in its main loop and shuts down cleanly.
        self._server.should_exit = True
        self._thread.join(timeout=10)


class ProxyRecorder:
    def __init__(self, config) -> None:
        # Read .env so OPENAI_API_KEY is available without exporting it.
        load_dotenv()
        self.config = load_config(config)
        self.session: Session | None = None
        self._server: BackgroundServer | None = None

    def start(self) -> str:
        """Start the proxy and return the endpoint to give to Codex."""
        if self._server is not None:
            raise RuntimeError("Recorder is already running")
        self.session = Session()
        app = create_app(self.config, self.session)
        self._server = BackgroundServer(app, self.config.listen_host, self.config.listen_port)
        self._server.start()
        return f"http://{self._server.host}:{self._server.port}/v1"

    def stop(self) -> list[dict]:
        """Stop the proxy and return the recorded calls for this session."""
        if self._server is None or self.session is None:
            raise RuntimeError("Recorder is not running")
        self._server.stop()
        self._server = None
        return self.session.snapshot()
