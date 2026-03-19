from __future__ import annotations

from src.workers.chat_worker.services.message_processing_service import (
    FRAME_TYPE_TOKEN_USAGE,
    MessageProcessingService,
)


def test_process_frame_data_captures_token_usage():
    service = object.__new__(MessageProcessingService)
    result_summary: dict = {
        "token_usage": None,
        "model_name": None,
        "cost": None,
        "duration_seconds": None,
    }
    data = {
        "type": FRAME_TYPE_TOKEN_USAGE,
        "input_tokens": 500,
        "output_tokens": 200,
        "total_tokens": 700,
        "reasoning_tokens": 10,
        "cache_read_tokens": 50,
        "cache_write_tokens": 10,
        "cost": 0.01,
        "duration_seconds": 2.5,
        "model_name": "gpt-4o",
    }
    service._process_frame_data(data, result_summary)

    # token_usage JSONB only has token counts — no cost/model/duration
    assert result_summary["token_usage"]["input_tokens"] == 500
    assert result_summary["token_usage"]["total_tokens"] == 700
    assert result_summary["token_usage"]["cache_read_tokens"] == 50
    assert result_summary["token_usage"]["cache_write_tokens"] == 10
    assert "cost" not in result_summary["token_usage"]
    assert "model_name" not in result_summary["token_usage"]
    assert "duration_seconds" not in result_summary["token_usage"]

    # cost, model_name, duration are stored separately
    assert result_summary["model_name"] == "gpt-4o"
    assert result_summary["cost"] == 0.01
    assert result_summary["duration_seconds"] == 2.5


def test_process_frame_data_ignores_non_token_frames():
    service = object.__new__(MessageProcessingService)
    result_summary: dict = {
        "token_usage": None,
        "model_name": None,
        "cost": None,
        "duration_seconds": None,
        "answer_parts": [],
    }
    data = {"type": "answer.delta", "content": "hello"}
    service._process_frame_data(data, result_summary)

    assert result_summary["token_usage"] is None
    assert result_summary["model_name"] is None
    assert result_summary["cost"] is None
    assert result_summary["duration_seconds"] is None
