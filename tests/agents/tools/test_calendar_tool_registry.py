"""Tests for exposing calendar tools in registry selections."""

from __future__ import annotations

from uuid import UUID

import pytest
from agno.tools.function import Function
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

    tools = get_tools_for_agent("GeneralAgent", context, lambda: None)
    tool_names = {tool.__name__ for tool in tools}
    send_tool = next(tool for tool in tools if tool.__name__ == "send_calendar_invite_tool")
    attendee_items = Function.from_callable(send_tool).parameters["properties"]["attendees"][
        "items"
    ]["anyOf"]

    assert ToolName.SEND_CALENDAR_INVITE in ToolName
    assert "send_calendar_invite_tool" in tool_names
    assert {item["type"] for item in attendee_items} == {"string", "object"}


def test_general_agent_exposes_calendar_mutation_tools() -> None:
    """GeneralAgent must register organizer update and cancellation tools together."""
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        channel="teams",
    )

    tool_names = {
        tool.__name__ for tool in get_tools_for_agent("GeneralAgent", context, lambda: None)
    }

    assert "update_calendar_meeting_tool" in tool_names
    assert "cancel_calendar_meeting_tool" in tool_names


@pytest.mark.asyncio
async def test_update_tool_accepts_mixed_attendees_through_agno_validation(monkeypatch) -> None:
    """A saved email plus a team-scoped person must cross the real tool boundary."""

    captured: dict = {}

    async def fake_execute(*_args, **kwargs):
        """Capture validated parameters without opening a database session."""
        captured.update(kwargs["parameters"])
        return {"status": "confirmation_required"}

    monkeypatch.setattr(
        "src.agents.tools.calendar.update_calendar_meeting.execute_with_capabilities",
        fake_execute,
    )
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        channel="whatsapp",
    )
    tool = next(
        tool
        for tool in get_tools_for_agent("GeneralAgent", context, lambda: None)
        if tool.__name__ == "update_calendar_meeting_tool"
    )
    agno_function = Function.from_callable(tool)

    result = await agno_function.entrypoint(
        event_id="33333333-3333-3333-3333-333333333333",
        attendees=[
            "bhawesh@copilotgtm.com",
            {"name": "Nikhil Singhal", "scope": "team"},
        ],
        attendee_action="add",
    )

    attendee_schema = agno_function.parameters["properties"]["attendees"]["anyOf"][0]["items"][
        "anyOf"
    ]
    assert {item["type"] for item in attendee_schema} == {"string", "object"}
    assert set(attendee_schema[1]["properties"]) >= {"email", "name", "optional", "scope"}
    assert captured["attendees"][0] == "bhawesh@copilotgtm.com"
    assert captured["attendees"][1].name == "Nikhil Singhal"
    assert captured["attendees"][1].scope == "team"
    assert result["status"] == "confirmation_required"


def test_general_agent_exposes_unified_meeting_list_tool_only() -> None:
    """GeneralAgent should expose one canonical meeting-list entry point."""
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
    """GeneralAgent must retain meeting-bot dispatch alongside calendar tools."""
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        channel="web",
    )

    tool_names = {
        tool.__name__ for tool in get_tools_for_agent("GeneralAgent", context, lambda: None)
    }

    assert "send_meeting_bot_invite_tool" in tool_names
