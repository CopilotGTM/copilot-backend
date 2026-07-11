from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError
from src.api.schemas.crm_deal_write_models import (
    DealAccountUpdateRequest,
    DealCloseRequest,
    DealCreateRequest,
    DealOwnerAssignRequest,
    DealPrimaryContactUpdateRequest,
    DealReopenRequest,
    DealStageMoveRequest,
    DealUpdateRequest,
)
from src.api.schemas.crm_models import DealDetail


def test_deal_detail_schema_exposes_detail_field_definitions() -> None:
    assert "detail_field_definitions" in DealDetail.model_fields


def test_deal_update_request_normalizes_currency_and_optional_text() -> None:
    request = DealUpdateRequest(
        currency=" inr ",
        lead_source="  Inbound  ",
        forecast_category="  Commit ",
    )

    assert request.currency == "INR"
    assert request.lead_source == "Inbound"
    assert request.forecast_category == "Commit"


def test_deal_update_request_accepts_custom_properties() -> None:
    request = DealUpdateRequest(
        custom_properties={
            "custom_steel_target": 100,
            "our_revenue": "25000000",
        }
    )

    assert request.custom_properties == {
        "custom_steel_target": 100,
        "our_revenue": "25000000",
    }


def test_deal_update_request_accepts_custom_columns_as_public_custom_field_name() -> None:
    request = DealUpdateRequest(custom_columns={"our_revenue": "25000000"})

    assert request.custom_columns == {"our_revenue": "25000000"}
    assert request.custom_properties == {"our_revenue": "25000000"}
    assert request.model_dump(exclude_unset=True) == {
        "custom_properties": {"our_revenue": "25000000"}
    }


def test_deal_update_request_rejects_conflicting_custom_field_aliases() -> None:
    with pytest.raises(ValidationError):
        DealUpdateRequest(
            custom_columns={"our_revenue": "25000000"},
            custom_properties={"our_revenue": "30000000"},
        )


def test_deal_update_request_rejects_dedicated_action_fields() -> None:
    with pytest.raises(ValidationError):
        DealUpdateRequest(deal_status="won")  # type: ignore[call-arg]


def test_deal_update_request_rejects_actual_close_date() -> None:
    with pytest.raises(ValidationError):
        DealUpdateRequest(close_date=datetime(2026, 4, 15, tzinfo=UTC))  # type: ignore[call-arg]


def test_deal_create_request_rejects_actual_close_date() -> None:
    with pytest.raises(ValidationError):
        DealCreateRequest(
            name="Acme Deal",
            close_date=datetime(2026, 4, 15, tzinfo=UTC),  # type: ignore[call-arg]
        )


@pytest.mark.parametrize("payload", [{"name": None}, {"name": "   "}])
def test_deal_update_request_rejects_empty_name(payload) -> None:
    with pytest.raises(ValidationError):
        DealUpdateRequest(**payload)


@pytest.mark.parametrize(
    "request_cls,payload",
    [
        (DealCreateRequest, {"name": "Acme Deal", "deal_status": "won"}),
        (
            DealCreateRequest,
            {"name": "Acme Deal", "account": {"name": "Acme", "deal_count": 2}},
        ),
        (
            DealCreateRequest,
            {"name": "Acme Deal", "team_members": [{"user_id": str(UUID(int=1)), "name": "SE"}]},
        ),
        (
            DealCreateRequest,
            {
                "name": "Acme Deal",
                "contact_participants": [{"contact_id": str(UUID(int=2)), "role": "buyer"}],
            },
        ),
        (
            DealPrimaryContactUpdateRequest,
            {"primary_contact_name": "Jane", "email": "jane@example.com"},
        ),
        (DealCloseRequest, {"outcome": "won", "deal_status": "won"}),
        (DealReopenRequest, {"stage_id": str(UUID(int=3)), "deal_status": "open"}),
        (DealStageMoveRequest, {"stage_id": str(UUID(int=4)), "stage_name": "Closed Won"}),
        (DealOwnerAssignRequest, {"owner_user_id": str(UUID(int=5)), "owner_name": "Jane"}),
    ],
)
def test_deal_write_requests_reject_unknown_fields(request_cls, payload) -> None:
    with pytest.raises(ValidationError):
        request_cls(**payload)


