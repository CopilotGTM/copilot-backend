"""Private server-to-server lead ingestion endpoint."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from fastapi import Response as FastAPIResponse
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from src.api.dependencies.crm_write_guard import is_crm_write_scope_enabled
from src.api.dependencies.rate_limit import client_ip_for_external_ingress
from src.api.presenters.crm.private_lead_ingestion_presenter import (
    log_private_form_failure,
    present_private_lead_form_submit_response,
    present_private_lead_ingestion_response,
    private_ingestion_error_response,
    validation_error_message_and_field,
)
from src.api.schemas.lead_form_models import (
    PrivateLeadFormSubmitRequest,
    PrivateLeadFormSubmitResponse,
)
from src.api.schemas.private_lead_ingestion_models import (
    PrivateLeadIngestionErrorResponse,
    PrivateLeadIngestionRequest,
    PrivateLeadIngestionResponse,
)
from src.domain.models.master_tables import PublicIngressResourceType
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.crm.leads.form_management_service import (
    LeadFormsServiceError,
    PublicLeadFormSubmissionService,
)
from src.service.crm.leads.ingestion_auth_service import (
    PrivateLeadIngestionAuthError,
    PrivateLeadIngestionAuthService,
)
from src.service.crm.leads.private_ingestion_service import (
    PrivateLeadIngestionService,
    PrivateLeadIngestionServiceError,
)

logger = get_logger("private_lead_ingestion_handler")

private_lead_ingestion_router = APIRouter()
_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"


@private_lead_ingestion_router.post(
    "/crm/leads",
    response_model=PrivateLeadIngestionResponse,
    status_code=201,
    responses={
        400: {"model": PrivateLeadIngestionErrorResponse},
        401: {"model": PrivateLeadIngestionErrorResponse},
        403: {"model": PrivateLeadIngestionErrorResponse},
        413: {"model": PrivateLeadIngestionErrorResponse},
        422: {"model": PrivateLeadIngestionErrorResponse},
        429: {"model": PrivateLeadIngestionErrorResponse},
        500: {"model": PrivateLeadIngestionErrorResponse},
    },
    summary="Create a CRM lead through private server-to-server ingestion",
)
async def ingest_private_lead(
    body: dict[str, Any],
    request: Request,
    response: FastAPIResponse,
    key_id: Annotated[str | None, Header(alias="X-CopilotGTM-Key-Id")] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> PrivateLeadIngestionResponse | JSONResponse:
    """Authenticate private ingest request and persist normalized lead payload for an org."""
    try:
        if not key_id or not key_id.startswith("skid_"):
            log_private_form_failure(
                logger,
                stage="auth",
                code="missing_key_id",
                status_code=401,
            )
            return private_ingestion_error_response(
                code="missing_key_id",
                message="Missing ingestion key id",
                status_code=401,
            )
        if not authorization or not authorization.lower().startswith("bearer "):
            log_private_form_failure(
                logger,
                stage="auth",
                code="missing_secret",
                status_code=401,
                key_id=key_id,
            )
            return private_ingestion_error_response(
                code="missing_secret",
                message="Missing ingestion bearer secret",
                status_code=401,
            )
        async for master_session in acquire_master_async_session():
            auth_service = PrivateLeadIngestionAuthService(master_session)
            ingress_key = await auth_service.authenticate(
                key_id=key_id,
                authorization=authorization,
                client_ip=client_ip_for_external_ingress(request),
            )
            break
        else:
            return private_ingestion_error_response(
                code="internal_error",
                message=_UNEXPECTED_ERR_MSG,
                status_code=500,
            )
        try:
            lead_payload = PrivateLeadIngestionRequest.model_validate(body)
        except ValidationError as exc:
            message, field = validation_error_message_and_field(exc, "Invalid lead payload")
            return private_ingestion_error_response(
                code="invalid_payload",
                message=message,
                status_code=422,
                field=field,
            )
        if not await is_crm_write_scope_enabled(
            ingress_key.organisation_id,
            "leads:create",
        ):
            return private_ingestion_error_response(
                code="internal_crm_writes_disabled",
                message="Internal CRM writes are disabled for this organisation",
                status_code=403,
            )
        async for org_session in acquire_org_async_session(ingress_key.organisation_id):
            service = PrivateLeadIngestionService(
                org_session=org_session,
                organisation_id=ingress_key.organisation_id,
            )
            lead, replayed = await service.ingest(
                payload=lead_payload,
                key_id=ingress_key.public_key or str(ingress_key.key_id),
                idempotency_key=idempotency_key,
            )
            if replayed:
                response.status_code = 200
            return present_private_lead_ingestion_response(lead)
        return private_ingestion_error_response(
            code="internal_error",
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
        )
    except PrivateLeadIngestionAuthError as exc:
        headers = {"Retry-After": "60"} if exc.code == "rate_limited" else None
        return private_ingestion_error_response(
            code=exc.code,
            message=exc.message,
            status_code=exc.status_code,
            field=exc.field,
            headers=headers,
        )
    except PrivateLeadIngestionServiceError as exc:
        return private_ingestion_error_response(
            code=exc.code,
            message=exc.message,
            status_code=exc.status_code,
            field=exc.field,
        )
    except Exception:
        logger.error("Error ingesting private lead", exc_info=True)
        return private_ingestion_error_response(
            code="internal_error",
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
        )


@private_lead_ingestion_router.post(
    "/forms/submissions",
    response_model=PrivateLeadFormSubmitResponse,
    status_code=201,
    responses={
        400: {"model": PrivateLeadIngestionErrorResponse},
        401: {"model": PrivateLeadIngestionErrorResponse},
        403: {"model": PrivateLeadIngestionErrorResponse},
        413: {"model": PrivateLeadIngestionErrorResponse},
        422: {"model": PrivateLeadIngestionErrorResponse},
        429: {"model": PrivateLeadIngestionErrorResponse},
        500: {"model": PrivateLeadIngestionErrorResponse},
    },
    summary="Submit a mapped private lead form payload",
)
async def ingest_private_form_submission(
    body: dict[str, Any],
    request: Request,
    response: FastAPIResponse,
    key_id: Annotated[str | None, Header(alias="X-CopilotGTM-Key-Id")] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> PrivateLeadFormSubmitResponse | JSONResponse:
    """Authenticate a form-scoped key and create a mapped lead through the form contract."""
    try:
        if not key_id or not key_id.startswith("skid_"):
            return private_ingestion_error_response(
                code="missing_key_id",
                message="Missing ingestion key id",
                status_code=401,
            )
        if not authorization or not authorization.lower().startswith("bearer "):
            return private_ingestion_error_response(
                code="missing_secret",
                message="Missing ingestion bearer secret",
                status_code=401,
            )
        async for master_session in acquire_master_async_session():
            auth_service = PrivateLeadIngestionAuthService(master_session)
            ingress_key = await auth_service.authenticate(
                key_id=key_id,
                authorization=authorization,
                client_ip=client_ip_for_external_ingress(request),
                resource_type=PublicIngressResourceType.LEAD_FORM,
            )
            break
        else:
            log_private_form_failure(
                logger,
                stage="auth",
                code="internal_error",
                status_code=500,
                key_id=key_id,
            )
            return private_ingestion_error_response(
                code="internal_error",
                message=_UNEXPECTED_ERR_MSG,
                status_code=500,
            )
        if not idempotency_key or not idempotency_key.strip():
            log_private_form_failure(
                logger,
                stage="validation",
                code="missing_idempotency_key",
                status_code=400,
                key_id=key_id,
                form_id=ingress_key.resource_id,
            )
            return private_ingestion_error_response(
                code="missing_idempotency_key",
                message="Idempotency-Key header is required",
                status_code=400,
            )
        try:
            mapped_payload = PrivateLeadFormSubmitRequest.model_validate(body)
        except ValidationError as exc:
            message, field = validation_error_message_and_field(
                exc, "Invalid form submission payload"
            )
            log_private_form_failure(
                logger,
                stage="validation",
                code="invalid_payload",
                status_code=422,
                key_id=key_id,
                form_id=ingress_key.resource_id,
                field=field,
            )
            return private_ingestion_error_response(
                code="invalid_payload",
                message=message,
                status_code=422,
                field=field,
            )
        if not await is_crm_write_scope_enabled(
            ingress_key.organisation_id,
            "lead_forms:ingest",
        ):
            log_private_form_failure(
                logger,
                stage="auth",
                code="internal_crm_writes_disabled",
                status_code=403,
                key_id=key_id,
                form_id=ingress_key.resource_id,
            )
            return private_ingestion_error_response(
                code="internal_crm_writes_disabled",
                message="Internal CRM writes are disabled for this organisation",
                status_code=403,
            )
        async for org_session in acquire_org_async_session(ingress_key.organisation_id):
            service = PublicLeadFormSubmissionService(
                org_session=org_session,
                organisation_id=ingress_key.organisation_id,
                form_id=ingress_key.resource_id,
                public_form_key=ingress_key.public_key or key_id,
            )
            lead, submission, replayed = await service.submit_private(
                fields_payload=mapped_payload.fields,
                context=mapped_payload.context,
                key_id=ingress_key.public_key or key_id,
                idempotency_key=idempotency_key,
                user_agent=request.headers.get("user-agent"),
                submitter_ip=client_ip_for_external_ingress(request),
            )
            if replayed:
                response.status_code = 200
            return present_private_lead_form_submit_response(lead=lead, submission=submission)
        return private_ingestion_error_response(
            code="internal_error",
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
        )
    except PrivateLeadIngestionAuthError as exc:
        log_private_form_failure(
            logger,
            stage="auth",
            code=exc.code,
            status_code=exc.status_code,
            key_id=key_id,
            field=exc.field,
        )
        headers = {"Retry-After": "60"} if exc.code == "rate_limited" else None
        return private_ingestion_error_response(
            code=exc.code,
            message=exc.message,
            status_code=exc.status_code,
            field=exc.field,
            headers=headers,
        )
    except LeadFormsServiceError as exc:
        code = exc.data.get("code") if exc.data else None
        error_field = exc.data.get("field") if exc.data else None
        log_private_form_failure(
            logger,
            stage="validation",
            code=code if isinstance(code, str) else "invalid_submission",
            status_code=exc.status_code,
            key_id=key_id,
            field=error_field if isinstance(error_field, str) else None,
        )
        return private_ingestion_error_response(
            code=code if isinstance(code, str) else "invalid_submission",
            message=exc.message,
            status_code=exc.status_code,
            field=error_field if isinstance(error_field, str) else None,
        )
    except Exception:
        logger.error(
            "Error ingesting private form submission",
            extra={"stage": "persistence", **({"key_id": key_id} if key_id else {})},
            exc_info=True,
        )
        return private_ingestion_error_response(
            code="internal_error",
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
        )
