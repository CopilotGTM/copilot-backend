"""Contracts for the code-owned capability catalog exposed to agent editors."""

from pathlib import Path

from src.agents.tools.registry import ToolName
from src.service.agents.capability_catalog import build_agent_capability_catalog


def test_catalog_lists_tool_metadata_and_only_skill_directories(tmp_path: Path) -> None:
    # Only directories containing SKILL.md are executable skills; nearby documentation is ignored.
    skill_dir = tmp_path / "pipeline-review"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Pipeline review", encoding="utf-8")
    (tmp_path / "README.md").write_text("not a skill", encoding="utf-8")
    (tmp_path / "incomplete-skill").mkdir()

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
