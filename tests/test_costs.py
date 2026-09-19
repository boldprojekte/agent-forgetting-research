import json

import pytest

from forgetting_agent.costs import estimate, fetch_pricing
from forgetting_agent.trace import Trace

PRICE = {
    "model": "model",
    "currency": "USD",
    "observed_at": "2026-09-10",
    "rates": {
        "input_cost_per_token": 0.0000002,
        "output_cost_per_token": 0.0000005,
        "cache_read_input_token_cost": 0.00000005,
        "cache_creation_input_token_cost": None,
    },
}


def start():
    return {
        "event": "run_start",
        "model": "model",
        "provider": {"provider": "openai-compatible", "pricing": PRICE},
    }


def response(cached=None):
    usage = {
        "prompt_tokens": 1_000_000,
        "completion_tokens": 100_000,
        "completion_tokens_details": {"reasoning_tokens": 90_000},
    }
    if cached is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    return {"event": "response", "step": 1, "usage": usage}


def test_cache_bounds_reasoning_not_double_charged():
    end = {"event": "termination"}
    cost = estimate([start(), response(800_000), end])
    assert cost["known_usage_lower_usd"] == pytest.approx(0.13)
    assert cost["total_upper_usd"] == pytest.approx(0.13)
    cost = estimate([start(), response(), end])
    assert cost["known_usage_lower_usd"] == pytest.approx(0.10)
    assert cost["total_upper_usd"] == pytest.approx(0.25)
    assert cost["tokens_reported"]["unknown_cache_input_tokens"] == 1_000_000


def test_missing_usage_retry_and_incomplete_never_claim_total_ceiling():
    cost = estimate(
        [
            start(),
            response(0),
            {"event": "provider_attempt", "step": 1, "outcome": "retry"},
            {"event": "termination"},
        ]
    )
    assert cost["known_usage_lower_usd"] == pytest.approx(0.25)
    assert cost["total_upper_usd"] is None
    assert cost["unmetered_attempts"] == 1
    cost = estimate([start(), {"event": "response", "usage": None}, {"event": "termination"}])
    assert cost["responses_with_missing_usage"] == 1
    assert cost["total_upper_usd"] is None
    assert estimate([start(), response(0)])["total_upper_usd"] is None


def test_invalid_cache_and_mismatched_pricing_are_not_trusted():
    cost = estimate([start(), response(2_000_000), {"event": "termination"}])
    assert cost["total_upper_usd"] == pytest.approx(0.25)
    assert cost["tokens_reported"]["cached_input_tokens"] == 0
    assert estimate([start(), response()], {**PRICE, "model": "other"})["status"] == "unpriced"


def test_live_cost_file_updates_without_polluting_trace_or_double_counting_chunks(tmp_path):
    trace = Trace(tmp_path / "trace.jsonl")
    trace.event("run_start", model="model", provider=start()["provider"])
    trace.event("provider_chunk", body={"usage": response(0)["usage"]})
    trace.event("provider_attempt", step=1, outcome="success")
    trace.event("response", step=1, usage=response(0)["usage"])
    trace.event("termination", reason="submitted")
    trace.close()
    cost = json.loads((tmp_path / "costs.json").read_text())
    assert cost["responses"] == 1
    assert cost["total_upper_usd"] == pytest.approx(0.25)
    assert len(trace.events) == 5


def test_metadata_failure_is_bounded_and_does_not_expose_secret(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("secret")

    monkeypatch.setattr("forgetting_agent.costs.urlopen", fail)
    value = fetch_pricing("https://api.tensorx.ai/v1", "secret", "model")
    assert value["status"] == "unavailable"
    assert "secret" not in json.dumps(value)
    assert fetch_pricing("https://another.example/v1", "secret", "model")["status"] == "unavailable"


def test_rejected_attempt_usage_counted_once_and_success_not_doubled():
    events = [
        start(),
        response(800_000),
        {
            "event": "provider_attempt",
            "step": 1,
            "outcome": "success",
            "usage": response(800_000)["usage"],
        },
        {
            "event": "provider_attempt",
            "step": 2,
            "outcome": "stream_error",
            "usage": response(0)["usage"],
        },
        {"event": "termination"},
    ]
    costs = estimate(events)
    assert costs["responses"] == 1
    assert costs["metered_rejected_attempts"] == 1
    assert costs["unmetered_attempts"] == 0
    assert costs["total_upper_usd"] == pytest.approx(0.38)
