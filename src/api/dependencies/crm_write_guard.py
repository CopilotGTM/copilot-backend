"""Guards for organisation-level internal CRM write permissions."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, HTTPException, status
from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
from src.infra.database.sessions import master_async_session_scope
from src.service.orgnisation.organisation_policy_service import (
    OrganisationPolicyDeniedError,
    OrganisationPolicyService,
)


async def are_internal_crm_writes_enabled(organisation_id) -> bool:
    """Return whether this organisation may write into CopilotGTM-owned CRM data."""
    async with master_async_session_scope() as session:
        preferences = await OrganisationPolicyService(session).get_policy_snapshot(organisation_id)
    return bool(preferences.get("crm", {}).get("write_policy", {}).get("allowed_scopes", []))


async def is_crm_write_scope_enabled(organisation_id, scope: str) -> bool:
    """Return whether this organisation may perform one internal CRM write scope."""
    async with master_async_session_scope() as session:
        return await OrganisationPolicyService(session).has_crm_write_scope(organisation_id, scope)


def require_crm_write_scope(scope: str) -> Callable[..., None]:
    async def dependency(user_ids: UserIDs = Depends(get_current_user)) -> None:
        async with master_async_session_scope() as session:
            service = OrganisationPolicyService(session)
            try:
                await service.require_crm_write_scope(user_ids.organisation_id, scope)
            except OrganisationPolicyDeniedError as exc:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=exc.message,
                ) from exc

    return dependency


async def require_internal_crm_writes_enabled(
    user_ids: UserIDs = Depends(get_current_user),
) -> None:
    """Reject internal CRM write APIs when the organisation uses an external CRM."""
    if not await are_internal_crm_writes_enabled(user_ids.organisation_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Internal CRM writes are disabled for this organisation",
        )
