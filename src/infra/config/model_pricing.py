"""Model pricing configuration for LLM cost calculation.

Providers (OpenAI, Azure) do not return cost in API responses, so we
calculate it from token counts using this pricing table.

All prices are USD per 1M tokens. Override via environment variables:
    MODEL_PRICING_GPT_5_4_INPUT=2.50
    MODEL_PRICING_GPT_5_4_CACHE_READ=0.25
    MODEL_PRICING_GPT_5_4_OUTPUT=15.00

cache_read pricing reflects the 90% discount on input for cached tokens.
cache_write has no extra cost (billed at normal input price).
"""

from __future__ import annotations

import os

from src.infra.utils.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "gpt-5.6-sol": {"input": 5.00, "cache_read": 0.50, "output": 30.00},
    "gpt-5.6-terra": {"input": 2.50, "cache_read": 0.25, "output": 15.00},
    "gpt-5.6-luna": {"input": 1.00, "cache_read": 0.10, "output": 6.00},
    # Realtime audio tokens need modality-specific accounting; these are text-token rates.
    "gpt-realtime-2.1": {"input": 4.00, "cache_read": 0.40, "output": 24.00},
    "gpt-5.4": {"input": 2.50, "cache_read": 0.25, "output": 15.00},
    "gpt-5.2": {"input": 1.75, "cache_read": 0.175, "output": 14.00},
    "gpt-5": {"input": 1.25, "cache_read": 0.125, "output": 10.00},
    "gpt-5-mini": {"input": 0.25, "cache_read": 0.025, "output": 2.00},
}


def _load_pricing() -> dict[str, dict[str, float]]:
    """Load pricing with optional env overrides.

    Env format: MODEL_PRICING_GPT_5_4_INPUT=2.50
    (model key: dots/hyphens replaced with underscores, uppercased)
    """
    pricing = {k: dict(v) for k, v in _DEFAULT_PRICING.items()}
    for model_name, rates in pricing.items():
        env_key = model_name.replace(".", "_").replace("-", "_").upper()
        for rate_type in ("input", "cache_read", "output"):
            env_val = os.getenv(f"MODEL_PRICING_{env_key}_{rate_type.upper()}")
            if env_val is not None:
                rates[rate_type] = float(env_val)
    return pricing


MODEL_PRICING = _load_pricing()


def calculate_cost(
    *,
    model_name: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
) -> float | None:
    """Calculate USD cost from token counts and model pricing.

    Formula:
        cost = (input_tokens - cache_read_tokens) × input_rate
             + cache_read_tokens × cache_read_rate
             + output_tokens × output_rate

    All rates are per 1M tokens. Returns None if model is not in pricing table.
    """
    if not model_name:
        return None

    rates = MODEL_PRICING.get(model_name)
    if not rates:
        logger.debug("No pricing for model %s, cost will be None", model_name)
        return None

    per_m = 1_000_000
    non_cached_input = max(input_tokens - cache_read_tokens, 0)

    cost = (
        (non_cached_input / per_m) * rates["input"]
        + (cache_read_tokens / per_m) * rates["cache_read"]
        + (output_tokens / per_m) * rates["output"]
    )
    return round(cost, 6)
