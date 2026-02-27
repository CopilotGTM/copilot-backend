"""Tests for Agno skills loading and validation."""

from pathlib import Path

from agno.skills import LocalSkills, Skills

SKILLS_DIR = Path(__file__).resolve().parents[2] / "src" / "agents" / "skills"


def test_skills_directory_exists() -> None:
    """The skills directory must exist at src/agents/skills/."""
    assert SKILLS_DIR.is_dir(), f"Skills directory not found: {SKILLS_DIR}"


def test_medpic_skill_loads() -> None:
    """The medpic-assessment skill must load without validation errors."""
    loader = LocalSkills(str(SKILLS_DIR))
    skills = loader.load()
    skill_names = [skill.name for skill in skills]
    assert "medpic-assessment" in skill_names


def test_medpic_skill_has_description() -> None:
    """The MEDPIC skill must have a non-empty description."""
    loader = LocalSkills(str(SKILLS_DIR))
    skills = loader.load()
    medpic = next(skill for skill in skills if skill.name == "medpic-assessment")
    assert medpic.description
    assert len(medpic.description) <= 1024


def test_medpic_skill_has_instructions() -> None:
    """The MEDPIC skill must have non-empty instructions."""
    loader = LocalSkills(str(SKILLS_DIR))
    skills = loader.load()
    medpic = next(skill for skill in skills if skill.name == "medpic-assessment")
    assert medpic.instructions
    assert "MEDPIC" in medpic.instructions


def test_medpic_skill_uses_data_first_tool_guidance() -> None:
    """The skill should use data-first guidance and optional bracket tool hints."""
    loader = LocalSkills(str(SKILLS_DIR))
    skills = loader.load()
    medpic = next(skill for skill in skills if skill.name == "medpic-assessment")
    assert "Use the best available tools for each data signal." in medpic.instructions
    assert "Bracket hints are examples, not hard constraints." in medpic.instructions
    assert "[GetDealDetails, GetDealChangeHistory]" in medpic.instructions


def test_medpic_skill_has_core_references() -> None:
    """The MEDPIC skill must include required reference files."""
    loader = LocalSkills(str(SKILLS_DIR))
    skills = loader.load()
    medpic = next(skill for skill in skills if skill.name == "medpic-assessment")
    assert "scoring-rubric.md" in medpic.references
    assert "evidence-quality-rules.md" in medpic.references
    assert "output-guidelines.md" in medpic.references


def test_skills_object_provides_tools() -> None:
    """Skills object must provide skill access tools."""
    skills = Skills(loaders=[LocalSkills(str(SKILLS_DIR))])
    tools = skills.get_tools()
    tool_names = [tool.name for tool in tools]
    assert "get_skill_instructions" in tool_names
    assert "get_skill_reference" in tool_names
    assert "get_skill_script" in tool_names


def test_deal_health_check_skill_loads() -> None:
    """The deal-health-check skill must load without validation errors."""
    loader = LocalSkills(str(SKILLS_DIR))
    skills = loader.load()
    skill_names = [skill.name for skill in skills]
    assert "deal-health-check" in skill_names


def test_deal_health_check_skill_has_description() -> None:
    """The deal-health-check skill must have a non-empty description."""
    loader = LocalSkills(str(SKILLS_DIR))
    skills = loader.load()
    skill = next(s for s in skills if s.name == "deal-health-check")
    assert skill.description
    assert len(skill.description) <= 1024


def test_deal_health_check_skill_has_instructions() -> None:
    """The deal-health-check skill must have non-empty instructions."""
    loader = LocalSkills(str(SKILLS_DIR))
    skills = loader.load()
    skill = next(s for s in skills if s.name == "deal-health-check")
    assert skill.instructions
    assert "Deal Health Check" in skill.instructions


def test_deal_health_check_skill_has_core_references() -> None:
    """The deal-health-check skill must include required reference files."""
    loader = LocalSkills(str(SKILLS_DIR))
    skills = loader.load()
    skill = next(s for s in skills if s.name == "deal-health-check")
    assert "scoring-rubric.md" in skill.references
    assert "evidence-quality-rules.md" in skill.references
    assert "output-guidelines.md" in skill.references


def test_deal_health_check_skill_uses_data_first_tool_guidance() -> None:
    """The skill should use data-first guidance and optional bracket tool hints."""
    loader = LocalSkills(str(SKILLS_DIR))
    skills = loader.load()
    skill = next(s for s in skills if s.name == "deal-health-check")
    assert "Use the best available tools for each data signal." in skill.instructions
    assert "Bracket hints are examples, not hard constraints." in skill.instructions
    assert "[GetDealDetails, GetDealChangeHistory]" in skill.instructions