def test_primary_contact_request_normalizes_fields() -> None:
    request = DealPrimaryContactUpdateRequest(
        primary_contact_name="  Jane Doe  ",
        primary_contact_email="  jane@example.com ",
    )

    assert request.primary_contact_name == "Jane Doe"
    assert request.primary_contact_email == "jane@example.com"


def test_primary_contact_request_rejects_invalid_email() -> None:
    with pytest.raises(ValidationError):
        DealPrimaryContactUpdateRequest(
            primary_contact_name="Jane Doe",
            primary_contact_email="not-an-email",
        )


def test_primary_contact_request_allows_explicit_clear() -> None:
    request = DealPrimaryContactUpdateRequest(
        primary_contact_name=None,
        primary_contact_email=None,
    )

    assert request.primary_contact_name is None
    assert request.primary_contact_email is None
    assert request.is_clear_request is True


def test_primary_contact_request_accepts_contact_id_update() -> None:
    contact_id = UUID(int=11)

    request = DealPrimaryContactUpdateRequest(primary_contact_id=contact_id)

    assert request.primary_contact_id == contact_id
    assert request.is_clear_request is False


def test_primary_contact_request_allows_contact_id_clear() -> None:
    request = DealPrimaryContactUpdateRequest(primary_contact_id=None)

    assert request.primary_contact_id is None
    assert request.is_clear_request is True


def test_primary_contact_request_rejects_mixed_contact_id_and_legacy_fields() -> None:
    with pytest.raises(ValidationError):
        DealPrimaryContactUpdateRequest(
            primary_contact_id=UUID(int=12),
            primary_contact_email="jane@example.com",
        )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"primary_contact_name": None},
        {"primary_contact_email": None},
        {"primary_contact_name": "   "},
        {"primary_contact_email": "   "},
        {"primary_contact_name": "   ", "primary_contact_email": "   "},
    ],
)
def test_primary_contact_request_requires_non_empty_contact_field(payload) -> None:
    with pytest.raises(ValidationError):
        DealPrimaryContactUpdateRequest(**payload)


def test_deal_close_request_requires_reason_for_lost_outcome() -> None:
    with pytest.raises(ValidationError):
        DealCloseRequest(outcome="lost")


def test_deal_close_request_normalizes_outcome() -> None:
    request = DealCloseRequest(outcome="  WON  ")

    assert request.outcome == "won"


def test_deal_close_request_clears_closed_reason_for_won_outcome() -> None:
    request = DealCloseRequest(outcome="won", closed_reason="  Contract signed  ")
    assert request.closed_reason is None


def test_deal_close_request_normalizes_closed_reason_for_lost_outcome() -> None:
    request = DealCloseRequest(outcome="lost", closed_reason="  Budget frozen  ")
    assert request.closed_reason == "Budget frozen"


def test_deal_reopen_request_defaults_clear_closed_reason_false() -> None:
    request = DealReopenRequest()
    assert request.clear_closed_reason is False


def test_owner_assign_request_requires_owner_user_id_key_even_when_clearing() -> None:
    with pytest.raises(ValidationError):
        DealOwnerAssignRequest()

    request = DealOwnerAssignRequest(owner_user_id=None)
    assert request.owner_user_id is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"name": "   "},
        {"domain": "   "},
        {"website": "   "},
        {"linkedin_url": "   "},
        {"industry": "   "},
        {"country": "   "},
    ],
)
def test_account_update_request_rejects_empty_or_blank_only_payload(payload) -> None:
    with pytest.raises(ValidationError):
        DealAccountUpdateRequest(**payload)


def test_account_update_request_allows_explicit_profile_field_clearing() -> None:
    request = DealAccountUpdateRequest(
        website=None,
        linkedin_url=None,
        industry=None,
        country=None,
    )

    assert request.website is None
    assert request.linkedin_url is None
    assert request.industry is None
    assert request.country is None


@pytest.mark.parametrize("field_name", ["name", "domain"])
def test_account_update_request_rejects_identity_field_clearing(field_name: str) -> None:
    with pytest.raises(ValidationError):
        DealAccountUpdateRequest(**{field_name: None, "website": "https://acme.com"})


# ---------------------------------------------------------------------------
# DealCreateRequest – source_created_at (Start Date)
# ---------------------------------------------------------------------------


