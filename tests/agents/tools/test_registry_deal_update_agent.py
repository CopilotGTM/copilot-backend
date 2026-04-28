"""Tests for deal-update agent tool allowlist and invariants."""

from src.agents.tools.registry import AGENT_TOOLS, ToolName


def test_deal_update_agent_has_exact_write_tools():
    """Keep DealUpdateAgent write surface intentionally limited."""
    tools = AGENT_TOOLS["DealUpdateAgent"]

    assert ToolName.GET_DEAL_MEMORY in tools
    assert ToolName.GET_DEAL_DETAILS in tools
    assert ToolName.GET_PIPELINE_DETAILS in tools
    assert ToolName.UPDATE_DEAL_FIELDS in tools
    assert ToolName.UPDATE_DEAL_LIFECYCLE in tools
    assert ToolName.UPDATE_DEAL_ACCOUNT in tools

    assert ToolName.CREATE_DEAL not in tools
    assert ToolName.ADD_STAKEHOLDER not in tools
    assert ToolName.CREATE_TASK not in tools
    assert ToolName.UPDATE_RISK_BLOCKER not in tools
    assert not hasattr(ToolName, "UPDATE_DEAL_PRIMARY_CONTACT")
