from uuid import uuid4

from src.service.integrations.crm.records.salesforce.users import user_normalizer
from src.service.integrations.crm.records.salesforce.users.user_normalizer import (
    normalize_email,
    normalize_users,
    resolve_matches,
)


def test_normalize_users_maps_profile_name_and_lowercases_email() -> None:
    normalized, skipped, skipped_inactive = normalize_users(
        [
            {
                "Id": "005A",
                "Email": " Owner@Acme.com ",
                "FirstName": "Alex",
                "LastName": "Morgan",
                "Name": "Alex Morgan",
                "IsActive": True,
                "UserType": "Standard",
                "ProfileId": "00ePROFILE",
                "Profile": {"Name": "Sales User"},
                "LastModifiedDate": "2026-04-23T12:00:00.000+0000",
            }
        ]
    )

    assert skipped == 0
    assert skipped_inactive == 0
    assert len(normalized) == 1
    user = normalized[0]
    assert user.external_id == "005A"
    assert user.email == "owner@acme.com"
    assert user.user_role == "Sales User"
    assert user.is_active is True
    assert user.properties["salesforce_profile_id"] == "00ePROFILE"
    assert user.properties["salesforce_user_type"] == "Standard"


def test_normalize_users_skips_inactive_salesforce_users() -> None:
    normalized, skipped, skipped_inactive = normalize_users(
        [
            {
                "Id": "005A",
                "Email": "inactive@acme.com",
                "Name": "Inactive User",
                "IsActive": False,
            }
        ]
    )

    assert normalized == []
    assert skipped == 0
    assert skipped_inactive == 1


def test_resolve_matches_reuses_public_user_id_when_email_matches() -> None:
    normalized, _, _ = normalize_users(
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


def test_build_owner_backfill_mapping_uses_crm_user_as_canonical_owner() -> None:
    normalized, _, _ = normalize_users(
        [
            {
                "Id": "005A",
                "Email": "owner@acme.com",
                "Name": "Alex Morgan",
                "IsActive": True,
            }
        ]
    )
    crm_user_id = uuid4()

    assert hasattr(user_normalizer, "build_owner_backfill_mapping")
    owner_mapping, fk_only_mapping = user_normalizer.build_owner_backfill_mapping(
        users=normalized,
        user_pk_map={"005A": crm_user_id},
    )

    assert owner_mapping == {"005A": (crm_user_id, "Alex Morgan", None, "owner@acme.com")}
    assert fk_only_mapping == {"005A": (crm_user_id, None, None, None)}