def test_create_request_accepts_source_created_at() -> None:
    request = DealCreateRequest(
        name="Acme Deal",
        source_created_at=datetime(2026, 2, 24, 0, 0, 0, tzinfo=UTC),
    )
    assert request.source_created_at is not None
    assert request.source_created_at.year == 2026
    assert request.source_created_at.month == 2
    assert request.source_created_at.day == 24


def test_create_request_source_created_at_defaults_to_none() -> None:
    request = DealCreateRequest(name="Acme Deal")
    assert request.source_created_at is None


def test_create_request_accepts_source_created_at_iso_string() -> None:
    request = DealCreateRequest(
        name="Acme Deal",
        source_created_at="2026-06-15T00:00:00Z",  # type: ignore[arg-type]
    )
    assert request.source_created_at is not None
    assert request.source_created_at.day == 15


def test_create_request_accepts_account_company_id() -> None:
    company_id = UUID("11111111-1111-1111-1111-111111111111")

    request = DealCreateRequest(
        name="Acme Deal",
        account={"company_id": str(company_id), "name": "Acme Corp"},
    )

    assert request.account is not None
    assert request.account.company_id == company_id
    assert request.account.name == "Acme Corp"


def test_create_request_accepts_contact_participants() -> None:
    contact_id = UUID("22222222-2222-2222-2222-222222222222")

    request = DealCreateRequest(
        name="Acme Deal",
        contact_participants=[{"contact_id": str(contact_id)}],
    )

    assert request.contact_participants is not None
    assert request.contact_participants[0].contact_id == contact_id


def test_create_request_rejects_invalid_primary_contact_email() -> None:
    with pytest.raises(ValidationError):
        DealCreateRequest(
            name="Acme Deal",
            primary_contact_name="Jane Doe",
            primary_contact_email="not-an-email",
        )


def test_create_request_accepts_forecast_and_custom_properties() -> None:
    request = DealCreateRequest(
        name="Acme Deal",
        probability=65,
        forecast_category="  Commit ",
        custom_properties={"our_revenue": "25000000"},
    )

    assert request.probability == 65
    assert request.forecast_category == "Commit"
    assert request.custom_properties == {"our_revenue": "25000000"}


def test_create_request_accepts_custom_columns_as_public_custom_field_name() -> None:
    request = DealCreateRequest(
        name="Acme Deal",
        custom_columns={"our_revenue": "25000000"},
    )

    assert request.custom_columns == {"our_revenue": "25000000"}
    assert request.custom_properties == {"our_revenue": "25000000"}
    assert request.model_dump()["custom_properties"] == {"our_revenue": "25000000"}
    assert "custom_columns" not in request.model_dump()


@pytest.mark.parametrize(
    "request_cls,payload",
    [
        (DealCreateRequest, {"name": "Acme Deal", "custom_properties": None}),
        (DealUpdateRequest, {"custom_properties": None}),
        (DealCreateRequest, {"name": "Acme Deal", "custom_columns": None}),
        (DealUpdateRequest, {"custom_columns": None}),
    ],
)
def test_deal_write_requests_reject_null_custom_fields(request_cls, payload) -> None:
    with pytest.raises(ValidationError):
        request_cls(**payload)


@pytest.mark.parametrize(
    "request_cls,payload",
    [
        (DealCreateRequest, {"name": "Acme Deal", "custom_properties": {}}),
        (DealUpdateRequest, {"custom_properties": {}}),
        (DealCreateRequest, {"name": "Acme Deal", "custom_columns": {}}),
        (DealUpdateRequest, {"custom_columns": {}}),
    ],
)
def test_deal_write_requests_reject_empty_custom_fields(request_cls, payload) -> None:
    with pytest.raises(ValidationError):
        request_cls(**payload)


# ---------------------------------------------------------------------------
# DealUpdateRequest – source_created_at (Start Date)
# ---------------------------------------------------------------------------


def test_update_request_accepts_source_created_at() -> None:
    request = DealUpdateRequest(
        source_created_at=datetime(2026, 3, 10, 0, 0, 0, tzinfo=UTC),
    )
    assert request.source_created_at is not None
    assert request.source_created_at.month == 3


def test_update_request_source_created_at_defaults_to_none() -> None:
    request = DealUpdateRequest()
    assert request.source_created_at is None
