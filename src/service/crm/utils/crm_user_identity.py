"""Shared CRM user identity resolution for manual CRM writes."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.infra.database.sessions import acquire_master_async_session
from src.service.crm.internal.internal_crm_user_service import (
    InternalCRMUserService,
    InternalCRMUserServiceError,
)


class CRMUserIdentityResolutionError(Exception):
    """Raised when an app user cannot be resolved to a canonical CRM user."""

    def __init__(self, message: str, status_code: int = 500) -> None:
        self.message = message
        self.status_code = status_code
        super().__init__(message)


async def ensure_app_user_crm_identity(
    *,
    org_session: AsyncSession,
    organisation_id: UUID,
    app_user_id: UUID,
    actor_user_id: UUID | None = None,
    master_session: AsyncSession | None = None,
) -> CRMUser:
    """Return the canonical `crm_users` row for a Copilot app user.

    Manual CRM writers should call this instead of instantiating
    `InternalCRMUserService` directly. App-user creation should still create the
    invariant first; this utility is the write-path guardrail for old data,
    races, and repair gaps.
    """
    service_actor_id = actor_user_id or app_user_id
    try:
        if master_session is not None:
            return await InternalCRMUserService(
                org_session=org_session,
                master_session=master_session,
                organisation_id=organisation_id,
                actor_user_id=service_actor_id,
            ).ensure_crm_user(app_user_id)

        async for acquired_master_session in acquire_master_async_session():
            return await InternalCRMUserService(
                org_session=org_session,
                master_session=acquired_master_session,
                organisation_id=organisation_id,
                actor_user_id=service_actor_id,
            ).ensure_crm_user(app_user_id)
    except InternalCRMUserServiceError as exc:
        raise CRMUserIdentityResolutionError(exc.message, exc.status_code) from exc

    raise CRMUserIdentityResolutionError("Unable to resolve CRM user identity", 500)
