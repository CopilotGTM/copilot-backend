"""Authenticated private lead ingestion key management endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi import Response as FastAPIResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.private_lead_ingestion_models import (
    CreateLeadIngestionKeyRequest,
    LeadIngestionKeyCreateResponse,
    LeadIngestionKeyItem,
    UpdateLeadIngestionKeyStatusRequest,
)
from src.api.utils.http_response import Response
from src.domain.models.master_tables import PublicIngressResourceType
from src.domain.models.organisation_tables.sync.crm.lead_forms import LeadForm
from src.infra.utils.logger import get_logger
from src.service.crm.access.lead_ingestion_key_service import (
    LeadIngestionKeyService,
    LeadIngestionKeyServiceError,
)

logger = get_logger("lead_ingestion_keys_handler")

lead_ingestion_keys_router = APIRouter()
_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"


async def _ensure_lead_form_exists(
    form_id: UUID,
    organisation_id: UUID,
    org_session: AsyncSession,
) -> LeadForm:
    """Verify a form-scoped key target belongs to the authenticated organisation."""
    stmt = select(LeadForm).where(
        col(LeadForm.organisation_id) == organisation_id,
        col(LeadForm.form_id) == form_id,
        col(LeadForm.is_deleted).is_(False),
    )
    form = (await org_session.execute(stmt)).scalars().first()
    if not form:
        raise LeadIngestionKeyServiceError("Lead form not found", 404)
    return form


async def get_lead_ingestion_key_service(
    user_ids: UserIDs = Depends(get_current_user),
    master_session: AsyncSession = Depends(get_master_async_session),
):
    """Yield a request-scoped ingestion-key service tied to caller identity."""
    service = LeadIngestionKeyService(
        master_session=master_session,
        organisation_id=user_ids.organisation_id,
        actor_user_id=user_ids.user_id,
    )
    try:
        yield service
    finally:
        await service.close()


@lead_ingestion_keys_router.post(
    "/lead-ingestion-keys",
    response_model=GenericResponse[LeadIngestionKeyCreateResponse],
    status_code=201,
    summary="Create a private lead ingestion key",
)
async def create_lead_ingestion_key(
    body: CreateLeadIngestionKeyRequest,
    response: FastAPIResponse,
    service: LeadIngestionKeyService = Depends(get_lead_ingestion_key_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[LeadIngestionKeyCreateResponse]:
    """Create a private lead-ingestion key and expose the secret once to UI/API callers."""
    try:
        key, secret = await service.create_key(body)
        return Response.created_response(
            message="Lead ingestion key created",
            data=LeadIngestionKeyCreateResponse(
                key_id=key.public_key or str(key.key_id),
                secret=secret,
                status=key.status,
                created_at=key.created_at,
            ),
            response=response,
        )
    except LeadIngestionKeyServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        logger.error("Error creating lead ingestion key", exc_info=True)
        return Response.error_response(
            status_code=500,
            message=_UNEXPECTED_ERR_MSG,
            response=response,
        )


@lead_ingestion_keys_router.get(
    "/lead-ingestion-keys",
    response_model=GenericResponse[list[LeadIngestionKeyItem]],
    summary="List private lead ingestion keys",
)
async def list_lead_ingestion_keys(
    response: FastAPIResponse,
    service: LeadIngestionKeyService = Depends(get_lead_ingestion_key_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[list[LeadIngestionKeyItem]]:
    """List private lead-ingestion keys for the authenticated caller's org."""
    try:
        return Response.success_response(
            message="Lead ingestion keys retrieved",
            data=await service.list_keys(),
        )
    except LeadIngestionKeyServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        logger.error("Error listing lead ingestion keys", exc_info=True)
        return Response.error_response(
            status_code=500,
            message=_UNEXPECTED_ERR_MSG,
            response=response,
        )


