"""Token usage extraction and tracking for agents.

Two extraction paths:
- extract_token_usage(): from Agno RunMetrics (non-streaming RunOutput or streaming RunCompletedEvent)
- extract_token_usage_from_frames(): from accumulated SSE Frame lists (for workers that consume frames)

Cost is calculated via the pricing config when the provider doesn't report it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.infra.messaging.chat_stream_models import Frame

from src.infra.config.model_pricing import calculate_cost
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class TokenUsageData:
    """Uniform token usage data extracted from Agno run metrics."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost: float | None = None
    model_name: str | None = None
    duration_seconds: float | None = None
    time_to_first_token: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }
        if self.reasoning_tokens:
            d["reasoning_tokens"] = self.reasoning_tokens
        if self.cache_read_tokens:
            d["cache_read_tokens"] = self.cache_read_tokens
        if self.cache_write_tokens:
            d["cache_write_tokens"] = self.cache_write_tokens
        if self.cost is not None:
            d["cost"] = self.cost
        if self.model_name:
            d["model_name"] = self.model_name
        if self.duration_seconds is not None:
            d["duration_seconds"] = round(self.duration_seconds, 2)
        if self.time_to_first_token is not None:
            d["time_to_first_token"] = self.time_to_first_token
        return d


def _extract_model_name(metrics: Any, response: Any) -> str | None:
    """Extract model name from RunMetrics.details or response-level field."""
    details = getattr(metrics, "details", None)
    if details:
        for _model_type, model_list in details.items():
            for m in model_list:
                m_id = getattr(m, "id", None)
                if m_id:
                    return str(m_id)
    model = getattr(response, "model", None)
    return str(model) if model else None


def extract_token_usage(response: Any, *, agent_name: str = "Agent") -> TokenUsageData | None:
    """Extract token usage from an Agno RunOutput or RunCompletedEvent.

    Reads top-level RunMetrics fields (already summed by Agno) rather than
    manually iterating details. The details dict is only used to extract
    the model name.
    """
    metrics = getattr(response, "metrics", None)
    if not metrics:
        return None

    model_name = _extract_model_name(metrics, response)

    # Use provider-reported cost if available, otherwise calculate from pricing table
    provider_cost = getattr(metrics, "cost", None)

    usage = TokenUsageData(
        input_tokens=getattr(metrics, "input_tokens", 0) or 0,
        output_tokens=getattr(metrics, "output_tokens", 0) or 0,
        total_tokens=getattr(metrics, "total_tokens", 0) or 0,
        reasoning_tokens=getattr(metrics, "reasoning_tokens", 0) or 0,
        cache_read_tokens=getattr(metrics, "cache_read_tokens", 0) or 0,
        cache_write_tokens=getattr(metrics, "cache_write_tokens", 0) or 0,
        cost=provider_cost,
        model_name=model_name,
        duration_seconds=getattr(metrics, "duration", None),
        time_to_first_token=getattr(metrics, "time_to_first_token", None),
    )

    if usage.cost is None:
        usage.cost = calculate_cost(
            model_name=usage.model_name,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
        )

    logger.info(
        "%s completed: %d input, %d output, %d total tokens (cache_read=%d, cache_write=%d)%s%s",
        agent_name,
        usage.input_tokens,
        usage.output_tokens,
        usage.total_tokens,
        usage.cache_read_tokens,
        usage.cache_write_tokens,
        f", cost=${usage.cost:.4f}" if usage.cost is not None else "",
        f", {usage.duration_seconds:.1f}s" if usage.duration_seconds is not None else "",
    )

    return usage


def extract_token_usage_from_frames(
    frames: list[Frame],
) -> tuple[dict | None, str | None, float | None, float | None]:
    """Extract token usage data from accumulated SSE frames.

    Used by worker services that consume Frame lists (e.g. BotMessageProcessingService).

    Returns:
        Tuple of (token_usage_dict, model_name, cost, duration_seconds)
        or (None, None, None, None) if no token.usage frame found.
    """
    for frame in frames:
        if getattr(frame, "type", "") == "token.usage":
            token_usage = {
                "input_tokens": getattr(frame, "input_tokens", 0),
                "output_tokens": getattr(frame, "output_tokens", 0),
                "total_tokens": getattr(frame, "total_tokens", 0),
                "reasoning_tokens": getattr(frame, "reasoning_tokens", 0),
                "cache_read_tokens": getattr(frame, "cache_read_tokens", 0),
                "cache_write_tokens": getattr(frame, "cache_write_tokens", 0),
            }
            model_name = getattr(frame, "model_name", None)
            cost = getattr(frame, "cost", None)
            duration_seconds = getattr(frame, "duration_seconds", None)
            return token_usage, model_name, cost, duration_seconds
    return None, None, None, None
