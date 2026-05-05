from uuid import uuid4

from src.agents.context import AgentContext
from src.agents.tools.registry import (
    AGENT_TOOLS,
    CHANNEL_ONLY_TOOLS,
    VOICE_REALTIME_WRITE_EXCLUDED_TOOLS,
    ToolName,
    ToolRegistry,
    get_excluded_tools_for_context,
)
from src.service.voice_realtime.tool_manifest import build_voice_allowed_tools


def test_slack_supported_crm_tools_are_available_on_slack_and_teams() -> None:
    slack_supported_crm_tools = (
        ToolName.ADD_DEAL_NOTE,
        ToolName.ADD_STAKEHOLDER,
        ToolName.UPDATE_DEAL_STAGE,
    )

    for tool_name in slack_supported_crm_tools:
        assert CHANNEL_ONLY_TOOLS[tool_name] == {"teams", "slack"}


def test_create_deal_is_available_on_web_voice_whatsapp_slack_and_teams() -> None:
    assert CHANNEL_ONLY_TOOLS[ToolName.CREATE_DEAL] == {
        "teams",
        "slack",
        "whatsapp",
        "web",
        "voice",
    }
    assert ToolName.CREATE_DEAL not in VOICE_REALTIME_WRITE_EXCLUDED_TOOLS

    for channel in ("web", "voice", "whatsapp", "slack", "teams"):
        context = AgentContext(organisation_id=uuid4(), channel=channel)
        registry = ToolRegistry(context, lambda: None)

        tools = registry.get_tools_for_agent("GeneralAgent", channel=channel)
        tool_names = {getattr(tool, "name", getattr(tool, "__name__", "")) for tool in tools}

        assert "create_deal_tool" in tool_names


def test_reminder_tools_are_available_on_slack_and_teams() -> None:
    reminder_tools = (
        ToolName.CREATE_REMINDER,
        ToolName.LIST_REMINDERS,
        ToolName.CANCEL_REMINDER,
    )

    for tool_name in reminder_tools:
        assert CHANNEL_ONLY_TOOLS[tool_name] == {"teams", "slack"}


def test_scheduled_alert_context_excludes_reminder_management_tools() -> None:
    context = AgentContext(
        organisation_id=uuid4(),
        metadata={"source": "scheduled_alert"},
    )

    excluded = get_excluded_tools_for_context(context)

    assert excluded == {
        ToolName.CREATE_REMINDER,
        ToolName.LIST_REMINDERS,
        ToolName.CANCEL_REMINDER,
    }


def test_voice_realtime_agent_excludes_backend_write_tools_except_create_deal() -> None:
    assert AGENT_TOOLS["VoiceRealtimeAgent"].isdisjoint(VOICE_REALTIME_WRITE_EXCLUDED_TOOLS)
    assert ToolName.CREATE_DEAL in AGENT_TOOLS["VoiceRealtimeAgent"]
    assert ToolName.SEARCH_INTERNAL_KNOWLEDGE in AGENT_TOOLS["VoiceRealtimeAgent"]
    assert set(build_voice_allowed_tools()).issubset(
        {tool_name.value for tool_name in AGENT_TOOLS["VoiceRealtimeAgent"]}
    )


def test_voice_realtime_agent_web_channel_filters_channel_only_tools() -> None:
    context = AgentContext(organisation_id=uuid4(), channel="web")
    registry = ToolRegistry(context, lambda: None)

    tools = registry.get_tools_for_agent("VoiceRealtimeAgent", channel="web")
    tool_names = {getattr(tool, "name", getattr(tool, "__name__", "")) for tool in tools}

    assert ToolName.SEARCH_INTERNAL_KNOWLEDGE.value in tool_names
    assert "create_deal_tool" in tool_names
    assert ToolName.CREATE_TASK.value not in tool_names
