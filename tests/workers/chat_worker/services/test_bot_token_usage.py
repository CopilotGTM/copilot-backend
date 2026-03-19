from __future__ import annotations

from src.agents.common.token_tracking import extract_token_usage_from_frames
from src.infra.messaging.chat_stream_models import TokenUsageFrame


def test_extract_token_usage_from_frames_found():
    frame = TokenUsageFrame(
        input_tokens=300,
        output_tokens=100,
        total_tokens=400,
        reasoning_tokens=5,
        cache_read_tokens=20,
        cache_write_tokens=10,
        cost=0.008,
        model_name="gpt-4o",
        duration_seconds=1.2,
    )
    usage, model, cost, duration = extract_token_usage_from_frames([frame])
    assert usage is not None
    assert usage["input_tokens"] == 300
    assert usage["cache_read_tokens"] == 20
    assert model == "gpt-4o"
    assert cost == 0.008
    assert duration == 1.2


def test_extract_token_usage_from_frames_not_found():
    usage, model, cost, duration = extract_token_usage_from_frames([])
    assert usage is None
    assert model is None
    assert cost is None
    assert duration is None
