"""Regression test for the contact research summary prompt contract."""

from src.agents.workflows.contact_research_summary.prompts import (
    get_contact_research_summary_system_prompt,
)


def test_contact_research_list_sections_require_markdown_bullets() -> None:
    """Career facts stay separately renderable while the overview remains prose."""
    prompt = get_contact_research_summary_system_prompt("Example Seller")
    sections = {
        heading: " ".join(prompt.split(f"## {heading}", 1)[1].split("## ", 1)[0].split())
        for heading in (
            "Professional Overview",
            "Experience",
            "Education",
            "Affiliations",
            "Professional Links",
        )
    }

    assert "prose" in sections["Professional Overview"]
    for heading in ("Experience", "Education", "Affiliations", "Professional Links"):
        assert "Markdown bullet" in sections[heading]
        assert "`- `" in sections[heading]
