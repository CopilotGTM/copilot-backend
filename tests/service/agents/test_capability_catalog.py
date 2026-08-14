"""Contracts for the code-owned capability catalog exposed to agent editors."""

from pathlib import Path

from src.agents.tools.registry import ToolName
from src.service.agents.capability_catalog import (
    build_agent_capability_catalog,
    discover_skill_keys,
)


def test_catalog_lists_tool_metadata_and_only_skill_directories(tmp_path: Path) -> None:
    # Only directories containing SKILL.md are executable skills; nearby documentation is ignored.
    skill_dir = tmp_path / "pipeline-review"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Pipeline review", encoding="utf-8")
    (tmp_path / "README.md").write_text("not a skill", encoding="utf-8")
    (tmp_path / "incomplete-skill").mkdir()
    control_plane_skill = tmp_path / "agent-builder"
    control_plane_skill.mkdir()
    (control_plane_skill / "SKILL.md").write_text("# Agent builder", encoding="utf-8")

    catalog = build_agent_capability_catalog(skills_dir=tmp_path)

    search_deals = next(item for item in catalog.tools if item.key == ToolName.SEARCH_DEALS.value)
    assert search_deals.label == "Search Deals"
    assert search_deals.category == "crm"
    assert ToolName.GET_BRIEFING.value not in {item.key for item in catalog.tools}
    assert [item.key for item in catalog.skills] == ["pipeline-review"]
    assert [item.model_dump() for item in catalog.output_delivery_channels] == [
        {
            "key": "email_notification",
            "label": "Email notification",
            "description": "Email successful output to the authorized recipients of each run.",
            "sender_mode": "platform",
            "body_source": "agent_output_markdown",
        }
    ]


def test_catalog_excludes_general_agent_control_plane_tools() -> None:
    """A configured agent cannot grant itself agent-management capabilities."""
    tool_keys = {item.key for item in build_agent_capability_catalog().tools}

    assert ToolName.INSPECT_AGENT_BUILDER.value not in tool_keys
    assert ToolName.CHANGE_AGENT_BUILDER.value not in tool_keys


def test_catalog_excludes_general_agent_control_plane_skill() -> None:
    """A configured agent cannot grant itself the agent-builder workflow."""
    assert "agent-builder" not in discover_skill_keys()
    assert "agent-builder" not in {item.key for item in build_agent_capability_catalog().skills}
