from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from src.api.schemas.crm_deal_write_models import (
    DealCloseRequest,
    DealCreateRequest,
    DealPrimaryContactUpdateRequest,
    DealReopenRequest,
    DealUpdateRequest,
)


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


def test_primary_contact_request_normalizes_fields() -> None:
    request = DealPrimaryContactUpdateRequest(
        primary_contact_name="  Jane Doe  ",
        primary_contact_email="  jane@example.com ",
    )

    assert request.primary_contact_name == "Jane Doe"
    assert request.primary_contact_email == "jane@example.com"


def test_deal_close_request_requires_reason_for_lost_outcome() -> None:
    with pytest.raises(ValidationError):
        DealCloseRequest(outcome="lost")


def test_deal_close_request_normalizes_closed_reason() -> None:
    request = DealCloseRequest(outcome="won", closed_reason="  Contract signed  ")
    assert request.closed_reason == "Contract signed"


def test_deal_reopen_request_defaults_clear_closed_reason_false() -> None:
    request = DealReopenRequest()
    assert request.clear_closed_reason is False


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
