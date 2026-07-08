"""Presentation helpers for private lead-ingestion API responses and logs."""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse
from pydantic import ValidationError
from src.api.schemas.lead_form_models import PrivateLeadFormSubmitResponse
from src.api.schemas.private_lead_ingestion_models import (
    PrivateLeadIngestionError,
    PrivateLeadIngestionErrorResponse,
    PrivateLeadIngestionResponse,
)


def private_ingestion_error_response(
    *,
    code: str,
    message: str,
    status_code: int,
    field: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    error = PrivateLeadIngestionErrorResponse(
        error=PrivateLeadIngestionError(code=code, message=message, field=field)
    )
    return JSONResponse(
        status_code=status_code,
        content=error.model_dump(exclude_none=True),
        headers=headers,
    )


def validation_error_message_and_field(
    exc: ValidationError, fallback_message: str
) -> tuple[str, str | None]:
    errors = exc.errors()
    first_error = dict(errors[0]) if errors else {}
    loc_value = first_error.get("loc")
    loc = loc_value if isinstance(loc_value, list | tuple) else []
    field = ".".join(str(part) for part in loc if part)
    return str(first_error.get("msg") or fallback_message), field or None


def private_form_failure_log_extra(
    *,
    stage: str,
    code: str,
    status_code: int,
    key_id: str | None = None,
    form_id: Any | None = None,
    field: str | None = None,
) -> dict[str, Any]:
    return {
        "stage": stage,
        "code": code,
        "status_code": status_code,
        **({"key_id": key_id} if key_id else {}),
        **({"form_id": str(form_id)} if form_id else {}),
        **({"field": field} if field else {}),
    }


def log_private_form_failure(
    logger: Any,
    *,
    stage: str,
    code: str,
    status_code: int,
    key_id: str | None = None,
    form_id: Any | None = None,
    field: str | None = None,
) -> None:
    logger.warning(
        "Private lead form submission failed",
        extra=private_form_failure_log_extra(
            stage=stage,
            code=code,
            status_code=status_code,
            key_id=key_id,
            form_id=form_id,
            field=field,
        ),
    )


def present_private_lead_ingestion_response(lead: Any) -> PrivateLeadIngestionResponse:
    return PrivateLeadIngestionResponse(lead_id=lead.lead_id)


def present_private_lead_form_submit_response(
    *,
    lead: Any,
    submission: Any,
) -> PrivateLeadFormSubmitResponse:
    return PrivateLeadFormSubmitResponse(
        lead_id=lead.lead_id,
        submission_id=submission.submission_id,
    )
