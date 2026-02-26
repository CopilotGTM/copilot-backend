from src.agents.workflows.general.prompts.general_prompt import get_general_prompt


def test_general_prompt_marks_crm_write_actions_as_teams_and_slack() -> None:
    prompt = get_general_prompt("Acme")
    assert "**CRM Write Actions** (Teams and Slack)" in prompt
    assert "**CRM Write Actions** (Teams channel only)" not in prompt


def test_general_prompt_instructs_create_deal_with_name_only() -> None:
    prompt = get_general_prompt("Acme")
    assert "call `CreateDeal` as soon as the deal name is known" in prompt
    assert "do NOT ask for company/account before calling the tool" in prompt
