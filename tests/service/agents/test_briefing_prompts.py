"""Regression tests for the adaptive briefing prompt contract."""

from src.agents.workflows.briefing.models import BriefingInput, EventDetails
from src.agents.workflows.briefing.prompts import (
    build_briefing_user_prompt,
    get_briefing_system_prompt,
)


def _sample_input() -> BriefingInput:
    return BriefingInput(
        deal_id="deal-123",
        event_id="event-456",
        current_datetime="2026-07-15T10:00:00+00:00",
        prior_meetings_count=1,
        emails_exchanged_count=4,
        deal_name="Acme opportunity",
        company_name="Acme",
        organisation_website_urls=["https://seller.example"],
        organisation_summary="Seller helps revenue teams automate account workflows.",
        event_details=EventDetails(
            title="Acme discussion",
            start_at="2026-07-16T10:00:00+00:00",
            attendees_preview=["Alex Buyer"],
        ),
    )


def test_system_prompt_positions_agent_as_grounded_sales_expert() -> None:
    """The role must be consultative sales, not a Solution Engineer persona."""
    prompt = get_briefing_system_prompt("Example Seller")

    assert "sales expert at Example Seller" in prompt
    assert "consultative selling and discovery" in prompt
    assert "Never invent seller capabilities" in prompt
    assert "Solution Engineer" not in prompt
    assert "{{ORG_NAME}}" not in prompt


def test_system_prompt_uses_supplied_seller_context_for_organisation_research() -> None:
    """Seller understanding must stay grounded in its summary and configured websites."""
    prompt = get_briefing_system_prompt("Example Seller")

    assert "supplied seller summary as the primary description" in prompt
    assert "Use `crawl_webpage` only on the supplied seller website URLs" in prompt
    assert "Do not search for Example Seller by name when an official seller URL" in prompt


def test_system_prompt_uses_adaptive_sections_without_call_templates() -> None:
    """The output stays generic and excludes the former call-type boilerplate."""
    prompt = get_briefing_system_prompt("Example Seller")

    assert "Choose only useful sections" in prompt
    assert "Omit unavailable fields and empty sections" in prompt
    assert "Give new attendees more detail" in prompt
    assert "TEMPLATE A" not in prompt
    assert "Call Type & Rationale" not in prompt
    assert "Discovery Question Bank" not in prompt
    assert "Qualification Checklist" not in prompt


def test_system_prompt_locks_tool_order_and_research_fallbacks() -> None:
    """Cheap cached context must precede transcript, Apollo, and web deep dives."""
    prompt = get_briefing_system_prompt("Example Seller")

    assert "first action MUST be `get_meeting_details(event_id)`" in prompt
    assert "gather this baseline in parallel" in prompt
    assert "Prefer cached `stakeholder_research`" in prompt
    assert "Use `get_meeting_transcript` only" in prompt
    prospect_research = prompt[prompt.index("Resolve its official website in this order:") :]
    research_steps = [
        "deal-linked `company_website`",
        "deal-linked `company_domain`",
        "current attendee email domains",
        "use `search_internet`",
        "Use `crawl_webpage`",
    ]
    positions = [prospect_research.index(step) for step in research_steps]
    assert positions == sorted(positions)
    assert "Do not use internal knowledge documents" in prompt
    assert "search_internal_knowledge" not in prompt


def test_system_prompt_rejects_unsafe_attendee_domain_inference() -> None:
    """Missing account URLs must not turn internal, personal, or ambiguous domains into facts."""
    prompt = get_briefing_system_prompt("Example Seller")

    assert "Exclude domains matching the supplied seller website URLs" in prompt
    assert "exclude personal/free-email providers" in prompt
    assert "one external company domain is unambiguous" in prompt
    assert "do not guess when multiple external companies may be represented" in prompt


def test_system_prompt_keeps_external_research_adaptive_and_non_blocking() -> None:
    """Crawler or search failures must yield a partial briefing instead of invented account data."""
    prompt = get_briefing_system_prompt("Example Seller")

    assert "when the prospect business description is missing or thin" in prompt
    assert "only when it materially improves the briefing" in prompt
    assert "External research failure must not prevent an answer" in prompt
    assert "Omit unavailable fields and empty sections" in prompt


def test_user_prompt_includes_configured_seller_context() -> None:
    """The worker-provided seller summary and official URL must reach the model."""
    prompt = build_briefing_user_prompt(_sample_input())

    assert "## Seller Organisation Context" in prompt
    assert "Seller helps revenue teams automate account workflows." in prompt
    assert "https://seller.example" in prompt
    assert "Acme discussion" in prompt
    assert "Alex Buyer" in prompt


def test_user_prompt_omits_empty_seller_context() -> None:
    """Missing optional organisation data must not create a placeholder section."""
    input_data = _sample_input().model_copy(
        update={"organisation_website_urls": [], "organisation_summary": None}
    )

    prompt = build_briefing_user_prompt(input_data)

    assert "## Seller Organisation Context" not in prompt
    assert "Not available" not in prompt
