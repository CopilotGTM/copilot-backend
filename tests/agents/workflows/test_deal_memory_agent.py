"""Regression tests for DealMemoryAgent model configuration."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


def test_deal_memory_agent_uses_azure_responses_with_low_reasoning() -> None:
    """Tool-enabled Terra reasoning must use Azure's Responses endpoint."""
    captured: dict = {}

    def fake_agent(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    with (
        patch("src.agents.workflows.deal_memory.agent.Agent", side_effect=fake_agent),
        patch(
            "src.agents.workflows.deal_memory.agent.get_tools_for_agent",
            return_value=[],
        ),
        patch(
            "src.agents.workflows.deal_memory.agent.openai_config.AZURE_OPENAI_ENDPOINT",
            "https://example.openai.azure.com/",
        ),
        patch(
            "src.agents.workflows.deal_memory.agent.openai_config.AZURE_OPENAI_API_KEY",
            "test-key",
        ),
    ):
        from src.agents.workflows.deal_memory.agent import DealMemoryAgent

        DealMemoryAgent(organisation_id=uuid4(), deal_id=uuid4())

    model = captured["model"]
    assert model.name == "OpenAIResponses"
    assert model.base_url == "https://example.openai.azure.com/openai/v1/"
    assert model.reasoning_effort == "low"


@pytest.mark.asyncio
async def test_deal_memory_agent_rejects_empty_provider_response() -> None:
    """A swallowed provider error must not be persisted as the string ``None``."""
    with (
        patch("src.agents.workflows.deal_memory.agent.Agent") as agno_agent,
        patch(
            "src.agents.workflows.deal_memory.agent.get_tools_for_agent",
            return_value=[],
        ),
        patch(
            "src.agents.workflows.deal_memory.agent.openai_config.AZURE_OPENAI_ENDPOINT",
            "https://example.openai.azure.com/",
        ),
        patch(
            "src.agents.workflows.deal_memory.agent.openai_config.AZURE_OPENAI_API_KEY",
            "test-key",
        ),
    ):
        from src.agents.workflows.deal_memory.agent import DealMemoryAgent

        agno_agent.return_value.arun = AsyncMock(return_value=SimpleNamespace(content=None))
        agent = DealMemoryAgent(organisation_id=uuid4(), deal_id=uuid4())

        with pytest.raises(RuntimeError, match="returned no content"):
            await agent.generate(is_initial_build=True)
