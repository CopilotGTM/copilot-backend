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


def test_direct_crm_write_tools_are_available_on_supported_chat_and_voice_channels() -> None:
    direct_crm_write_tools = (
        ToolName.ADD_DEAL_NOTE,
        ToolName.ADD_STAKEHOLDER,
    )

    for tool_name in direct_crm_write_tools:
        assert CHANNEL_ONLY_TOOLS[tool_name] == {
            "teams",
            "slack",
            "whatsapp",
            "web",
            "voice",
        }
        assert tool_name not in VOICE_REALTIME_WRITE_EXCLUDED_TOOLS

    for channel in ("web", "voice", "whatsapp", "slack", "teams"):
        context = AgentContext(organisation_id=uuid4(), channel=channel)
        registry = ToolRegistry(context, lambda: None)

        tools = registry.get_tools_for_agent("GeneralAgent", channel=channel)
        tool_names = {getattr(tool, "name", getattr(tool, "__name__", "")) for tool in tools}

        assert "add_deal_note_tool" in tool_names
        assert "add_stakeholder_tool" in tool_names


def test_interactive_stage_update_stays_available_on_slack_and_teams() -> None:
    assert CHANNEL_ONLY_TOOLS[ToolName.UPDATE_DEAL_STAGE] == {"teams", "slack"}


def test_general_agent_does_not_expose_visualization_card_tool() -> None:
    assert "create_visualization_card" not in {
        tool_name.value for tool_name in AGENT_TOOLS["GeneralAgent"]
    }
    assert "create_visualization_card" not in {tool_name.value for tool_name in CHANNEL_ONLY_TOOLS}


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


def test_create_contact_and_account_are_available_on_supported_channels() -> None:
    for tool_name in (ToolName.CREATE_CONTACT, ToolName.CREATE_ACCOUNT):
        assert CHANNEL_ONLY_TOOLS[tool_name] == {
            "teams",
            "slack",
            "whatsapp",
            "web",
            "voice",
        }
        assert tool_name not in VOICE_REALTIME_WRITE_EXCLUDED_TOOLS

    for channel in ("web", "voice", "whatsapp", "slack", "teams"):
        context = AgentContext(organisation_id=uuid4(), channel=channel)
        registry = ToolRegistry(context, lambda: None)

        tools = registry.get_tools_for_agent("GeneralAgent", channel=channel)
        tool_names = {getattr(tool, "name", getattr(tool, "__name__", "")) for tool in tools}

        assert "create_contact_tool" in tool_names
        assert "create_account_tool" in tool_names
        assert "search_accounts_tool" in tool_names


def test_reminder_tools_are_available_on_supported_chat_and_voice_channels() -> None:
    reminder_tools = (
        ToolName.CREATE_REMINDER,
        ToolName.LIST_REMINDERS,
        ToolName.CANCEL_REMINDER,
    )

    for tool_name in reminder_tools:
        assert CHANNEL_ONLY_TOOLS[tool_name] == {
            "teams",
            "slack",
            "whatsapp",
            "web",
            "voice",
        }

    assert ToolName.CREATE_REMINDER not in VOICE_REALTIME_WRITE_EXCLUDED_TOOLS
    assert ToolName.LIST_REMINDERS not in VOICE_REALTIME_WRITE_EXCLUDED_TOOLS
    assert ToolName.CANCEL_REMINDER not in VOICE_REALTIME_WRITE_EXCLUDED_TOOLS


def test_send_email_is_available_on_supported_chat_and_voice_channels() -> None:
    assert CHANNEL_ONLY_TOOLS[ToolName.SEND_EMAIL] == {
        "teams",
        "slack",
        "whatsapp",
        "web",
        "voice",
    }
    assert ToolName.SEND_EMAIL not in VOICE_REALTIME_WRITE_EXCLUDED_TOOLS

    for channel in ("web", "voice", "whatsapp", "slack", "teams"):
        context = AgentContext(organisation_id=uuid4(), channel=channel)
        registry = ToolRegistry(context, lambda: None)

        tools = registry.get_tools_for_agent("GeneralAgent", channel=channel)
        tool_names = {getattr(tool, "name", getattr(tool, "__name__", "")) for tool in tools}

        assert "send_email_tool" in tool_names


def test_general_agent_exposes_context_resolution_read_tools_on_web() -> None:
    context = AgentContext(organisation_id=uuid4(), user_id=uuid4(), channel="web")
    registry = ToolRegistry(context, lambda: None)

    tools = registry.get_tools_for_agent("GeneralAgent", channel="web")
    tool_names = {getattr(tool, "name", getattr(tool, "__name__", "")) for tool in tools}

    assert "list_meetings_tool" in tool_names
    assert "get_compressed_transcript_tool" in tool_names
    assert "list_deal_tasks_tool" in tool_names
    assert "list_chat_attachments_tool" in tool_names
    assert "list_deal_meetings_tool" not in tool_names
    assert "list_meetings_by_date_tool" not in tool_names


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
        ToolName.SEND_CALENDAR_INVITE,
        ToolName.SEND_EMAIL,
    }


def test_voice_realtime_agent_excludes_unsupported_backend_write_tools() -> None:
    assert AGENT_TOOLS["VoiceRealtimeAgent"].isdisjoint(VOICE_REALTIME_WRITE_EXCLUDED_TOOLS)
    assert ToolName.CREATE_DEAL in AGENT_TOOLS["VoiceRealtimeAgent"]
    assert ToolName.ADD_DEAL_NOTE in AGENT_TOOLS["VoiceRealtimeAgent"]
    assert ToolName.ADD_STAKEHOLDER in AGENT_TOOLS["VoiceRealtimeAgent"]
    assert ToolName.SEND_EMAIL in AGENT_TOOLS["VoiceRealtimeAgent"]
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
