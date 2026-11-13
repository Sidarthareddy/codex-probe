"""Token usage extraction, cost calculation, and the session summary."""

import json

import httpx
import pytest

from codex_probe.pricing import compute_cost, load_pricing
from codex_probe.usage import extract_usage
from tests.mock_backend import CHAT_USAGE, RESPONSES_USAGE

HEADERS = {"content-type": "application/json"}

PRICING = {
    "gpt-test": {"input": 2.0, "cached_input": 0.5, "output": 10.0},
    "qwen-local": {"input": 0, "cached_input": 0, "output": 0},
}
NORMALIZED = {"input_tokens": 1200, "cached_input_tokens": 1000, "output_tokens": 45, "reasoning_tokens": 20}


@pytest.fixture
def pricing_file(tmp_path):
    path = tmp_path / "pricing.json"
    path.write_text(json.dumps({"checked_on": "2026-01-01", "models": PRICING}))
    return path


# --- usage extraction --------------------------------------------------------


def test_extract_usage_from_responses_format():
    assert extract_usage({"usage": RESPONSES_USAGE}) == NORMALIZED


def test_extract_usage_from_chat_format():
    assert extract_usage({"usage": CHAT_USAGE}) == {
        "input_tokens": 800, "cached_input_tokens": 0, "output_tokens": 30, "reasoning_tokens": 0,
    }


def test_extract_usage_without_detail_fields():
    assert extract_usage({"usage": {"input_tokens": 7, "output_tokens": 2}}) == {
        "input_tokens": 7, "cached_input_tokens": 0, "output_tokens": 2, "reasoning_tokens": 0,
    }


@pytest.mark.parametrize("body", [None, "plain text", {}, {"usage": None}, {"usage": {}}])
def test_missing_usage_is_none_never_a_guess(body):
    assert extract_usage(body) is None


# --- cost --------------------------------------------------------------------


def test_cost_bills_cached_and_fresh_input_separately():
    # 200 fresh * $2 + 1000 cached * $0.5 + 45 output * $10, per 1M tokens
    expected = (200 * 2.0 + 1000 * 0.5 + 45 * 10.0) / 1_000_000
    assert compute_cost("gpt-test", NORMALIZED, PRICING) == pytest.approx(expected)


def test_local_model_costs_zero():
    assert compute_cost("qwen-local", NORMALIZED, PRICING) == 0


def test_cost_is_none_when_model_has_no_price_or_usage_is_missing():
    assert compute_cost("unknown-model", NORMALIZED, PRICING) is None
    assert compute_cost("gpt-test", None, PRICING) is None


def test_cached_price_defaults_to_input_price():
    pricing = {"m": {"input": 3.0, "output": 0}}
    usage = {"input_tokens": 1_000_000, "cached_input_tokens": 500_000, "output_tokens": 0, "reasoning_tokens": 0}
    assert compute_cost("m", usage, pricing) == pytest.approx(3.0)


def test_load_pricing_reads_models_and_tolerates_missing_file(pricing_file, tmp_path):
    assert load_pricing(pricing_file) == PRICING
    assert load_pricing(tmp_path / "nope.json") == {}


def test_repo_pricing_file_lists_the_local_qwen_model_as_free():
    assert compute_cost("qwen2.5-coder:7b", NORMALIZED, load_pricing("pricing.json")) == 0


# --- end to end: usage and cost in the log, and the summary -------------------


def test_usage_and_cost_are_logged_per_call(backend, make_recorder, pricing_file):
    recorder = make_recorder(pricing_file=pricing_file)
    endpoint = recorder.start()
    httpx.post(endpoint + "/responses", content=b'{"model":"gpt-test","stream":true}', headers=HEADERS)
    httpx.post(endpoint + "/responses", content=b'{"model":"gpt-test"}', headers=HEADERS)
    calls = recorder.stop()

    assert calls[0]["usage"] == NORMALIZED                      # from the final stream event
    assert calls[0]["cost_usd"] == pytest.approx(0.00135)
    assert calls[1]["usage"]["input_tokens"] == 12              # from the plain JSON body
    assert calls[1]["cost_usd"] == pytest.approx((12 * 2.0 + 3 * 10.0) / 1_000_000)


def test_summary_totals_and_per_call_table(backend, make_recorder, pricing_file):
    recorder = make_recorder(pricing_file=pricing_file)
    endpoint = recorder.start()
    httpx.post(endpoint + "/responses", content=b'{"model":"gpt-test","stream":true}', headers=HEADERS)
    httpx.post(endpoint + "/responses", content=b'{"model":"gpt-test"}', headers=HEADERS)
    calls = recorder.stop()

    summary = json.loads(recorder.session.summary_path.read_text())
    assert summary == recorder.summary
    assert summary["session_id"] == recorder.session.session_id
    assert summary["num_calls"] == 2
    assert summary["total_input_tokens"] == 1212
    assert summary["total_cached_input_tokens"] == 1000
    assert summary["total_output_tokens"] == 48
    assert summary["total_cost_usd"] == pytest.approx(sum(c["cost_usd"] for c in calls))
    assert summary["total_latency_ms"] == sum(c["latency_ms"] for c in calls)
    assert summary["notes"] == []
    assert [row["input_tokens"] for row in summary["per_call"]] == [1200, 12]
    assert summary["per_call"][0]["output_tokens_per_second"] is not None


def test_summary_says_so_when_usage_is_missing(backend, make_recorder, pricing_file):
    backend.include_usage = False
    recorder = make_recorder(pricing_file=pricing_file)
    endpoint = recorder.start()
    httpx.post(endpoint + "/responses", content=b'{"model":"gpt-test","stream":true}', headers=HEADERS)
    calls = recorder.stop()

    assert calls[0]["usage"] is None
    assert calls[0]["cost_usd"] is None
    assert recorder.summary["calls_missing_usage"] == [0]
    assert recorder.summary["total_cost_usd"] is None
    assert "No usage data" in recorder.summary["notes"][0]


def test_summary_notes_models_without_a_price(backend, make_recorder, pricing_file):
    recorder = make_recorder(pricing_file=pricing_file)
    endpoint = recorder.start()
    httpx.post(endpoint + "/responses", content=b'{"model":"mystery-model"}', headers=HEADERS)
    recorder.stop()

    assert recorder.summary["calls_missing_price"] == [0]
    assert recorder.summary["total_input_tokens"] == 12   # tokens still counted
    assert "No price entry" in recorder.summary["notes"][0]


def test_tokens_per_second_only_for_streamed_calls(backend, make_recorder, pricing_file):
    recorder = make_recorder(pricing_file=pricing_file)
    endpoint = recorder.start()
    httpx.post(endpoint + "/responses", content=b'{"model":"gpt-test","stream":true}', headers=HEADERS)
    httpx.post(endpoint + "/responses", content=b'{"model":"gpt-test"}', headers=HEADERS)
    recorder.stop()

    streamed, plain = recorder.summary["per_call"]
    assert streamed["output_tokens_per_second"] > 0
    assert plain["output_tokens_per_second"] is None
