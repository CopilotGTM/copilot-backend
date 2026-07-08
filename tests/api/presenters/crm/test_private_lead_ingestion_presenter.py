"""Tests for private lead-ingestion API presentation helpers."""

from __future__ import annotations

import json
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError
from src.api.presenters.crm.private_lead_ingestion_presenter import (
    private_form_failure_log_extra,
    private_ingestion_error_response,
    validation_error_message_and_field,
)


def test_private_ingestion_error_response_shapes_contract_and_headers() -> None:
    response = private_ingestion_error_response(
        code="rate_limited",
        message="Too many requests",
        status_code=429,
        field="body.email",
        headers={"Retry-After": "60"},
    )

    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert json.loads(response.body) == {
        "error": {
            "code": "rate_limited",
            "message": "Too many requests",
            "field": "body.email",
        }
    }


def test_validation_error_message_and_field_extracts_first_error_location() -> None:
    class StrictPayload(BaseModel):
        model_config = ConfigDict(extra="forbid")

        email: str

    try:
        StrictPayload.model_validate({"email": "buyer@example.com", "unexpected": "field"})
    except ValidationError as exc:
        message, field = validation_error_message_and_field(exc, "Invalid payload")

    assert message == "Extra inputs are not permitted"
    assert field == "unexpected"


def test_private_form_failure_log_extra_omits_empty_optional_fields() -> None:
    form_id = uuid4()

    extra = private_form_failure_log_extra(
        stage="validation",
        code="invalid_payload",
        status_code=422,
        key_id="skid_form",
        form_id=form_id,
        field=None,
    )

    assert extra == {
        "stage": "validation",
        "code": "invalid_payload",
        "status_code": 422,
        "key_id": "skid_form",
        "form_id": str(form_id),
    }
