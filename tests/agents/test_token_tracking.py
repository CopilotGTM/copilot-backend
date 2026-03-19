from __future__ import annotations

from types import SimpleNamespace

from src.agents.common.token_tracking import TokenUsageData, extract_token_usage
from src.infra.config.model_pricing import calculate_cost


def test_to_dict_minimal():
    usage = TokenUsageData(input_tokens=100, output_tokens=50, total_tokens=150)
    d = usage.to_dict()
    assert d == {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}


def test_to_dict_full():
    usage = TokenUsageData(
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        reasoning_tokens=20,
        cache_read_tokens=30,
        cache_write_tokens=10,
        cost=0.0035,
        model_name="gpt-4o",
        duration_seconds=3.456,
        time_to_first_token=0.25,
    )
    d = usage.to_dict()
    assert d["reasoning_tokens"] == 20
    assert d["cache_read_tokens"] == 30
    assert d["cache_write_tokens"] == 10
    assert d["cost"] == 0.0035
    assert d["model_name"] == "gpt-4o"
    assert d["duration_seconds"] == 3.46
    assert d["time_to_first_token"] == 0.25


# ---------------------------------------------------------------------------
# calculate_cost tests
# ---------------------------------------------------------------------------


def test_calculate_cost_gpt_5_4_no_cache():
    """gpt-5.4: 1M input × $2.50 + 1M output × $15.00 = $17.50"""
    cost = calculate_cost(
        model_name="gpt-5.4",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert cost is not None
    assert abs(cost - 17.50) < 0.001


def test_calculate_cost_gpt_5_4_with_cache():
    """gpt-5.4: 8350 input, 3328 cache_read, 67 output.

    non_cached_input = 8350 - 3328 = 5022
    cost = (5022/1M × $2.50) + (3328/1M × $0.25) + (67/1M × $15.00)
    """
    cost = calculate_cost(
        model_name="gpt-5.4",
        input_tokens=8350,
        output_tokens=67,
        cache_read_tokens=3328,
    )
    assert cost is not None
    expected = (5022 * 2.50 + 3328 * 0.25 + 67 * 15.00) / 1_000_000
    assert abs(cost - expected) < 0.000001


def test_calculate_cost_gpt_5_mini():
    """gpt-5-mini: much cheaper rates."""
    cost = calculate_cost(
        model_name="gpt-5-mini",
        input_tokens=10_000,
        output_tokens=500,
    )
    assert cost is not None
    expected = (10_000 * 0.25 + 500 * 2.00) / 1_000_000
    assert abs(cost - expected) < 0.000001


def test_calculate_cost_unknown_model():
    """Unknown model returns None."""
    assert calculate_cost(model_name="unknown-model", input_tokens=100, output_tokens=50) is None


def test_calculate_cost_no_model():
    """No model name returns None."""
    assert calculate_cost(model_name=None, input_tokens=100, output_tokens=50) is None


# ---------------------------------------------------------------------------
# extract_token_usage tests
# ---------------------------------------------------------------------------


def test_extract_token_usage_with_provider_cost():
    """When provider returns cost, use it directly (don't override)."""
    model_metric = SimpleNamespace(
        input_tokens=500,
        output_tokens=200,
        total_tokens=700,
        reasoning_tokens=0,
        cache_read_tokens=50,
        cache_write_tokens=10,
        cost=0.01,
        id="gpt-4o",
        provider="azure_openai",
    )
    metrics = SimpleNamespace(
        input_tokens=500,
        output_tokens=200,
        total_tokens=700,
        reasoning_tokens=0,
        cache_read_tokens=50,
        cache_write_tokens=10,
        cost=0.01,
        duration=2.5,
        time_to_first_token=0.3,
        details={"model": [model_metric]},
    )
    response = SimpleNamespace(metrics=metrics, model=None)

    usage = extract_token_usage(response, agent_name="TestAgent")
    assert usage is not None
    assert usage.cost == 0.01  # provider cost preserved


def test_extract_token_usage_calculates_cost_when_provider_returns_none():
    """When provider returns cost=None (Azure), calculate from pricing table."""
    model_metric = SimpleNamespace(
        input_tokens=8350,
        output_tokens=67,
        total_tokens=8417,
        reasoning_tokens=0,
        cache_read_tokens=3328,
        cache_write_tokens=0,
        cost=None,
        id="gpt-5.4",
        provider="azure_openai",
    )
    metrics = SimpleNamespace(
        input_tokens=8350,
        output_tokens=67,
        total_tokens=8417,
        reasoning_tokens=0,
        cache_read_tokens=3328,
        cache_write_tokens=0,
        cost=None,
        duration=4.6,
        time_to_first_token=None,
        details={"model": [model_metric]},
    )
    response = SimpleNamespace(metrics=metrics, model=None)

    usage = extract_token_usage(response, agent_name="TestAgent")
    assert usage is not None
    assert usage.model_name == "gpt-5.4"
    assert usage.cost is not None
    assert usage.cost > 0
    # Verify the math: (5022 × 2.50 + 3328 × 0.25 + 67 × 15.00) / 1M
    expected = (5022 * 2.50 + 3328 * 0.25 + 67 * 15.00) / 1_000_000
    assert abs(usage.cost - expected) < 0.000001


def test_extract_token_usage_no_details_fallback():
    """When details is empty, read top-level RunMetrics fields."""
    metrics = SimpleNamespace(
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        reasoning_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost=0.005,
        duration=1.0,
        time_to_first_token=None,
        details=None,
    )
    response = SimpleNamespace(metrics=metrics, model="gpt-4o-mini")

    usage = extract_token_usage(response, agent_name="TestAgent")
    assert usage is not None
    assert usage.input_tokens == 100
    assert usage.model_name == "gpt-4o-mini"
    assert usage.cost == 0.005  # provider cost preserved


def test_extract_token_usage_no_metrics():
    response = SimpleNamespace(metrics=None)
    assert extract_token_usage(response) is None
