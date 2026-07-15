"""Tool-surface tests for the read-only briefing agent."""

from src.agents.tools.registry import AGENT_TOOLS, ToolName


def test_briefing_agent_has_existing_context_and_research_tools() -> None:
    """Briefings need account, task, webpage, and participant reads without KB access."""
    tools = AGENT_TOOLS["BriefingAgent"]

    assert ToolName.GET_DEAL_DETAILS in tools
    assert ToolName.LIST_DEAL_TASKS in tools
    assert ToolName.SEARCH_INTERNET in tools
    assert ToolName.CRAWL_WEBPAGE in tools
    assert ToolName.RESEARCH_LINKEDIN_PROFILE in tools
    assert ToolName.SEARCH_INTERNAL_KNOWLEDGE not in tools

    assert ToolName.GET_DEAL_MEMORY in tools
    assert ToolName.GET_MEETING_SUMMARY in tools
    assert ToolName.GET_MEETING_TRANSCRIPT in tools
    assert ToolName.LIST_DEAL_STAKEHOLDERS in tools
    assert ToolName.LIST_DEAL_EMAILS in tools
    assert ToolName.LIST_DEAL_NOTES in tools
    assert ToolName.LIST_DEAL_CHANNEL_MESSAGES in tools
