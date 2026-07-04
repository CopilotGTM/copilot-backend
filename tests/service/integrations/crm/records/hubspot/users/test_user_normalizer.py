from src.service.integrations.crm.records.hubspot.users.user_normalizer import (
    normalize_owners,
)


def test_normalize_owners_skips_archived_hubspot_owners() -> None:
    normalized, skipped = normalize_owners(
        [
            {
                "id": "owner-1",
                "email": "archived@example.com",
                "archived": True,
            }
        ]
    )

    assert normalized == []
    assert skipped == 1
