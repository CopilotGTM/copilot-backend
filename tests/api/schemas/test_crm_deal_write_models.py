from __future__ import annotations

import pytest
from pydantic import ValidationError
from src.api.schemas.crm_deal_write_models import (
    DealCloseRequest,
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
