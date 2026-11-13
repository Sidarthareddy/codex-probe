"""Shared test fixtures: a mock backend and a recorder pointed at it."""

import pytest

from codex_probe import ProxyRecorder
from tests.mock_backend import MockBackend


@pytest.fixture
def backend():
    mock = MockBackend()
    mock.start()
    yield mock
    mock.stop()


@pytest.fixture
def make_recorder(backend, tmp_path):
    """Build a recorder that forwards to the mock backend.

    Port 0 lets the OS choose a free port so tests never clash.
    """
    recorders = []

    def _make(api_key_env=None, wire_api="responses"):
        recorder = ProxyRecorder(
            {
                "listen_host": "127.0.0.1",
                "listen_port": 0,
                "backend": {
                    "name": "mock",
                    "base_url": backend.base_url,
                    "wire_api": wire_api,
                    "api_key_env": api_key_env,
                },
                "log_dir": str(tmp_path / "logs"),
            }
        )
        recorders.append(recorder)
        return recorder

    yield _make

    for recorder in recorders:
        if recorder._server is not None:
            recorder.stop()
