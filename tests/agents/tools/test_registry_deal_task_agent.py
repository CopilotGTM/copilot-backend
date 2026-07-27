"""Regression checks for DealTaskAgent registry mapping.

If the registry misses a task entry here, task execution fails at runtime with a
tool-not-found error that looks identical to a capability/network issue.
"""

from __future__ import annotations

from src.agents.tools.registry import AGENT_TOOLS, ToolName


def test_deal_task_agent_tools_are_registered() -> None:
    """Ensure DealTaskAgent gets both read and write task tools."""
    names = AGENT_TOOLS["DealTaskAgent"]

    # Read-side visibility is required to avoid duplicate automation on same summary.
    assert ToolName.LIST_DEAL_TASKS in names
    # Write-side actions are gated by registry presence; missing any one breaks sync.
    assert ToolName.CREATE_DEAL_TASK in names
    assert ToolName.COMPLETE_DEAL_TASK in names
    assert ToolName.GET_DEAL_MEMORY in names
    assert ToolName.GET_MEETING_SUMMARY in names
    assert ToolName.GET_COMPRESSED_TRANSCRIPT in names
    assert ToolName.CREATE_TASK not in names
    assert ToolName.LIST_MY_TASKS not in names
