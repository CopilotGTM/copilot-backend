from uuid import uuid4

from src.agents.context import AgentContext
from src.service.orgnisation.kai_training_service import (
    effective_fields,
    format_organisation_profile,
)


def test_user_values_override_research_without_removing_untouched_fields() -> None:
    fields, origins = effective_fields(
        {
            "research_values": {
                "about_company": "Researched overview",
                "what_you_sell": "Researched offering",
                "who_you_sell_to": "Researched buyer",
            },
            "user_values": {
                "about_company": "Admin-corrected overview",
                "who_you_sell_to": "",
            },
        }
    )

    assert fields["about_company"] == "Admin-corrected overview"
    assert fields["what_you_sell"] == "Researched offering"
    assert fields["who_you_sell_to"] == ""
    assert origins["about_company"] == "user"
    assert origins["what_you_sell"] == "research"
    assert origins["who_you_sell_to"] == "user"


def test_profile_is_preserved_when_agent_context_is_copied() -> None:
    profile = format_organisation_profile(
        {
            "research_values": {"about_company": "Acme builds sales software."},
            "user_values": {"instructions": "Call the product Acme One."},
        }
    )
    context = AgentContext(organisation_id=uuid4(), organisation_profile=profile)

    assert profile
    assert "Acme builds sales software" in profile
    assert context.with_metadata(source="test").organisation_profile == profile
    assert context.with_message_id("message-id").organisation_profile == profile
