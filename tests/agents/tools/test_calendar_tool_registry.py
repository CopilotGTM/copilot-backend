"""Tests for exposing calendar tools in registry selections."""

from __future__ import annotations

from uuid import UUID

from src.agents.context import AgentContext
from src.agents.tools.registry import ToolName, get_tools_for_agent


def test_general_agent_exposes_send_calendar_invite_tool() -> None:
    """GeneralAgent tool selection must include invite tool for callable execution."""
    # Contract: GeneralAgent must include a callable calendar tool function so the
    # LLM can execute invite flows from user chat.
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        channel="teams",
    )

    tool_names = {
        tool.__name__ for tool in get_tools_for_agent("GeneralAgent", context, lambda: None)
    }

    assert ToolName.SEND_CALENDAR_INVITE in ToolName
    assert "send_calendar_invite_tool" in tool_names


def test_general_agent_exposes_unified_meeting_list_tool_only() -> None:
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        channel="web",
    )

    tool_names = {
        tool.__name__ for tool in get_tools_for_agent("GeneralAgent", context, lambda: None)
    }

    assert "list_meetings_tool" in tool_names
    assert "list_deal_meetings_tool" not in tool_names
    assert "list_meetings_by_date_tool" not in tool_names


def test_general_agent_exposes_send_meeting_bot_invite_tool() -> None:
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        channel="web",
    )

    tool_names = {
        tool.__name__ for tool in get_tools_for_agent("GeneralAgent", context, lambda: None)
    }

    assert "send_meeting_bot_invite_tool" in tool_names
