from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from src.agents.orchestrator import run_agent_for_chat
from src.infra.messaging.chat_stream_models import Finalize


class _FakeGeneralAgent:
    def __init__(self, *, context):
        self.context = context

    async def run_stream(self, message_list):
        _ = message_list
        yield Finalize(reason="done")


@pytest.mark.asyncio
async def test_run_agent_for_chat_uses_general_agent_for_current_chat_flows() -> None:
    context = SimpleNamespace()

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
