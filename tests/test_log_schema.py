"""The JSONL session log: one file per session, every line matches the schema."""

import json

import httpx

from codex_probe.logger import CallRecord, redact_headers

HEADERS = {
    "content-type": "application/json",
    "authorization": "Bearer sk-secret-123",
    "x-api-key": "another-secret",
    "session_id": "codex-session-42",
}

EXPECTED_FIELDS = {
    "session_id", "call_index", "timestamp_start", "latency_ms", "first_chunk_ms",
    "backend", "model", "path", "client_session_id", "request", "translated_request",
    "response", "usage", "cost_usd", "error",
}


def run_session(make_recorder, bodies, path="/responses"):
    recorder = make_recorder()
    endpoint = recorder.start()
    for body in bodies:
        httpx.post(endpoint + path, content=json.dumps(body).encode(), headers=HEADERS)
    calls = recorder.stop()
    return recorder, calls


def test_one_jsonl_file_per_session_with_one_line_per_call(backend, make_recorder):
    recorder, calls = run_session(
        make_recorder,
        [{"model": "gpt-test", "input": "a"}, {"model": "gpt-test", "stream": True, "input": "b"}],
    )
    log_path = recorder.session.log_path
    assert log_path.name == f"{recorder.session.session_id}.jsonl"

    lines = log_path.read_text().splitlines()
    assert len(lines) == 2
    # The file holds exactly what stop() returned, in order.
    assert [json.loads(line) for line in lines] == calls


def test_every_line_validates_against_the_schema(backend, make_recorder):
    recorder, _ = run_session(
        make_recorder,
        [{"model": "gpt-test", "input": "a"}, {"model": "gpt-test", "stream": True, "input": "b"}],
    )
    for line in recorder.session.log_path.read_text().splitlines():
        data = json.loads(line)
        assert set(data) == EXPECTED_FIELDS
        CallRecord.model_validate(data)  # raises if a field is missing or mistyped
        assert set(data["request"]) == {"headers", "body"}
        assert set(data["response"]) == {"status", "streamed", "body"}
        assert set(data["backend"]) == {"name", "base_url"}


def test_request_is_logged_in_full_not_truncated(backend, make_recorder):
    big_request = {
        "model": "gpt-test",
        "instructions": "You are a coding agent. " * 2000,
        "input": [{"role": "user", "content": f"message {i} " + "x" * 500} for i in range(200)],
        "tools": [{"type": "function", "name": f"tool_{i}", "parameters": {"type": "object"}} for i in range(50)],
    }
    recorder, calls = run_session(make_recorder, [big_request])
    assert calls[0]["request"]["body"] == big_request
    logged = json.loads(recorder.session.log_path.read_text().splitlines()[0])
    assert logged["request"]["body"] == big_request


def test_secrets_are_redacted_in_the_file_but_other_headers_are_kept(backend, make_recorder):
    recorder, calls = run_session(make_recorder, [{"model": "gpt-test"}])
    file_text = recorder.session.log_path.read_text()
    assert "sk-secret-123" not in file_text
    assert "another-secret" not in file_text

    headers = calls[0]["request"]["headers"]
    assert headers["authorization"] == "[REDACTED]"
    assert headers["x-api-key"] == "[REDACTED]"
    assert headers["content-type"] == "application/json"


def test_redaction_ignores_header_name_case():
    safe = redact_headers([("Authorization", "Bearer x"), ("X-Api-Key", "y"), ("Accept", "*/*")])
    assert safe == {"Authorization": "[REDACTED]", "X-Api-Key": "[REDACTED]", "Accept": "*/*"}


def test_client_session_header_is_recorded(backend, make_recorder):
    _, calls = run_session(make_recorder, [{"model": "gpt-test"}])
    assert calls[0]["client_session_id"] == "codex-session-42"


def test_each_start_stop_cycle_gets_its_own_files(backend, make_recorder):
    first, _ = run_session(make_recorder, [{"model": "gpt-test"}])
    second, _ = run_session(make_recorder, [{"model": "gpt-test"}, {"model": "gpt-test"}])
    assert first.session.session_id != second.session.session_id
    assert len(first.session.log_path.read_text().splitlines()) == 1
    assert len(second.session.log_path.read_text().splitlines()) == 2
    assert first.session.summary_path.exists() and second.session.summary_path.exists()
