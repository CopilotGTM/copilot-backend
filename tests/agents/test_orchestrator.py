from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from src.agents.context import AgentContext
from src.agents.orchestrator import run_agent_for_chat
from src.infra.messaging.chat_stream_models import Finalize


class _FakeGeneralAgent:
    contexts = []

    def __init__(self, *, context):
        self.context = context
        self.contexts.append(context)

    async def run_stream(self, message_list, *, images=None, attachment_text=None):
        _ = message_list
        _ = images
        _ = attachment_text
        yield Finalize(reason="done")


@pytest.mark.asyncio
async def test_run_agent_for_chat_uses_general_agent_for_current_chat_flows() -> None:
    context = AgentContext(organisation_id=uuid4())
    _FakeGeneralAgent.contexts = []

    with patch("src.agents.orchestrator.GeneralAgent", _FakeGeneralAgent):
        frames = []
        async for frame in run_agent_for_chat(
            chat_type="SLACK_CHAT",
            messages=[{"role": "user", "content": "hi"}],
            context=context,
        ):
            frames.append(frame)

    assert len(frames) == 1
    assert frames[0].type == "finalize"
    assert _FakeGeneralAgent.contexts[0].metadata["latest_user_text"] == "hi"


@pytest.mark.asyncio
async def test_run_agent_for_chat_adds_temporal_metadata_before_tool_registration() -> None:
    context = AgentContext(
        organisation_id=uuid4(),
        metadata={"current_datetime": "2026-05-01T00:00:00+00:00"},
    )
    _FakeGeneralAgent.contexts = []

    with patch("src.agents.orchestrator.GeneralAgent", _FakeGeneralAgent):
        async for _frame in run_agent_for_chat(
            chat_type="GENERAL_CHAT",
            messages=[{"role": "user", "content": "last 5 calls"}],
            context=context,
        ):
            pass

    # The GeneralAgent registers tools in __init__, so temporal defaults must be
    # present on the context before the agent object is constructed.
    assert _FakeGeneralAgent.contexts[0].metadata["temporal_direction"] == "past"
    assert _FakeGeneralAgent.contexts[0].metadata["temporal_limit"] == 5


@pytest.mark.asyncio
async def test_run_agent_for_chat_resolves_upcoming_meetings_before_agent() -> None:
    context = AgentContext(organisation_id=uuid4(), user_id=uuid4(), channel="whatsapp")
    _FakeGeneralAgent.contexts = []
    meetings = [
        {
            "event_id": "event-1",
            "title": "Product Demo",
            "start_at": "2026-07-14T10:30:00+00:00",
            "end_at": "2026-07-14T11:30:00+00:00",
            "organiser_email": "nikhil@example.com",
            "attendee_count": 4,
            "has_transcript": False,
            "has_summary": False,
            "has_prep": False,
            "deal": None,
        },
        {
            "event_id": "event-2",
            "title": "Daily Catchup",
            "start_at": "2026-07-15T06:30:00+00:00",
            "end_at": "2026-07-15T07:00:00+00:00",
            "organiser_email": "mrinmoy@example.com",
            "attendee_count": 3,
            "has_transcript": False,
            "has_summary": False,
            "has_prep": False,
            "deal": None,
        },
    ]

    with (
        patch("src.agents.orchestrator.GeneralAgent", _FakeGeneralAgent),
        patch("src.agents.context_resolution.list_meetings", new=AsyncMock(return_value=meetings)),
    ):
        async for _frame in run_agent_for_chat(
            chat_type="WHATSAPP_CHAT",
            messages=[{"role": "user", "content": "Prepare me for my upcoming meeting"}],
            context=context,
        ):
            pass

    resolution = _FakeGeneralAgent.contexts[0].metadata["context_resolution"]
    assert resolution["status"] == "multiple"
    assert resolution["object_type"] == "meeting"
    assert resolution["checked"] == ["calendar meetings"]
    assert [candidate["title"] for candidate in resolution["candidates"]] == [
        "Product Demo",
        "Daily Catchup",
    ]


@pytest.mark.asyncio
async def test_run_agent_for_chat_records_no_meeting_match_before_agent() -> None:
    context = AgentContext(organisation_id=uuid4(), user_id=uuid4())
    _FakeGeneralAgent.contexts = []

    with (
        patch("src.agents.orchestrator.GeneralAgent", _FakeGeneralAgent),
        patch("src.agents.context_resolution.list_meetings", new=AsyncMock(return_value=[])),
    ):
        async for _frame in run_agent_for_chat(
            chat_type="GENERAL_CHAT",
            messages=[{"role": "user", "content": "summarize my last call"}],
            context=context,
        ):
            pass

    resolution = _FakeGeneralAgent.contexts[0].metadata["context_resolution"]
    assert resolution == {
        "status": "no_match",
        "object_type": "meeting",
        "reference": "summarize my last call",
        "checked": ["calendar meetings"],
        "candidates": [],
    }


@pytest.mark.asyncio
async def test_run_agent_for_chat_does_not_resolve_new_meeting_invites() -> None:
    context = AgentContext(organisation_id=uuid4(), user_id=uuid4())
    _FakeGeneralAgent.contexts = []
    list_meetings = AsyncMock(return_value=[])

    with (
        patch("src.agents.orchestrator.GeneralAgent", _FakeGeneralAgent),
        patch("src.agents.context_resolution.list_meetings", new=list_meetings),
    ):
        async for _frame in run_agent_for_chat(
            chat_type="GENERAL_CHAT",
            messages=[{"role": "user", "content": "schedule a meeting with Nikhil tomorrow"}],
            context=context,
        ):
            pass

    assert "context_resolution" not in _FakeGeneralAgent.contexts[0].metadata
    list_meetings.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_agent_for_chat_still_resolves_existing_meeting_bot_target() -> None:
    context = AgentContext(organisation_id=uuid4(), user_id=uuid4())
    _FakeGeneralAgent.contexts = []
    list_meetings = AsyncMock(
        return_value=[
            {
                "event_id": "event-1",
                "title": "Customer Call",
                "start_at": "2026-07-14T10:30:00+00:00",
                "end_at": "2026-07-14T11:30:00+00:00",
                "organiser_email": "customer@example.com",
                "attendee_count": 2,
                "has_transcript": False,
                "has_summary": False,
                "has_prep": False,
                "deal": None,
            }
        ]
    )

    with (
        patch("src.agents.orchestrator.GeneralAgent", _FakeGeneralAgent),
        patch("src.agents.context_resolution.list_meetings", new=list_meetings),
    ):
        async for _frame in run_agent_for_chat(
            chat_type="GENERAL_CHAT",
            messages=[{"role": "user", "content": "send a bot to my next meeting"}],
            context=context,
        ):
            pass

    resolution = _FakeGeneralAgent.contexts[0].metadata["context_resolution"]
    assert resolution["status"] == "one_match"
    assert resolution["candidates"][0]["title"] == "Customer Call"
    list_meetings.assert_awaited_once()
