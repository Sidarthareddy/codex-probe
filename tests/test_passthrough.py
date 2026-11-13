"""Passthrough correctness: the proxy must not change requests or responses."""

import httpx
import pytest

from codex_probe import ProxyRecorder, load_config
from tests.mock_backend import CHAT_BODY, ERROR_BODY, RESPONSES_BODY

# Deliberately odd formatting: extra spaces, unsorted keys, unicode.
REQUEST_BYTES = (
    b'{"model":"gpt-test",   "stream":false,\n'
    b' "instructions":"You are a coding agent. \xe2\x9c\x93",'
    b'"input":[{"role":"user","content":"hello"}],'
    b'"tools":[{"type":"function","name":"shell","parameters":{"type":"object"}}]}'
)


def post(endpoint, path, body=REQUEST_BYTES, headers=None):
    all_headers = {"content-type": "application/json", "authorization": "Bearer sk-from-codex"}
    all_headers.update(headers or {})
    return httpx.post(endpoint + path, content=body, headers=all_headers)


def test_request_body_reaches_backend_byte_for_byte(backend, make_recorder):
    recorder = make_recorder()
    endpoint = recorder.start()
    post(endpoint, "/responses")
    recorder.stop()

    assert len(backend.received) == 1
    assert backend.received[0]["body"] == REQUEST_BYTES
    assert backend.received[0]["path"] == "/v1/responses"


def test_response_comes_back_byte_for_byte(backend, make_recorder):
    recorder = make_recorder()
    endpoint = recorder.start()
    response = post(endpoint, "/responses")
    recorder.stop()

    assert response.status_code == 200
    assert response.content == RESPONSES_BODY
    assert response.headers["content-type"] == "application/json"
    # Backend headers are relayed too.
    assert response.headers["x-request-id"] == "req_mock_123"


def test_chat_completions_path_also_passes_through(backend, make_recorder):
    recorder = make_recorder(wire_api="chat")
    endpoint = recorder.start()
    response = post(endpoint, "/chat/completions")
    recorder.stop()

    assert response.content == CHAT_BODY
    assert backend.received[0]["path"] == "/v1/chat/completions"


def test_error_status_and_body_are_relayed(backend, make_recorder):
    recorder = make_recorder()
    endpoint = recorder.start()
    response = post(endpoint, "/always_429")
    calls = recorder.stop()

    assert response.status_code == 429
    assert response.content == ERROR_BODY
    assert calls[0]["response"]["status"] == 429


def test_query_string_and_custom_headers_are_forwarded(backend, make_recorder):
    recorder = make_recorder()
    endpoint = recorder.start()
    post(endpoint, "/responses?api-version=2025-01-01", headers={"x-codex-session": "abc"})
    recorder.stop()

    assert backend.received[0]["query"] == "api-version=2025-01-01"
    assert backend.received[0]["headers"]["x-codex-session"] == "abc"


def test_calls_are_returned_in_order_with_full_bodies(backend, make_recorder):
    recorder = make_recorder()
    endpoint = recorder.start()
    for i in range(3):
        post(endpoint, "/responses", body=b'{"model":"gpt-test","input":"turn %d"}' % i)
    calls = recorder.stop()

    assert [c["call_index"] for c in calls] == [0, 1, 2]
    assert [c["request"]["body"]["input"] for c in calls] == ["turn 0", "turn 1", "turn 2"]
    first = calls[0]
    assert first["model"] == "gpt-test"
    assert first["path"] == "/v1/responses"
    assert first["backend"] == {"name": "mock", "base_url": backend.base_url}
    assert first["response"]["body"]["output"][0]["content"][0]["text"] == "café ok"
    assert first["latency_ms"] >= 0
    assert first["error"] is None


def test_get_requests_are_forwarded_but_not_recorded_as_llm_calls(backend, make_recorder):
    recorder = make_recorder()
    endpoint = recorder.start()
    response = httpx.get(endpoint + "/models")
    calls = recorder.stop()

    assert response.json()["data"][0]["id"] == "mock-model"
    assert calls == []


# --- auth handling (decided only by config) ---------------------------------


def test_no_api_key_env_drops_authorization(backend, make_recorder):
    recorder = make_recorder(api_key_env=None)
    endpoint = recorder.start()
    post(endpoint, "/responses")
    recorder.stop()

    assert "authorization" not in backend.received[0]["headers"]


def test_api_key_env_replaces_authorization(backend, make_recorder, monkeypatch):
    monkeypatch.setenv("PROBE_TEST_KEY", "sk-from-proxy-env")
    recorder = make_recorder(api_key_env="PROBE_TEST_KEY")
    endpoint = recorder.start()
    post(endpoint, "/responses")
    recorder.stop()

    assert backend.received[0]["headers"]["authorization"] == "Bearer sk-from-proxy-env"


def test_api_key_env_missing_forwards_codex_header(backend, make_recorder, monkeypatch):
    monkeypatch.delenv("PROBE_TEST_KEY", raising=False)
    recorder = make_recorder(api_key_env="PROBE_TEST_KEY")
    endpoint = recorder.start()
    post(endpoint, "/responses")
    recorder.stop()

    assert backend.received[0]["headers"]["authorization"] == "Bearer sk-from-codex"


def test_secrets_never_appear_in_recorded_calls(backend, make_recorder, monkeypatch):
    monkeypatch.setenv("PROBE_TEST_KEY", "sk-from-proxy-env")
    recorder = make_recorder(api_key_env="PROBE_TEST_KEY")
    endpoint = recorder.start()
    post(endpoint, "/responses")
    calls = recorder.stop()

    dumped = str(calls)
    assert "sk-from-codex" not in dumped
    assert "sk-from-proxy-env" not in dumped
    assert calls[0]["request"]["headers"]["authorization"] == "[REDACTED]"


# --- failure and lifecycle ---------------------------------------------------


def test_unreachable_backend_returns_502_and_records_error(tmp_path):
    recorder = ProxyRecorder(
        {
            "listen_port": 0,
            # Port 9 on localhost: nothing listens there.
            "backend": {"name": "dead", "base_url": "http://127.0.0.1:9/v1"},
            "log_dir": str(tmp_path),
        }
    )
    endpoint = recorder.start()
    response = post(endpoint, "/responses")
    calls = recorder.stop()

    assert response.status_code == 502
    assert calls[0]["error"] is not None
    assert calls[0]["response"] is None


def test_start_returns_v1_endpoint_and_stop_frees_the_port(make_recorder):
    recorder = make_recorder()
    endpoint = recorder.start()
    assert endpoint.startswith("http://127.0.0.1:") and endpoint.endswith("/v1")
    recorder.stop()
    with pytest.raises(httpx.ConnectError):
        httpx.get(endpoint + "/models")


def test_config_can_be_loaded_from_example_files():
    openai = load_config("examples/config.openai.json")
    qwen = load_config("examples/config.qwen.json")
    assert openai.backend.wire_api == "responses"
    assert qwen.backend.api_key_env is None
    # Same listen address: Codex config does not change when the backend swaps.
    assert (openai.listen_host, openai.listen_port) == (qwen.listen_host, qwen.listen_port)
