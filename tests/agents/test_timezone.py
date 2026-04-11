from __future__ import annotations

from uuid import uuid4

from src.agents.context import AgentContext
from src.agents.timezone import DEFAULT_AGENT_TIMEZONE, resolve_agent_timezone_identifier
from src.agents.workflows.general.agent import GeneralAgent


def test_resolve_agent_timezone_identifier_uses_valid_user_timezone() -> None:
    assert (
        resolve_agent_timezone_identifier({"user_timezone": "Asia/Kolkata"}) == "Asia/Kolkata"
    )


def test_resolve_agent_timezone_identifier_falls_back_to_utc_for_invalid_timezone() -> None:
    assert (
        resolve_agent_timezone_identifier({"user_timezone": "Mars/Olympus_Mons"})
        == DEFAULT_AGENT_TIMEZONE
    )


def test_general_agent_enables_datetime_context_with_user_timezone(mocker) -> None:
    captured_kwargs: dict = {}

    class _FakeAgnoAgent:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)

    mocker.patch("src.agents.workflows.general.agent.get_tools_for_agent", return_value=[])
    mocker.patch(
        "src.agents.workflows.general.agent.AzureOpenAI",
        side_effect=lambda **kwargs: kwargs,
    )
    mocker.patch("src.agents.workflows.general.agent.LocalSkills", side_effect=lambda path: path)
    mocker.patch(
        "src.agents.workflows.general.agent.Skills",
        side_effect=lambda *, loaders: {"loaders": loaders},
    )
    mocker.patch("src.agents.workflows.general.agent.AgnoAgent", _FakeAgnoAgent)

    GeneralAgent(
        context=AgentContext(
            organisation_id=uuid4(),
            metadata={"user_timezone": "America/Los_Angeles"},
        )
    )

    assert captured_kwargs["add_datetime_to_context"] is True
    assert captured_kwargs["timezone_identifier"] == "America/Los_Angeles"


def test_general_agent_uses_anchored_datetime_when_provided(mocker) -> None:
    captured_kwargs: dict = {}

    class _FakeAgnoAgent:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)

    mocker.patch("src.agents.workflows.general.agent.get_tools_for_agent", return_value=[])
    mocker.patch(
        "src.agents.workflows.general.agent.AzureOpenAI",
        side_effect=lambda **kwargs: kwargs,
    )
    mocker.patch("src.agents.workflows.general.agent.LocalSkills", side_effect=lambda path: path)
    mocker.patch(
        "src.agents.workflows.general.agent.Skills",
        side_effect=lambda *, loaders: {"loaders": loaders},
    )
    mocker.patch("src.agents.workflows.general.agent.AgnoAgent", _FakeAgnoAgent)

    GeneralAgent(
        context=AgentContext(
            organisation_id=uuid4(),
            metadata={
                "user_timezone": "Asia/Kolkata",
                "current_datetime": "2026-04-11T18:30:00+00:00",
            },
        )
    )

    assert captured_kwargs["add_datetime_to_context"] is False
    assert "2026-04-12T00:00:00+05:30" in captured_kwargs["additional_context"]
