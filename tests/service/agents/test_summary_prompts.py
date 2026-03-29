from src.agents.workflows.summary.models import SummaryAttendee, SummaryInput
from src.agents.workflows.summary.prompts import (
    build_summary_user_prompt,
    get_summary_system_prompt,
)


def _sample_input() -> SummaryInput:
    return SummaryInput(
        event_id="event-123",
        deal_id="deal-456",
        attendees=[
            SummaryAttendee(
                email="alex@customer.com",
                name="Alex Rivera",
                response_status="accepted",
                is_organiser=False,
            ),
            SummaryAttendee(
                email="sam@seller.com",
                name="Sam Patel",
                response_status="tentative",
                is_organiser=True,
            ),
        ],
        internal_domains=["seller.com", "seller.ai"],
        current_datetime="2026-03-28T10:00:00+00:00",
    )


def test_summary_system_prompt_replaces_org_name() -> None:
    prompt = get_summary_system_prompt("Acme")
    assert "Acme" in prompt
    assert "{{ORG_NAME}}" not in prompt


def test_summary_system_prompt_does_not_name_tools() -> None:
    prompt = get_summary_system_prompt("Acme")
    assert "get_meeting_transcript" not in prompt
    assert "upsert_stakeholder" not in prompt
    assert "list_deal_meetings" not in prompt


def test_summary_user_prompt_includes_identity_grounding_inputs() -> None:
    prompt = build_summary_user_prompt(_sample_input())
    assert "canonical identity roster" in prompt
    assert "seller.ai, seller.com" in prompt
    assert "Alex Rivera" in prompt
    assert "alex@customer.com" in prompt
    assert "accepted" in prompt
    assert "Sam Patel" in prompt


def test_summary_user_prompt_handles_missing_internal_domains() -> None:
    input_data = _sample_input().model_copy(update={"internal_domains": []})
    prompt = build_summary_user_prompt(input_data)
    assert "None configured" in prompt
