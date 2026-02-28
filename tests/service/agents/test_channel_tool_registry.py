from uuid import uuid4

from src.agents.context import AgentContext
from src.agents.tools.registry import CHANNEL_ONLY_TOOLS, ToolName, get_excluded_tools_for_context


def test_slack_supported_crm_tools_are_available_on_slack_and_teams() -> None:
    slack_supported_crm_tools = (
        ToolName.CREATE_DEAL,
        ToolName.ADD_DEAL_NOTE,
        ToolName.ADD_STAKEHOLDER,
        ToolName.UPDATE_DEAL_STAGE,
    )

    for tool_name in slack_supported_crm_tools:
        assert CHANNEL_ONLY_TOOLS[tool_name] == {"teams", "slack"}


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
