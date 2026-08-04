"""Organisation-admin controls for same-domain membership admission."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import require_organisation_admin
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.membership_models import (
    MembershipPolicyResponse,
    UpdateMembershipPolicyRequest,
)
from src.api.utils.http_response import Response
from src.domain.models.master_tables import User
from src.infra.auth.user_auth import user_auth
from src.service.orgnisation.membership_policy_service import MembershipPolicyService

membership_policy_router = APIRouter()


@membership_policy_router.get(
    "/membership",
    response_model=GenericResponse[MembershipPolicyResponse],
)
async def get_membership_policy(
    current_user: User = Depends(require_organisation_admin),
    master_session: AsyncSession = Depends(get_master_async_session),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[MembershipPolicyResponse]:
    payload = await MembershipPolicyService(
        master_session, current_user.organisation_id
    ).get_policy()
    return Response.success_response(message="Membership policy retrieved", data=payload)


@membership_policy_router.patch(
    "/membership",
    response_model=GenericResponse[MembershipPolicyResponse],
)
async def update_membership_policy(
    request: UpdateMembershipPolicyRequest,
    current_user: User = Depends(require_organisation_admin),
    master_session: AsyncSession = Depends(get_master_async_session),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[MembershipPolicyResponse]:
    payload = await MembershipPolicyService(
        master_session, current_user.organisation_id
    ).update_policy(request)
    return Response.success_response(message="Membership policy updated", data=payload)


__all__ = ["membership_policy_router", "user_auth"]
