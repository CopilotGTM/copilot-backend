from src.agents.workflows.general.prompts.general_prompt import get_general_prompt


def test_general_prompt_uses_skills_system_instead_of_tool_selection() -> None:
    prompt = get_general_prompt("Acme")

    assert "## Skills" in prompt
    assert "## Tool Selection" not in prompt
    assert "<skills_system>" in prompt
    assert "**CRM Write Actions** (Teams channel only)" not in prompt


def test_general_prompt_has_context_resolution_gate() -> None:
    prompt = get_general_prompt("Acme")

    assert "## Context Resolution Gate" in prompt
    assert "Before asking the user for missing context" in prompt
    assert "ask a grounded choice question" in prompt
    assert "Do not ask generic questions" in prompt


def test_general_prompt_does_not_embed_crm_skill_router_details() -> None:
    prompt = get_general_prompt("Acme")

    assert "`crm-change-workflow`" not in prompt
    assert "`crm-deal-analysis-workflow`" not in prompt
    assert "Read-only CRM/deal analysis" not in prompt
    assert "CRM changes" not in prompt


def test_general_prompt_no_longer_embeds_crm_write_workflow_details() -> None:
    prompt = get_general_prompt("Acme")

    assert "call `CreateDeal` as soon as the deal name is known" not in prompt
    assert "creates the deal directly" not in prompt
    assert "do NOT ask for company/account before calling the tool" not in prompt
    assert "optional details can be provided later" not in prompt


def test_general_prompt_no_longer_embeds_stakeholder_and_note_write_details() -> None:
    prompt = get_general_prompt("Acme")

    assert "call `AddStakeholder` directly once deal_id" not in prompt
    assert "call `AddDealNote` directly once deal_id" not in prompt
    assert "Do not describe stakeholder or note preview cards as the normal path" not in prompt


def test_general_prompt_no_longer_uses_preview_cards_for_normal_stakeholder_or_note_flow() -> None:
    prompt = get_general_prompt("Acme")

    assert "send a preview" not in prompt.lower()
    assert "send a form" not in prompt.lower()
    assert "click confirm" not in prompt.lower()


def test_general_prompt_no_longer_embeds_task_write_workflow_details() -> None:
    prompt = get_general_prompt("Acme")

    assert "Create the task directly" not in prompt
    assert "once the title is meaningful" not in prompt
    assert "If the user only provides assignment or metadata" not in prompt
    assert "ask what the task is about" not in prompt


def test_general_prompt_does_not_embed_email_meeting_skill_router_details() -> None:
    prompt = get_general_prompt("Acme")

    assert "`email-meeting-workflow`" not in prompt
    assert "Email and meeting requests" not in prompt
    assert "sending invites to stakeholders" not in prompt


def test_general_prompt_no_longer_embeds_email_or_meeting_send_workflow_details() -> None:
    prompt = get_general_prompt("Acme")

    assert "Meeting creation requests -> use `send_calendar_invite`" not in prompt
    assert "Meeting bot requests -> use the send-meeting-bot tool" not in prompt
    assert "Email send requests -> use `send_email`" not in prompt
    assert "recipient email, subject, and body are known" not in prompt


def test_general_prompt_does_not_instruct_agent_switch_metadata() -> None:
    prompt = get_general_prompt("Acme")
    assert "switch_agent" not in prompt
