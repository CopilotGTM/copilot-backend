from __future__ import annotations

from unittest.mock import patch
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
