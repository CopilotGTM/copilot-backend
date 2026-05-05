from src.agents.workflows.general.prompts.general_prompt import get_general_prompt


def test_general_prompt_marks_crm_write_actions_as_teams_and_slack() -> None:
    prompt = get_general_prompt("Acme")
    assert (
        "**CRM write behavior on supported channels (Web/Voice/Teams/Slack/WhatsApp):**" in prompt
    )
    assert "**CRM Write Actions** (Teams channel only)" not in prompt


def test_general_prompt_instructs_create_deal_with_name_only() -> None:
    prompt = get_general_prompt("Acme")
    assert "call `CreateDeal` as soon as the deal name is known" in prompt
    assert "creates the deal directly" in prompt
    assert "do NOT ask for company/account before calling the tool" in prompt


def test_general_prompt_does_not_collect_optional_create_deal_fields_via_preview_card() -> None:
    prompt = get_general_prompt("Acme")
    create_deal_section = prompt[
        prompt.index("**CRM write behavior on supported channels") : prompt.index(
            "**Visualizations (Teams only):**"
        )
    ]

    assert "preview card collect/confirm optional details" not in create_deal_section
    assert "optional details can be provided later" in create_deal_section


def test_general_prompt_instructs_direct_stakeholder_and_note_creation() -> None:
    prompt = get_general_prompt("Acme")

    assert "resolve/search the deal first" in prompt
    assert "call `AddStakeholder` directly once deal_id, deal_name, and email are known" in prompt
    assert (
        "call `AddDealNote` directly once deal_id, deal_name, and note_content are known" in prompt
    )
    assert "Do not describe stakeholder or note preview cards as the normal path" in prompt


def test_general_prompt_no_longer_uses_preview_cards_for_normal_stakeholder_or_note_flow() -> None:
    prompt = get_general_prompt("Acme")
    crm_write_section = prompt[
        prompt.index("**CRM write behavior on supported channels") : prompt.index(
            "**Visualizations (Teams only):**"
        )
    ]

    assert "send a preview" not in crm_write_section.lower()
    assert "send a form" not in crm_write_section.lower()
    assert "click confirm" not in crm_write_section.lower()


def test_general_prompt_requires_meaningful_task_title_before_create_task() -> None:
    prompt = get_general_prompt("Acme")

    assert "Create the task directly" in prompt
    assert "once the title is meaningful" in prompt
    assert "If the user only provides assignment or metadata" in prompt
    assert "ask what the task is about" in prompt


def test_general_prompt_does_not_instruct_agent_switch_metadata() -> None:
    prompt = get_general_prompt("Acme")
    assert "switch_agent" not in prompt
