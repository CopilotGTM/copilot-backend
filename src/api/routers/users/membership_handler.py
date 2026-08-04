"""Organisation-admin endpoint for external-provider member invitations."""

from fastapi import APIRouter, Depends
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import require_organisation_admin
from src.api.dependencies.rate_limit import rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.membership_models import (
    OrganisationMemberInvitationRequest,
    OrganisationMemberInvitationResponse,
)
from src.api.utils.http_response import Response
from src.domain.models.master_tables import Organisation, User, UserAuthenticationMethod
from src.infra.auth.email_service import email_service
from src.infra.auth.user_auth import user_auth
from src.infra.utils.logger import get_logger
from src.service.users.organisation_membership_service import (
    OrganisationMembershipError,
    OrganisationMembershipService,
)

logger = get_logger("organisation_membership_handler")
organisation_membership_router = APIRouter()


@organisation_membership_router.post(
    "/invitations",
    response_model=GenericResponse[OrganisationMemberInvitationResponse],
)
async def invite_organisation_member(
    request: OrganisationMemberInvitationRequest,
    response: FastAPIResponse,
    current_user: User = Depends(require_organisation_admin),
    master_session: AsyncSession = Depends(get_master_async_session),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[OrganisationMemberInvitationResponse]:
    organisation = await master_session.get(Organisation, current_user.organisation_id)
    if organisation is None:
        return Response.error_response(
            status_code=404,
            message="Organisation not found",
            code="ORGANISATION_NOT_FOUND",
            response=response,
        )
    try:
        user = await OrganisationMembershipService(master_session).invite_member(
            organisation=organisation,
            inviter=current_user,
            request=request,
        )
    except OrganisationMembershipError as exc:
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            code=exc.code,
            response=response,
        )

    provider_name = (
        "Google"
        if user.authentication_method == UserAuthenticationMethod.GOOGLE_WORKSPACE
        else "Microsoft"
    )
    delivered = await email_service.send_external_auth_invitation_email(
        to_email=str(user.email),
        first_name=user.first_name,
        provider_name=provider_name,
    )
    if not delivered:
        logger.warning(
            "Membership invitation delivery failed",
            extra={
                "event": "membership_invitation_delivery_failed",
                "organisation_id": str(user.organisation_id),
                "user_id": str(user.user_id),
            },
        )
    return Response.success_response(
        message="Member invited successfully",
        data=OrganisationMemberInvitationResponse(user_id=user.user_id, email=user.email),
        response=response,
    )


__all__ = ["organisation_membership_router", "user_auth"]
