"""Tests for SummaryAgent output normalization."""

from __future__ import annotations

from src.agents.workflows.summary.agent import SummaryAgent
from src.agents.workflows.summary.models import SummaryData


def _agent_instance() -> SummaryAgent:
    return SummaryAgent.__new__(SummaryAgent)


def test_normalize_accepts_schema_instance() -> None:
    agent = _agent_instance()
    content = SummaryData(meeting_summary="# Overview\n- Good summary")

    result = agent._normalize_summary_output(content)
    assert result == content


def test_normalize_accepts_valid_dict() -> None:
    agent = _agent_instance()

    result = agent._normalize_summary_output(
        {
            "meeting_summary": "```meeting_summary\n# Overview\n- Structured summary\n```",
        }
    )

    assert isinstance(result, SummaryData)
    assert result.meeting_summary == "# Overview\n- Structured summary"


def test_normalize_accepts_plain_markdown_string() -> None:
    agent = _agent_instance()

    result = agent._normalize_summary_output("```meeting_summary\n# Overview\n- Plain text\n```")

    assert isinstance(result, SummaryData)
    assert result.meeting_summary == "# Overview\n- Plain text"


def test_normalize_accepts_json_string() -> None:
    agent = _agent_instance()

    result = agent._normalize_summary_output('{"meeting_summary":"# Overview\\n- Json payload"}')

    assert isinstance(result, SummaryData)
    assert result.meeting_summary == "# Overview\n- Json payload"
