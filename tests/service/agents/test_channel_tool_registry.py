from src.agents.tools.registry import CHANNEL_ONLY_TOOLS, ToolName


def test_slack_supported_crm_tools_are_available_on_slack_and_teams() -> None:
    slack_supported_crm_tools = (
        ToolName.CREATE_DEAL,
        ToolName.ADD_DEAL_NOTE,
        ToolName.ADD_STAKEHOLDER,
        ToolName.UPDATE_DEAL_STAGE,
    )

    for tool_name in slack_supported_crm_tools:
        assert CHANNEL_ONLY_TOOLS[tool_name] == {"teams", "slack"}


def test_reminder_tools_remain_teams_only() -> None:
    teams_only_reminder_tools = (
        ToolName.CREATE_REMINDER,
        ToolName.LIST_REMINDERS,
        ToolName.CANCEL_REMINDER,
    )

    for tool_name in teams_only_reminder_tools:
        assert CHANNEL_ONLY_TOOLS[tool_name] == {"teams"}
