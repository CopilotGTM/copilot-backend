"""Lead form management and public submission endpoints.

Authenticated routes under `/lead-forms` create/update/list internal form
configuration in org context. Public routes under `/public/v1/forms/*` resolve a
form key from master schema and load definition/submission state from org schema.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi import Response as FastAPIResponse
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.crm_write_guard import (
    is_crm_write_scope_enabled,
    require_crm_write_scope,
)
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import (
    enforce_public_lead_form_read_rate_limit,
    enforce_public_lead_form_submit_rate_limit,
    rate_limit_user_read,
    rate_limit_user_write,
)
from src.api.presenters.crm.lead_form_presenter import (
    log_public_form_failure,
    present_lead_form_item,
    public_form_error_code,
    public_form_error_response,
    public_rate_limit_error_response,
    public_unexpected_error_response,
)
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.lead_form_models import (
    CreateLeadFormRequest,
    LeadFormItem,
    PublicLeadFormDefinitionResponse,
    PublicLeadFormErrorResponse,
    PublicLeadFormSubmitRequest,
    PublicLeadFormSubmitResponse,
    UpdateLeadFormRequest,
    UpdateLeadFormStatusRequest,
)
from src.api.utils.http_response import Response
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.crm.leads.form_management_service import (
    LeadFormsService,
    LeadFormsServiceError,
    PublicLeadFormSubmissionService,
    load_active_public_form_key,
)

logger = get_logger("lead_forms_handler")

lead_forms_router = APIRouter()
public_lead_forms_router = APIRouter()

_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"
_PUBLIC_FORM_KEY_PATH = Path(min_length=4, max_length=128, pattern=r"^lf_[A-Za-z0-9]+$")


async def get_lead_forms_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
    master_session: AsyncSession = Depends(get_master_async_session),
):
    """Build per-request lead form service with both master and org sessions."""
    service = LeadFormsService(
        org_session=org_session,
        master_session=master_session,
        organisation_id=user_ids.organisation_id,
        actor_user_id=user_ids.user_id,
    )
    try:
        yield service
    finally:
        await service.close()


@lead_forms_router.get(
    "/lead-forms",
    response_model=GenericResponse[list[LeadFormItem]],
    summary="List lead capture forms",
)
async def list_lead_forms(
    request: Request,
    response: FastAPIResponse,
    service: LeadFormsService = Depends(get_lead_forms_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[list[LeadFormItem]]:
    """List non-deleted lead forms for the current organisation.

    Enriched with active public ingress keys so UI consumers can render direct
    public links in one call.
    """
    try:
        rows = await service.list_forms()
        return Response.success_response(
            message="Lead forms retrieved",
            data=[
                present_lead_form_item(form=form, fields=fields, ingress_key=key, request=request)
                for form, fields, key in rows
            ],
        )
    except LeadFormsServiceError as exc:
        return Response.error_response(
            message=exc.message,
            status_code=exc.status_code,
            response=response,
        )
    except Exception:
        logger.error("Error listing lead forms", exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
            response=response,
        )


@lead_forms_router.post(
    "/lead-forms",
    response_model=GenericResponse[LeadFormItem],
    status_code=201,
    summary="Create a lead capture form",
)
async def create_lead_form(
    body: CreateLeadFormRequest,
    request: Request,
    response: FastAPIResponse,
    service: LeadFormsService = Depends(get_lead_forms_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("lead_forms:create")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[LeadFormItem]:
    """Create a lead form and its public form ingress key for the current org."""
    try:
        form, fields, ingress_key = await service.create_form(body)
        return Response.created_response(
            message="Lead form created",
            data=present_lead_form_item(
                form=form, fields=fields, ingress_key=ingress_key, request=request
            ),
            response=response,
        )
    except LeadFormsServiceError as exc:
        await service.org_session.rollback()
        await service.master_session.rollback()
        return Response.error_response(
            message=exc.message,
            status_code=exc.status_code,
            response=response,
        )
    except Exception:
        await service.org_session.rollback()
        await service.master_session.rollback()
        logger.error("Error creating lead form", exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
            response=response,
        )


@lead_forms_router.get(
    "/lead-forms/{form_id}",
    response_model=GenericResponse[LeadFormItem],
    summary="Get a lead capture form",
)
async def get_lead_form(
    form_id: UUID,
    request: Request,
    response: FastAPIResponse,
    service: LeadFormsService = Depends(get_lead_forms_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[LeadFormItem]:
    """Return one lead form and its mapped fields for management screens.

    The org lookup is always filtered by organisation and non-deleted flag so
    keys from another tenant cannot be read by a valid API caller.
    """
    try:
        form, fields, ingress_key = await service.get_form(form_id)
        return Response.success_response(
            message="Lead form retrieved",
            data=present_lead_form_item(
                form=form, fields=fields, ingress_key=ingress_key, request=request
            ),
        )
    except LeadFormsServiceError as exc:
        return Response.error_response(
            message=exc.message,
            status_code=exc.status_code,
            response=response,
        )
    except Exception:
        logger.error("Error getting lead form", extra={"form_id": str(form_id)}, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
            response=response,
        )


@lead_forms_router.patch(
    "/lead-forms/{form_id}",
    response_model=GenericResponse[LeadFormItem],
    summary="Update a lead capture form",
)
async def update_lead_form(
    form_id: UUID,
    body: UpdateLeadFormRequest,
    request: Request,
    response: FastAPIResponse,
    service: LeadFormsService = Depends(get_lead_forms_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("lead_forms:update")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[LeadFormItem]:
    """Update metadata and mapped fields for a lead form.

    A version bump is emitted with the update so public submissions and payload
    mapping always read consistent field snapshots.
    """
    try:
        form, fields, ingress_key = await service.update_form(form_id, body)
        return Response.success_response(
            message="Lead form updated",
            data=present_lead_form_item(
                form=form, fields=fields, ingress_key=ingress_key, request=request
            ),
        )
    except LeadFormsServiceError as exc:
        # Keep form + ingress-key updates in sync across org and master sessions.
        await service.org_session.rollback()
        await service.master_session.rollback()
        return Response.error_response(
            message=exc.message,
            status_code=exc.status_code,
            response=response,
        )
    except Exception:
        await service.org_session.rollback()
        await service.master_session.rollback()
        logger.error(
            "Error updating lead form",
            extra={"form_id": str(form_id)},
            exc_info=True,
        )
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
            response=response,
        )


@lead_forms_router.patch(
    "/lead-forms/{form_id}/status",
    response_model=GenericResponse[LeadFormItem],
    summary="Update lead capture form status",
)
async def update_lead_form_status(
    form_id: UUID,
    body: UpdateLeadFormStatusRequest,
    request: Request,
    response: FastAPIResponse,
    service: LeadFormsService = Depends(get_lead_forms_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("lead_forms:update")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[LeadFormItem]:
    """Transition form lifecycle status and sync ingress key availability.

    Status writes are mirrored into public ingress keys so a deactivated form is
    not accidentally reachable through stale browser links.
    """
    try:
        form, fields, ingress_key = await service.update_form_status(form_id, body.status)
        return Response.success_response(
            message="Lead form status updated",
            data=present_lead_form_item(
                form=form, fields=fields, ingress_key=ingress_key, request=request
            ),
        )
    except LeadFormsServiceError as exc:
        # Status changes can deactivate ingress keys, so both schemas must be
        # rolled back together on failure.
        await service.org_session.rollback()
        await service.master_session.rollback()
        return Response.error_response(
            message=exc.message,
            status_code=exc.status_code,
            response=response,
        )
    except Exception:
        await service.org_session.rollback()
        await service.master_session.rollback()
        logger.error(
            "Error updating lead form status",
            extra={"form_id": str(form_id)},
            exc_info=True,
        )
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
            response=response,
        )


@public_lead_forms_router.get(
    "/forms/{public_form_key}",
    response_model=PublicLeadFormDefinitionResponse,
    responses={
        403: {"model": PublicLeadFormErrorResponse},
        404: {"model": PublicLeadFormErrorResponse},
        429: {"model": PublicLeadFormErrorResponse},
        500: {"model": PublicLeadFormErrorResponse},
    },
    summary="Get a public lead capture form definition",
)
async def get_public_lead_form(
    public_form_key: Annotated[str, _PUBLIC_FORM_KEY_PATH],
    request: Request,
    master_session: AsyncSession = Depends(get_master_async_session),
) -> PublicLeadFormDefinitionResponse | JSONResponse:
    """Resolve a published lead form definition using its public form key.

    Only active keys can resolve and this endpoint returns browser-safe fields,
    excluding internal target mapping details.
    """
    try:
        enforce_public_lead_form_read_rate_limit(request, public_form_key)
        ingress_key = await load_active_public_form_key(public_form_key, master_session)
        if not ingress_key:
            return public_form_error_response(
                LeadFormsServiceError("Lead form not found", status_code=404)
            )

        # Acquire one org-scoped session for this public tenant lookup and return
        # as soon as service logic finishes; this avoids holding tenant sessions
        # across retries or long response serialization.
        async for org_session in acquire_org_async_session(ingress_key.organisation_id):
            service = PublicLeadFormSubmissionService(
                org_session=org_session,
                organisation_id=ingress_key.organisation_id,
                form_id=ingress_key.resource_id,
                public_form_key=public_form_key,
                allowed_origins=ingress_key.allowed_origins,
            )
            definition = await service.load_definition(origin=request.headers.get("origin"))
            return definition

        return public_unexpected_error_response()
    except HTTPException as exc:
        if exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
            return public_rate_limit_error_response(exc)
        raise
    except LeadFormsServiceError as exc:
        return public_form_error_response(exc)
    except Exception:
        logger.error("Error getting public lead form", exc_info=True)
        return public_unexpected_error_response()


@public_lead_forms_router.post(
    "/forms/{public_form_key}/submissions",
    response_model=PublicLeadFormSubmitResponse,
    status_code=201,
    responses={
        403: {"model": PublicLeadFormErrorResponse},
        404: {"model": PublicLeadFormErrorResponse},
        413: {"model": PublicLeadFormErrorResponse},
        422: {"model": PublicLeadFormErrorResponse},
        429: {"model": PublicLeadFormErrorResponse},
        500: {"model": PublicLeadFormErrorResponse},
    },
    summary="Submit a public lead capture form",
)
async def submit_public_lead_form(
    public_form_key: Annotated[str, _PUBLIC_FORM_KEY_PATH],
    body: PublicLeadFormSubmitRequest,
    request: Request,
    master_session: AsyncSession = Depends(get_master_async_session),
) -> PublicLeadFormSubmitResponse | JSONResponse:
    """Accept an anonymous public form submission.

    Writes one form submission audit row and one lead row in the same org
    transaction so callers never observe a submission without a durable lead.
    """
    try:
        enforce_public_lead_form_submit_rate_limit(request, public_form_key)
        ingress_key = await load_active_public_form_key(public_form_key, master_session)
        if not ingress_key:
            log_public_form_failure(
                logger,
                public_form_key=public_form_key,
                stage="auth",
                code="lead_form_not_found",
                status_code=404,
            )
            return public_form_error_response(
                LeadFormsServiceError("Lead form not found", status_code=404)
            )
        if not await is_crm_write_scope_enabled(
            ingress_key.organisation_id,
            "lead_forms:ingest",
        ):
            log_public_form_failure(
                logger,
                public_form_key=public_form_key,
                stage="auth",
                code="internal_crm_writes_disabled",
                status_code=403,
            )
            return public_form_error_response(
                LeadFormsServiceError(
                    "Internal CRM writes are disabled for this organisation",
                    status_code=403,
                    data={"code": "internal_crm_writes_disabled"},
                )
            )

        # Public submission writes both a submission audit row and a lead row in a
        # single transaction so we cannot return a submission_id unless both are
        # durable.
        async for org_session in acquire_org_async_session(ingress_key.organisation_id):
            service = PublicLeadFormSubmissionService(
                org_session=org_session,
                organisation_id=ingress_key.organisation_id,
                form_id=ingress_key.resource_id,
                public_form_key=public_form_key,
                allowed_origins=ingress_key.allowed_origins,
            )
            _lead, submission = await service.submit(
                fields_payload=body.fields,
                context=body.context,
                origin=request.headers.get("origin"),
                user_agent=request.headers.get("user-agent"),
                submitter_ip=request.client.host if request.client else None,
                idempotency_key=body.idempotency_key,
            )
            return PublicLeadFormSubmitResponse(submission_id=submission.submission_id)

        return public_unexpected_error_response()
    except HTTPException as exc:
        if exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
            return public_rate_limit_error_response(exc)
        raise
    except LeadFormsServiceError as exc:
        code = public_form_error_code(exc)
        field = exc.data.get("field") if exc.data else None
        log_public_form_failure(
            logger,
            public_form_key=public_form_key,
            stage="validation",
            code=code,
            status_code=exc.status_code,
            field=field if isinstance(field, str) else None,
        )
        return public_form_error_response(exc)
    except Exception:
        logger.error(
            "Error submitting public lead form",
            extra={"public_form_key": public_form_key, "stage": "persistence"},
            exc_info=True,
        )
        return public_unexpected_error_response()
