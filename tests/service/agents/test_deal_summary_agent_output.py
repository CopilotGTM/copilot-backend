"""Tests for DealSummaryAgent output normalization."""

from __future__ import annotations

import pytest
from src.agents.workflows.deal_summary.agent import DealSummaryAgent
from src.agents.workflows.deal_summary.models import DealHealthStatus, DealSummaryData


def _agent_instance() -> DealSummaryAgent:
    # Bypass __init__ to unit-test output normalization only.
    return DealSummaryAgent.__new__(DealSummaryAgent)


def test_normalize_accepts_schema_instance() -> None:
    agent = _agent_instance()
    content = DealSummaryData(
        deal_summary="Short summary",
        deal_health=DealHealthStatus.HEALTHY,
        deal_health_reason="Strong engagement and clear next steps.",
    )

    result = agent._normalize_summary_output(content)
    assert result == content


def test_normalize_accepts_valid_dict() -> None:
    agent = _agent_instance()

    result = agent._normalize_summary_output(
        {
            "deal_summary": "Structured summary",
            "deal_health": "AT_RISK",
            "deal_health_reason": "Decision maker not fully aligned yet.",
        }
    )

    assert isinstance(result, DealSummaryData)
    assert result.deal_health == DealHealthStatus.AT_RISK


def test_normalize_rejects_plain_text() -> None:
    agent = _agent_instance()

    with pytest.raises(ValueError, match="plain text"):
        agent._normalize_summary_output("Connection error.")


def test_normalize_rejects_invalid_dict() -> None:
    agent = _agent_instance()

    with pytest.raises(ValueError, match="failed validation"):
        agent._normalize_summary_output({"deal_summary": "missing required fields"})
