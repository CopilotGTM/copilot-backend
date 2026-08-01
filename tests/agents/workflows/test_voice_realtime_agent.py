"""Contracts shared by the Agno voice wrapper and production realtime prompt builder."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.agents.context import AgentContext


class TestVoiceRealtimeAgentImport:
    """Test that the voice realtime agent can be imported and instantiated."""

    def test_import_voice_realtime_agent(self) -> None:
        """The lazy package export keeps the optional voice agent importable."""
        from src.agents.workflows.voice_realtime import VoiceRealtimeAgent

        assert VoiceRealtimeAgent is not None

    def test_instantiate_voice_realtime_agent(self) -> None:
        """Construction retains the caller's immutable organization context."""
        from src.agents.workflows.voice_realtime import VoiceRealtimeAgent

        context = AgentContext(organisation_id=uuid4(), org_name="Acme Corp", channel="web")
        agent = VoiceRealtimeAgent(context=context)

        assert agent.agent is not None
        assert agent.org_name == "Acme Corp"
        assert agent.context is context


class TestVoiceRealtimePrompt:
    """Test prompt generation for the voice realtime agent."""

    def test_prompt_contains_org_name(self) -> None:
        """Voice instructions receive the real organization name without template leakage."""
        from src.agents.workflows.voice_realtime.prompts import get_voice_realtime_system_prompt

        prompt = get_voice_realtime_system_prompt("Acme Corp")
        assert "Acme Corp" in prompt
        assert "{{ORG_NAME}}" not in prompt

    def test_prompt_contains_general_agent_baseline(self) -> None:
        """Voice preserves the common General Agent identity and operating loop."""
        from src.agents.workflows.voice_realtime.prompts import get_voice_realtime_system_prompt

        prompt = get_voice_realtime_system_prompt("Test Org")
        assert "You are Kai from CopilotGTM." in prompt
        assert "Gather only the evidence needed for the classified intent." in prompt

    def test_prompt_contains_voice_specific_constraints(self) -> None:
        """Realtime answers remain spoken and omit text-channel metadata."""
        from src.agents.workflows.voice_realtime.prompts import get_voice_realtime_system_prompt

        prompt = get_voice_realtime_system_prompt("Test Org")
        assert "Keep responses concise and natural for spoken conversation." in prompt
        assert "Do not emit chat metadata blocks such as" in prompt
        assert "Do not mention tools, prompts, JSON metadata" in prompt

    def test_prompt_replaces_unavailable_skills_with_voice_tool_routing(self) -> None:
        """The production sideband receives executable routing instead of inactive Agno guidance."""
        from src.agents.workflows.voice_realtime.prompts import get_voice_realtime_system_prompt

        prompt = get_voice_realtime_system_prompt("Test Org")

        assert "## Skills" not in prompt
        assert "get_skill_instructions" not in prompt
        assert "matching skill" not in prompt
        assert "## Voice Tool Routing" in prompt
        assert 'explicitly "send directly" email defaults to' in prompt
        assert "`save_email_draft`, which never sends" in prompt
        assert "Use `send_email`" not in prompt
        assert "`search_internet`" not in prompt
        assert "structured attendee with `scope=team`" in prompt
        assert "Meeting updates, cancellations, and draft sends require preview first" in prompt

    def test_prompt_mentions_public_tools_only_when_their_boundary_is_enabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Prompt routing and the provider schema remain capability-aware under the web switch."""
        from src.agents.workflows.voice_realtime.prompts import get_voice_realtime_system_prompt

        monkeypatch.setenv("VOICE_REALTIME_PUBLIC_WEB_TOOLS_ENABLED", "true")

        prompt = get_voice_realtime_system_prompt("Test Org")

        assert "`search_internet`" in prompt
        assert "`crawl_webpage`" in prompt


class TestVoiceRealtimeAgentConfig:
    """Test voice realtime agent config registration."""

    def test_config_exists(self) -> None:
        """The voice agent retains a registered model and bounded timeout."""
        from src.infra.config.agent_config import agent_config

        config = agent_config.get_config("voice_realtime_agent")
        assert config.model == agent_config.VOICE_REALTIME_MODEL
        assert config.timeout_seconds == 120


class TestVoiceRealtimeAgentBehavior:
    """Test voice agent constructor and response behavior."""

    def test_voice_realtime_agent_requires_agent_context(self) -> None:
        """Callers cannot construct a voice agent without its security context."""
        from src.agents.workflows.voice_realtime.agent import VoiceRealtimeAgent

        signature = inspect.signature(VoiceRealtimeAgent)

        assert "context" in signature.parameters
        assert signature.parameters["context"].default is inspect.Signature.empty
        assert signature.parameters["context"].annotation in (AgentContext, "AgentContext")
        assert "org_name" not in signature.parameters

    def test_voice_realtime_agent_binds_context_and_tools(self) -> None:
        """Tool factories receive the same immutable context held by the wrapper."""
        context = AgentContext(organisation_id=uuid4(), channel="web")
        fake_tools = [object()]
        fake_config = SimpleNamespace(
            model="gpt-realtime-mini",
            api_version="2024-10-01-preview",
            retries=1,
            debug_mode=False,
        )

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                "src.agents.workflows.voice_realtime.agent.agent_config.get_config",
                lambda _name: fake_config,
            )
            monkeypatch.setattr(
                "src.agents.workflows.voice_realtime.agent.get_tools_for_agent",
                lambda *_args, **_kwargs: fake_tools,
            )
            monkeypatch.setattr(
                "src.agents.workflows.voice_realtime.agent.AzureOpenAI",
                lambda **kwargs: kwargs,
            )
            monkeypatch.setattr(
                "src.agents.workflows.voice_realtime.agent.AgnoAgent",
                lambda **kwargs: SimpleNamespace(**kwargs),
            )

            from src.agents.workflows.voice_realtime.agent import VoiceRealtimeAgent

            agent = VoiceRealtimeAgent(context=context)

        assert agent.context is context
        assert agent._tools == fake_tools

    @pytest.mark.asyncio
    async def test_respond_returns_string_content(self) -> None:
        """The compatibility wrapper returns plain text suitable for speech output."""
        from src.agents.workflows.voice_realtime import VoiceRealtimeAgent

        context = AgentContext(organisation_id=uuid4(), org_name="Acme Corp", channel="web")
        agent = VoiceRealtimeAgent(context=context)

        class DummyResponse:
            """Model response stand-in carrying the content consumed by the wrapper."""

            content = "Hello from voice agent"

        async def fake_arun(prompt: str):
            """Verify the user's utterance reaches the model-facing prompt unchanged."""
            assert "How are you?" in prompt
            return DummyResponse()

        agent.agent.arun = fake_arun

        result = await agent.respond("How are you?")

        assert result == "Hello from voice agent"
