"""Presentation helpers for CRM lead-form API responses and logs."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from src.api.schemas.lead_form_models import (
    LeadFormFieldItem,
    LeadFormItem,
    PublicLeadFormError,
    PublicLeadFormErrorResponse,
)
from src.domain.models.master_tables import PublicIngressKey
from src.service.crm.leads.form_management_service import LeadFormsServiceError

PUBLIC_EXTERNAL_API_VERSION = "v1"
UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"


def present_lead_form_field_item(field: Any) -> LeadFormFieldItem:
    return LeadFormFieldItem.model_validate(field)


def public_form_url(request: Request, public_form_key: str) -> str:
    return (
        f"{str(request.base_url).rstrip('/')}/public/"
        f"{PUBLIC_EXTERNAL_API_VERSION}/forms/{public_form_key}"
    )


def present_lead_form_item(
    *,
    form: Any,
    fields: list[Any],
    ingress_key: PublicIngressKey | None,
    request: Request,
) -> LeadFormItem:
    item = LeadFormItem.model_validate(form)
    public_form_key = ingress_key.public_key if ingress_key and ingress_key.public_key else ""
    item.public_form_key = public_form_key
    item.public_form_url = public_form_url(request, public_form_key) if public_form_key else ""
    item.allowed_origins = ingress_key.allowed_origins if ingress_key else []
    item.fields = [present_lead_form_field_item(field) for field in fields]
    return item


def public_form_error_code(exc: LeadFormsServiceError) -> str:
    code = exc.data.get("code") if exc.data else None
    if isinstance(code, str):
        return code
    if exc.status_code == 403:
        return "origin_not_allowed"
    if exc.status_code == 404:
        return "lead_form_not_found"
    if exc.status_code == 413:
        return "payload_too_large"
    if exc.status_code == 422:
        return "invalid_submission"
    return "lead_form_error"


def public_form_failure_log_extra(
    *,
    public_form_key: str,
    stage: str,
    code: str,
    status_code: int,
    field: str | None = None,
) -> dict[str, Any]:
    return {
        "public_form_key": public_form_key,
        "stage": stage,
        "code": code,
        "status_code": status_code,
        **({"field": field} if field else {}),
    }


def log_public_form_failure(
    logger: Any,
    *,
    public_form_key: str,
    stage: str,
    code: str,
    status_code: int,
    field: str | None = None,
) -> None:
    logger.warning(
        "Public lead form submission failed",
        extra=public_form_failure_log_extra(
            public_form_key=public_form_key,
            stage=stage,
            code=code,
            status_code=status_code,
            field=field,
        ),
    )


def public_form_error_response(exc: LeadFormsServiceError) -> JSONResponse:
    field = exc.data.get("field") if exc.data else None
    error = PublicLeadFormErrorResponse(
        error=PublicLeadFormError(
            code=public_form_error_code(exc),
            message=exc.message,
            field=field if isinstance(field, str) else None,
        )
    )
    return JSONResponse(status_code=exc.status_code, content=error.model_dump(exclude_none=True))


def public_rate_limit_error_response(exc: HTTPException) -> JSONResponse:
    message = exc.detail if isinstance(exc.detail, str) else "Too many requests"
    error = PublicLeadFormErrorResponse(
        error=PublicLeadFormError(
            code="rate_limited",
            message=message,
        )
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=error.model_dump(exclude_none=True),
        headers=exc.headers,
    )


def public_unexpected_error_response() -> JSONResponse:
    error = PublicLeadFormErrorResponse(
        error=PublicLeadFormError(
            code="internal_error",
            message=UNEXPECTED_ERR_MSG,
        )
    )
    return JSONResponse(status_code=500, content=error.model_dump(exclude_none=True))