@lead_ingestion_keys_router.patch(
    "/lead-ingestion-keys/{key_id}/status",
    response_model=GenericResponse[LeadIngestionKeyItem],
    summary="Update private lead ingestion key status",
)
async def update_lead_ingestion_key_status(
    key_id: str,
    body: UpdateLeadIngestionKeyStatusRequest,
    response: FastAPIResponse,
    service: LeadIngestionKeyService = Depends(get_lead_ingestion_key_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[LeadIngestionKeyItem]:
    """Update a key's lifecycle state and return the refreshed ingestion key record."""
    try:
        key = await service.update_status(key_id, body.status)
        return Response.success_response(
            message="Lead ingestion key status updated",
            data=LeadIngestionKeyService.to_item(key),
        )
    except LeadIngestionKeyServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        logger.error("Error updating lead ingestion key status", exc_info=True)
        return Response.error_response(
            status_code=500,
            message=_UNEXPECTED_ERR_MSG,
            response=response,
        )


@lead_ingestion_keys_router.post(
    "/lead-forms/{form_id}/private-keys",
    response_model=GenericResponse[LeadIngestionKeyCreateResponse],
    status_code=201,
    summary="Create a form-scoped private lead form key",
)
async def create_lead_form_private_key(
    form_id: UUID,
    body: CreateLeadIngestionKeyRequest,
    response: FastAPIResponse,
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
    service: LeadIngestionKeyService = Depends(get_lead_ingestion_key_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[LeadIngestionKeyCreateResponse]:
    """Create a private API key that can submit only through this lead form mapping."""
    try:
        form = await _ensure_lead_form_exists(form_id, user_ids.organisation_id, org_session)
        key, secret = await service.create_key(
            body,
            resource_type=PublicIngressResourceType.LEAD_FORM,
            resource_id=form_id,
            metadata={"form_id": str(form_id), "form_name": form.name},
        )
        return Response.created_response(
            message="Lead form private key created",
            data=LeadIngestionKeyCreateResponse(
                key_id=key.public_key or str(key.key_id),
                secret=secret,
                status=key.status,
                created_at=key.created_at,
            ),
            response=response,
        )
    except LeadIngestionKeyServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        logger.error("Error creating lead form private key", exc_info=True)
        return Response.error_response(
            status_code=500,
            message=_UNEXPECTED_ERR_MSG,
            response=response,
        )


@lead_ingestion_keys_router.get(
    "/lead-forms/{form_id}/private-keys",
    response_model=GenericResponse[list[LeadIngestionKeyItem]],
    summary="List form-scoped private lead form keys",
)
async def list_lead_form_private_keys(
    form_id: UUID,
    response: FastAPIResponse,
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
    service: LeadIngestionKeyService = Depends(get_lead_ingestion_key_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[list[LeadIngestionKeyItem]]:
    """List private keys for one lead form without returning secrets or hashes."""
    try:
        await _ensure_lead_form_exists(form_id, user_ids.organisation_id, org_session)
        return Response.success_response(
            message="Lead form private keys retrieved",
            data=await service.list_keys(
                resource_type=PublicIngressResourceType.LEAD_FORM,
                resource_id=form_id,
            ),
        )
    except LeadIngestionKeyServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        logger.error("Error listing lead form private keys", exc_info=True)
        return Response.error_response(
            status_code=500,
            message=_UNEXPECTED_ERR_MSG,
            response=response,
        )


@lead_ingestion_keys_router.patch(
    "/lead-forms/{form_id}/private-keys/{key_id}/status",
    response_model=GenericResponse[LeadIngestionKeyItem],
    summary="Update form-scoped private lead form key status",
)
async def update_lead_form_private_key_status(
    form_id: UUID,
    key_id: str,
    body: UpdateLeadIngestionKeyStatusRequest,
    response: FastAPIResponse,
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
    service: LeadIngestionKeyService = Depends(get_lead_ingestion_key_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[LeadIngestionKeyItem]:
    """Pause, activate, or revoke a private key scoped to one lead form."""
    try:
        await _ensure_lead_form_exists(form_id, user_ids.organisation_id, org_session)
        key = await service.update_status(
            key_id,
            body.status,
            resource_type=PublicIngressResourceType.LEAD_FORM,
            resource_id=form_id,
        )
        return Response.success_response(
            message="Lead form private key status updated",
            data=LeadIngestionKeyService.to_item(key),
        )
    except LeadIngestionKeyServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        logger.error("Error updating lead form private key status", exc_info=True)
        return Response.error_response(
            status_code=500,
            message=_UNEXPECTED_ERR_MSG,
            response=response,
        )
