"""FastAPI dependencies for record-level CRM authorization.

Deal child routes use these guards before loading notes, assets, meetings, or
other dependent data. This keeps the parent deal policy as the single source of
truth and returns 404 when a member must not learn that a record exists.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.service.crm.access.record_access_policy import (
    CRMAccessActor,
    CRMRecordAccessError,
    DealAccessLevel,
    DealAccessPolicy,
)


def access_actor_from_user(user: UserIDs) -> CRMAccessActor:
    """Build the immutable CRM principal from the authenticated master user."""
    return CRMAccessActor(user.user_id, user.user_role, user.user_email)


def require_deal_access(level: DealAccessLevel) -> Callable[..., None]:
    """Return a dependency that enforces parent-deal access for a path deal ID."""

    async def dependency(
        deal_id: UUID,
        user: UserIDs = Depends(get_current_user),
        session: AsyncSession = Depends(get_org_async_session),
    ) -> None:
        """Authorize one path-bound deal without returning its private record."""
        try:
            await DealAccessPolicy(
                session,
                user.organisation_id,
                access_actor_from_user(user),
            ).require_deal(deal_id, level)
        except CRMRecordAccessError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return dependency


async def require_crm_admin(user: UserIDs = Depends(get_current_user)) -> None:
    """Allow organisation-wide CRM administration only to an authenticated admin."""
    if not access_actor_from_user(user).is_admin:
        raise HTTPException(status_code=403, detail="Administrator access required")


require_deal_view = require_deal_access("view")
require_deal_collaborate = require_deal_access("collaborate")
require_deal_control = require_deal_access("control")
