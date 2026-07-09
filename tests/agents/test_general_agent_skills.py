"""Tests for GeneralAgent skills integration."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

from agno.skills import Skills
from src.agents.context import AgentContext


def test_general_agent_has_skills() -> None:
    """GeneralAgent must configure Agno agent with skills."""
    context = AgentContext(organisation_id=uuid4())
    captured: dict = {}

    def fake_agno_agent(**kwargs):
        captured.update(kwargs)
        mock = MagicMock()
        mock.skills = kwargs.get("skills")
        return mock

    with (
        patch("src.agents.workflows.general.agent.AgnoAgent", side_effect=fake_agno_agent),
        patch("src.agents.workflows.general.agent.get_tools_for_agent", return_value=[]),
    ):
        from src.agents.workflows.general.agent import GeneralAgent

        agent = GeneralAgent(context=context)
        assert agent.general_agent.skills is not None
        assert isinstance(agent.general_agent.skills, Skills)
        assert captured["tool_call_limit"] == 150


def test_general_prompt_does_not_contain_hardcoded_meddic_playbook() -> None:
    """The system prompt should not embed the old MEDDIC playbook section."""
    from src.agents.workflows.general.prompts.general_prompt import get_general_prompt

    prompt = get_general_prompt("TestOrg")
    assert "### Playbook: MEDDIC Assessment" not in prompt


def test_general_prompt_has_skills_assessment_section() -> None:
    """The prompt must have a Skills section that tells the model to check and load skills."""
    from src.agents.workflows.general.prompts.general_prompt import get_general_prompt

    prompt = get_general_prompt("TestOrg")
    assert "## Skills" in prompt
    assert "## Tool Selection" not in prompt
    assert "analytical, action, workflow, writing, scheduling, or structured-assessment" in prompt
    assert "call `get_skill_instructions(skill_name)` before executing" in prompt
    assert "<skills_system>" in prompt


def test_general_prompt_has_compact_datetime_grounding_rule() -> None:
    """The prompt should ground local dates without hardcoding workflow details."""
    from src.agents.workflows.general.prompts.general_prompt import get_general_prompt

    prompt = get_general_prompt("TestOrg")
    assert "## Date and Time Grounding" in prompt
    assert "user timezone" in prompt
    assert "Do not manually convert local times to UTC" in prompt


def test_general_prompt_requires_english_side_effect_inputs() -> None:
    """CRM/tool writes should use English even when the conversation is multilingual."""
    from src.agents.workflows.general.prompts.general_prompt import get_general_prompt

    prompt = get_general_prompt("TestOrg")
    assert "## Side-Effect Input Language" in prompt
    assert "Persisted inputs must default to English" in prompt
    assert "Passageway" in prompt
    assert "Exotel" in prompt


def test_general_prompt_does_not_contain_hardcoded_call_analysis_playbook() -> None:
    """The system prompt should not embed the old Call/Meeting Analysis playbook section."""
    from src.agents.workflows.general.prompts.general_prompt import get_general_prompt

    prompt = get_general_prompt("TestOrg")
    assert "### Playbook: Call / Meeting Analysis" not in prompt


def test_general_prompt_does_not_contain_hardcoded_deal_health_playbook() -> None:
    """The system prompt should not embed the old Deal Health Check playbook section."""
    from src.agents.workflows.general.prompts.general_prompt import get_general_prompt

    prompt = get_general_prompt("TestOrg")
    assert "### Playbook: Deal Health Check" not in prompt


def test_general_prompt_does_not_contain_hardcoded_reminder_instructions() -> None:
    """The prompt should not embed detailed reminder type/recurrence instructions."""
    from src.agents.workflows.general.prompts.general_prompt import get_general_prompt

    prompt = get_general_prompt("TestOrg")
    assert 'reminder_type="static"' not in prompt
    assert 'reminder_type="agentic"' not in prompt
    assert "Do NOT ask the user for their timezone" not in prompt
