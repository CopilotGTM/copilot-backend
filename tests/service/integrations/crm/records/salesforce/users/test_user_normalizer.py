from uuid import uuid4

from src.service.integrations.crm.records.salesforce.users.user_normalizer import (
    normalize_email,
    normalize_users,
    resolve_matches,
)


def test_normalize_users_maps_profile_name_and_lowercases_email() -> None:
    normalized, skipped = normalize_users(
        [
            {
                "Id": "005A",
                "Email": " Owner@Acme.com ",
                "FirstName": "Alex",
                "LastName": "Morgan",
                "Name": "Alex Morgan",
                "IsActive": False,
                "UserType": "Standard",
                "ProfileId": "00ePROFILE",
                "Profile": {"Name": "Sales User"},
                "LastModifiedDate": "2026-04-23T12:00:00.000+0000",
            }
        ]
    )

    assert skipped == 0
    assert len(normalized) == 1
    user = normalized[0]
    assert user.external_id == "005A"
    assert user.email == "owner@acme.com"
    assert user.user_role == "Sales User"
    assert user.is_active is False
    assert user.properties["salesforce_profile_id"] == "00ePROFILE"
    assert user.properties["salesforce_user_type"] == "Standard"


def test_resolve_matches_reuses_public_user_id_when_email_matches() -> None:
    normalized, _ = normalize_users(
        [
            {
                "Id": "005A",
                "Email": "owner@acme.com",
                "Name": "Alex Morgan",
                "IsActive": True,
                "Profile": {"Name": "Sales User"},
            }
        ]
    )
    app_user_id = uuid4()

    results = resolve_matches(normalized, {"owner@acme.com": app_user_id})

    assert results[0].matched_app_user_id == app_user_id
    assert normalize_email(" Owner@Acme.com ") == "owner@acme.com"
