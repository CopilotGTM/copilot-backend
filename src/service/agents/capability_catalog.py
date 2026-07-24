"""Code-owned tool and skill catalog for user-agent configuration.

The agent editor calls this through the user API. Keys come from executable
backend registrations rather than the database, so saved definitions cannot
refer to tenant-uploaded code in V1.
"""

from __future__ import annotations

from pathlib import Path

from src.agents.tools.registry import GENERAL_AGENT_TOOLS, TOOL_CATEGORIES
from src.api.schemas.agent_models import AgentCapabilityCatalog, AgentCapabilityItem

DEFAULT_SKILLS_DIR = Path(__file__).resolve().parents[2] / "agents" / "skills"


def discover_skill_keys(skills_dir: Path = DEFAULT_SKILLS_DIR) -> list[str]:
    """Return sorted local skill directories that contain an executable contract."""

    return sorted(
        path.name
        for path in skills_dir.iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    )


def build_agent_capability_catalog(
    skills_dir: Path = DEFAULT_SKILLS_DIR,
) -> AgentCapabilityCatalog:
    """Build editor metadata directly from the backend's capability registries."""

    tools = [
        AgentCapabilityItem(
            key=tool.value,
            label=tool.value.replace("_", " ").title(),
            category=TOOL_CATEGORIES[tool].value,
        )
        for tool in sorted(GENERAL_AGENT_TOOLS, key=lambda item: item.value)
    ]
    skills = [
        AgentCapabilityItem(
            key=key,
            label=key.replace("-", " ").replace("_", " ").title(),
            category="skill",
        )
        for key in discover_skill_keys(skills_dir)
    ]
    return AgentCapabilityCatalog(tools=tools, skills=skills)
