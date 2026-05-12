from src.service.integrations.crm.records.hubspot.deals.deal_normalizer import (
    extract_participants,
)


def test_extract_participants_merges_default_and_unlabeled_company_associations() -> None:
    raw = {
        "id": "deal-1",
        "associations": {
            "companies": {
                "results": [
                    {"id": "company-1", "type": "deal_to_company_unlabeled"},
                    {"id": "company-1", "type": "deal_to_company"},
                ]
            }
        },
    }

    participants = extract_participants(raw)

    assert len(participants) == 1
    assert participants[0].participant_type == "company"
    assert participants[0].related_external_id == "company-1"
    assert participants[0].relationship_role == "deal_to_company"


def test_extract_participants_merges_default_and_unlabeled_contact_associations() -> None:
    raw = {
        "id": "deal-1",
        "associations": {
            "contacts": {
                "results": [
                    {"id": "contact-1", "type": "deal_to_contact_unlabeled"},
                    {"id": "contact-1", "type": "deal_to_contact"},
                ]
            }
        },
    }

    participants = extract_participants(raw)

    assert len(participants) == 1
    assert participants[0].participant_type == "contact"
    assert participants[0].related_external_id == "contact-1"
    assert participants[0].relationship_role == "deal_to_contact"
