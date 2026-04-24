from __future__ import annotations

from src.service.integrations.crm.records.salesforce.deals.participant_normalizer import (
    extract_participants,
)


def test_normalizer_emits_primary_contact_role_and_company_participant() -> None:
    extraction = extract_participants(
        [
            {
                "OpportunityId": "006A",
                "ContactId": "003A",
                "Role": "Decision Maker",
                "IsPrimary": True,
            }
        ],
        company_external_id_by_deal={"006A": "001COMP"},
    )

    roles = {
        (
            participant.participant_type,
            participant.related_external_id,
            participant.relationship_role,
        )
        for participant in extraction.participants
    }
    assert ("contact", "003A", "Decision Maker") in roles
    assert ("contact", "003A", "primary_contact") in roles
    assert ("company", "001COMP", None) in roles
    assert extraction.primary_contact_by_deal["006A"] == "003A"
    assert extraction.contact_count_by_deal["006A"] == 1
    assert extraction.associations[0].association_type_id == "OpportunityContactRole"