def test_call_analysis_skill_loads() -> None:
    """The call-analysis skill must load without validation errors."""
    loader = LocalSkills(str(SKILLS_DIR))
    skills = loader.load()
    skill_names = [skill.name for skill in skills]
    assert "call-analysis" in skill_names


def test_call_analysis_skill_has_description() -> None:
    """The call-analysis skill must have a non-empty description."""
    loader = LocalSkills(str(SKILLS_DIR))
    skills = loader.load()
    skill = next(s for s in skills if s.name == "call-analysis")
    assert skill.description
    assert len(skill.description) <= 1024


def test_call_analysis_skill_has_instructions() -> None:
    """The call-analysis skill must have non-empty instructions."""
    loader = LocalSkills(str(SKILLS_DIR))
    skills = loader.load()
    skill = next(s for s in skills if s.name == "call-analysis")
    assert skill.instructions
    assert "Call" in skill.instructions


def test_call_analysis_skill_has_core_references() -> None:
    """The call-analysis skill must include required reference files."""
    loader = LocalSkills(str(SKILLS_DIR))
    skills = loader.load()
    skill = next(s for s in skills if s.name == "call-analysis")
    assert "scoring-rubric.md" in skill.references
    assert "evidence-quality-rules.md" in skill.references
    assert "output-guidelines.md" in skill.references


def test_call_analysis_skill_uses_data_first_tool_guidance() -> None:
    """The skill should use data-first guidance and optional bracket tool hints."""
    loader = LocalSkills(str(SKILLS_DIR))
    skills = loader.load()
    skill = next(s for s in skills if s.name == "call-analysis")
    assert "Use the best available tools for each data signal." in skill.instructions
    assert "Bracket hints are examples, not hard constraints." in skill.instructions
    assert "[GetMeetingDetails" in skill.instructions


def test_skills_system_prompt_snippet_mentions_medpic() -> None:
    """Skills prompt snippet should include medpic-assessment."""
    skills = Skills(loaders=[LocalSkills(str(SKILLS_DIR))])
    snippet = skills.get_system_prompt_snippet()
    assert "medpic-assessment" in snippet


def test_skills_system_prompt_snippet_mentions_deal_health_check() -> None:
    """Skills prompt snippet should include deal-health-check."""
    skills = Skills(loaders=[LocalSkills(str(SKILLS_DIR))])
    snippet = skills.get_system_prompt_snippet()
    assert "deal-health-check" in snippet


def test_reminders_skill_loads() -> None:
    """The reminders skill must load without validation errors."""
    loader = LocalSkills(str(SKILLS_DIR))
    skills = loader.load()
    skill_names = [skill.name for skill in skills]
    assert "reminders" in skill_names


def test_reminders_skill_has_description() -> None:
    """The reminders skill must have a non-empty description."""
    loader = LocalSkills(str(SKILLS_DIR))
    skills = loader.load()
    skill = next(s for s in skills if s.name == "reminders")
    assert skill.description
    assert len(skill.description) <= 1024


def test_reminders_skill_has_instructions() -> None:
    """The reminders skill must have non-empty instructions."""
    loader = LocalSkills(str(SKILLS_DIR))
    skills = loader.load()
    skill = next(s for s in skills if s.name == "reminders")
    assert skill.instructions
    assert "Static" in skill.instructions
    assert "Agentic" in skill.instructions


def test_reminders_skill_has_core_references() -> None:
    """The reminders skill must include required reference files."""
    loader = LocalSkills(str(SKILLS_DIR))
    skills = loader.load()
    skill = next(s for s in skills if s.name == "reminders")
    assert "recurrence-guide.md" in skill.references
    assert "classification-examples.md" in skill.references
    assert "agentic-prompt-patterns.md" in skill.references


def test_reminders_skill_covers_classification_and_parameters() -> None:
    """The reminders skill should cover type classification and parameter details."""
    loader = LocalSkills(str(SKILLS_DIR))
    skills = loader.load()
    skill = next(s for s in skills if s.name == "reminders")
    assert "reminder_type" in skill.instructions
    assert "trigger_time" in skill.instructions
    assert "CreateReminder" in skill.instructions
    assert "ListReminders" in skill.instructions
    assert "CancelReminder" in skill.instructions


def test_skills_system_prompt_snippet_mentions_call_analysis() -> None:
    """Skills prompt snippet should include call-analysis."""
    skills = Skills(loaders=[LocalSkills(str(SKILLS_DIR))])
    snippet = skills.get_system_prompt_snippet()
    assert "call-analysis" in snippet


def test_skills_system_prompt_snippet_mentions_reminders() -> None:
    """Skills prompt snippet should include reminders."""
    skills = Skills(loaders=[LocalSkills(str(SKILLS_DIR))])
    snippet = skills.get_system_prompt_snippet()
    assert "reminders" in snippet
