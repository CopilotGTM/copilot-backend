"""Read-only email endpoints for CRM sidebar contexts."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_email_models import CRMEmailPage
from src.api.utils.http_response import Response
from src.infra.utils.logger import get_logger
from src.service.crm.access.record_access_policy import CRMAccessActor
from src.service.crm.deals.read_service import DealReadService, DealReadServiceError
from src.service.crm.emails.read_service import CRMEmailReadService, CRMEmailScope

logger = get_logger("crm_emails_handler")
emails_router = APIRouter()
_UNEXPECTED_ERR_MSG = "An unexpected error occurred while retrieving emails"


async def get_crm_email_read_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Yield a sidebar reader constrained to the caller's own connected mailboxes."""
    service = CRMEmailReadService(session, user_ids.organisation_id, user_ids.user_id)
    try:
        yield service
    finally:
        await service.close()


async def get_deal_email_access_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Yield the standard deal visibility reader before a deal email query."""
    service = DealReadService(
        session,
        user_ids.organisation_id,
        actor=CRMAccessActor(user_ids.user_id, user_ids.user_role, user_ids.user_email),
    )
    try:
        yield service
    finally:
        await service.close()


async def _list_scope(
    *,
    response: FastAPIResponse,
    service: CRMEmailReadService,
    scope: CRMEmailScope,
    object_id: UUID | None,
    integration_id: UUID | None,
    limit: int,
    cursor: str | None,
) -> GenericResponse[CRMEmailPage]:
    try:
        page = await service.list_emails(
            scope=scope,
            object_id=object_id,
            integration_id=integration_id,
            limit=limit,
            cursor=cursor,
        )
        return Response.success_response("Emails retrieved successfully", page)
    except ValueError as exc:
        return Response.bad_request_error_response(str(exc), response=response)
    except Exception:
        logger.exception(
            "CRM email read failed", extra={"scope": scope, "object_id": str(object_id)}
        )
        return Response.internal_server_error_response(_UNEXPECTED_ERR_MSG, response=response)


@emails_router.get(
    "/emails",
    response_model=GenericResponse[CRMEmailPage],
    summary="List emails from the current user's connected mailbox",
)
async def list_crm_emails(
    response: FastAPIResponse,
    integration_id: UUID | None = Query(None, description="Optional connected mailbox ID"),
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None),
    service: CRMEmailReadService = Depends(get_crm_email_read_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[CRMEmailPage]:
    """List the caller's synced mailbox messages without provider-side mutations."""
    return await _list_scope(
        response=response,
        service=service,
        scope=CRMEmailScope.OVERALL,
        object_id=None,
        integration_id=integration_id,
        limit=limit,
        cursor=cursor,
    )


@emails_router.get(
    "/contacts/{contact_id}/emails",
    response_model=GenericResponse[CRMEmailPage],
    summary="List emails related to a CRM contact",
)
async def get_contact_emails(
    contact_id: UUID,
    response: FastAPIResponse,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None),
    service: CRMEmailReadService = Depends(get_crm_email_read_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[CRMEmailPage]:
    """List caller-visible mailbox messages projected onto a contact."""
    return await _list_scope(
        response=response,
        service=service,
        scope=CRMEmailScope.CONTACT,
        object_id=contact_id,
        integration_id=None,
        limit=limit,
        cursor=cursor,
    )


@emails_router.get(
    "/accounts/{company_id}/emails",
    response_model=GenericResponse[CRMEmailPage],
    summary="List emails related to a CRM account",
)
async def get_account_emails(
    company_id: UUID,
    response: FastAPIResponse,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None),
    service: CRMEmailReadService = Depends(get_crm_email_read_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[CRMEmailPage]:
    """List caller-visible mailbox messages projected onto an account."""
    return await _list_scope(
        response=response,
        service=service,
        scope=CRMEmailScope.ACCOUNT,
        object_id=company_id,
        integration_id=None,
        limit=limit,
        cursor=cursor,
    )


@emails_router.get(
    "/deals/{deal_id}/emails",
    response_model=GenericResponse[CRMEmailPage],
    summary="List emails related to a CRM deal",
)
async def get_deal_emails(
    deal_id: UUID,
    response: FastAPIResponse,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None),
    service: CRMEmailReadService = Depends(get_crm_email_read_service),
    access_service: DealReadService = Depends(get_deal_email_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[CRMEmailPage]:
    """List caller-visible messages with an active association to a visible deal."""
    try:
        if await access_service.get_deal_by_id(deal_id) is None:
            return Response.not_found_error_response("Deal not found", response=response)
    except DealReadServiceError as exc:
        return Response.error_response(exc.status_code, exc.message, response=response)
    return await _list_scope(
        response=response,
        service=service,
        scope=CRMEmailScope.DEAL,
        object_id=deal_id,
        integration_id=None,
        limit=limit,
        cursor=cursor,
    )
