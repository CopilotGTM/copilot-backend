"""Unit tests for stakeholder agent tool registration and read behavior."""

from __future__ import annotations

from pathlib import Path

from src.agents.tools.registry import AGENT_TOOLS, ToolName


def test_registry_does_not_define_inactive_upsert_tool() -> None:
    """The removed upsert tool must not reappear in the public enum."""
    assert "upsert_stakeholder" not in {tool.value for tool in ToolName}


def test_registry_keeps_summary_agent_on_create_if_missing() -> None:
    """SummaryAgent keeps the create-only stakeholder write path."""
    tools = AGENT_TOOLS["SummaryAgent"]
    assert ToolName.CREATE_STAKEHOLDER_IF_MISSING in tools, (
        "SummaryAgent should create net-new external stakeholders discovered in meetings"
    )


def test_stakeholder_read_tools_use_read_service() -> None:
    """The surviving read tool should stay behind StakeholderReadService."""
    source = Path("src/agents/tools/crm/list_stakeholders.py").read_text()
    assert "select(CRMDealParticipant)" not in source
    assert "StakeholderReadService(" in source


def test_registry_does_not_define_separate_stakeholder_details_tool() -> None:
    """The detail read contract is merged into list_deal_stakeholders."""
    assert "get_stakeholder_details" not in {tool.value for tool in ToolName}


def test_registry_does_not_define_manual_stakeholder_enrichment_tool() -> None:
    """Stakeholder enrichment runs through automatic processes, not an agent tool."""
    assert "enrich_stakeholder" not in {tool.value for tool in ToolName}
