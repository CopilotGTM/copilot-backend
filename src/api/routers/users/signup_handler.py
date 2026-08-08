"""Thin public endpoints for verified provider organisation signup and status."""

from uuid import UUID

from fastapi import APIRouter, Depends, Request, Security
from fastapi import Response as FastAPIResponse
from sqlalchemy import select
from sqlmodel import col
from src.api.dependencies.rate_limit import rate_limit_signup, rate_limit_signup_status
from src.api.schemas.base_models import GenericResponse
from src.api.utils.http_response import Response
from src.domain.models.master_tables import (
    AdminUserStatus,
    Organisation,
    SignupRequestStatus,
    SupabaseUserStatus,
    User,
    UserAppRole,
    UserAuthenticationMethod,
)
from src.domain.models.user_signup_models import (
    ExistingOrganisationJoinRequest,
    ExternalSignupProfile,
    ExternalSignupRequest,
    ExternalSignupResponse,
)
from src.infra.auth.email_service import email_service
from src.infra.auth.supabase_user_client import SupabaseAuthError, supabase_user_client
from src.infra.auth.user_auth import user_bearer
from src.infra.database.sessions import master_async_session_scope
from src.infra.utils.logger import get_logger
from src.service.users.external_auth_service import (
    ExternalAuthError,
    ExternalAuthService,
    ExternalSignupIdentity,
)
from src.service.users.organisation_membership_service import (
    MembershipResult,
    OrganisationMembershipError,
    OrganisationMembershipService,
)
from src.service.users.signup_service import SignupService, SignupServiceError
from src.workers.organization_worker.client import send_signup_provisioning_task

logger = get_logger("user_signup_handler")
router = APIRouter()


def _bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization")
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization[7:].strip() or None


def _response(signup) -> ExternalSignupResponse:
    is_active = signup.status == SignupRequestStatus.ACTIVE
    return ExternalSignupResponse(
        signup_request_id=signup.signup_request_id,
        status=signup.status,
        organisation_id=signup.organisation_id if is_active else None,
        user_id=signup.user_id if is_active else None,
    )


async def _notify_membership_admins(
    result: MembershipResult,
    identity: ExternalSignupIdentity,
) -> None:
    organisation_id = result.signup.organisation_id
    if organisation_id is None:
        return
    async with master_async_session_scope() as session:
        organisation = await session.get(Organisation, organisation_id)
        admins = (
            (
                await session.execute(
                    select(User).where(
                        col(User.organisation_id) == organisation_id,
                        col(User.user_role) == UserAppRole.ADMIN,
                        col(User.status) == SupabaseUserStatus.ACTIVE,
                        col(User.admin_status) == AdminUserStatus.APPROVED,
                    )
                )
            )
            .scalars()
            .all()
        )
    if organisation is None:
        return
    for admin in admins:
        if result.status == SignupRequestStatus.ACTIVE:
            delivered = await email_service.send_domain_member_joined_email(
                str(admin.email), identity.claims.email, organisation.organisation_name
            )
        else:
            delivered = await email_service.send_domain_access_attempt_email(
                str(admin.email), identity.claims.email, organisation.organisation_name
            )
        if not delivered:
            logger.warning(
                "Membership notification delivery failed",
                extra={
                    "event": "membership_notification_delivery_failed",
                    "signup_request_id": str(result.signup.signup_request_id),
                    "organisation_id": str(organisation_id),
                },
            )


def _verify_identity(
    request: Request,
    *,
    provider: str | None,
) -> ExternalSignupIdentity:
    token = _bearer_token(request)
    if token is None:
        raise ExternalAuthError("Authentication is required")
    verified = supabase_user_client.verify_session(token)
    if provider is None:
        provider = (
            "google"
            if verified.authentication_method == UserAuthenticationMethod.GOOGLE_WORKSPACE
            else "microsoft"
            if verified.authentication_method == UserAuthenticationMethod.MICROSOFT_ENTRA
            else ""
        )
    claims = ExternalAuthService().extract_claims(
        verified.user,
        requested_provider=provider,
    )
    if claims.authentication_method != verified.authentication_method:
        raise ExternalAuthError("The session provider does not match the requested provider")
    return ExternalAuthService().validate_signup_identity(claims)


# Every signup lifecycle route requires a bearer session; handlers still bind the
# verified claims to the requested provider before reading or mutating signup state.
@router.put(
    "/{provider}/signup/draft",
    response_model=GenericResponse[ExternalSignupProfile],
    tags=["user_signup"],
    dependencies=[Security(user_bearer)],
)
async def put_external_signup_draft(
    provider: str,
    request: Request,
    response: FastAPIResponse,
    _rate_limit: None = Depends(rate_limit_signup),
) -> GenericResponse[ExternalSignupProfile]:
    """Verify signup eligibility and idempotently create or resume its draft."""
    if provider not in {"google", "microsoft"}:
        return Response.error_response(
            status_code=400,
            message="Unsupported signup provider",
            code="SIGNUP_PROVIDER_INVALID",
            response=response,
        )
    try:
        identity = _verify_identity(request, provider=provider)
        async with master_async_session_scope() as session:
            result = await SignupService(session).preflight(identity=identity)
        signup = result.signup
        return Response.success_response(
            message="Signup draft ready",
            data=ExternalSignupProfile(
                flow=result.flow,
                signup_request_id=signup.signup_request_id if signup else None,
                status=signup.status if signup else None,
                provider=provider,
                email=identity.claims.email,
                primary_domain=identity.primary_domain,
                full_name=identity.claims.full_name,
                profile_image_url=identity.claims.profile_image_url,
                organisation_name=(
                    signup.organisation_name
                    if signup and result.flow.value in {"create_organisation", "resume_signup"}
                    else None
                ),
            ),
            response=response,
        )
    except SupabaseAuthError:
        return Response.error_response(
            status_code=401,
            message="Authentication failed",
            code="AUTHENTICATION_FAILED",
            response=response,
        )
    except (ExternalAuthError, SignupServiceError) as exc:
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            code=exc.code,
            response=response,
        )


@router.post(
    "/{provider}/signup/join",
    response_model=GenericResponse[ExternalSignupResponse],
    status_code=202,
    tags=["user_signup"],
    dependencies=[Security(user_bearer)],
)
async def join_existing_organisation(
    provider: str,
    join_request: ExistingOrganisationJoinRequest,
    request: Request,
    response: FastAPIResponse,
    _rate_limit: None = Depends(rate_limit_signup),
) -> GenericResponse[ExternalSignupResponse]:
    """Join the draft's existing organisation under its current membership policy."""
    if provider not in {"google", "microsoft"}:
        return Response.error_response(
            status_code=400,
            message="Unsupported signup provider",
            code="SIGNUP_PROVIDER_INVALID",
            response=response,
        )
    try:
        identity = _verify_identity(request, provider=provider)
        async with master_async_session_scope() as session:
            result = await OrganisationMembershipService(session).join_verified_identity(
                identity,
                join_request.signup_request_id,
            )
        if result.notify_admins:
            try:
                await _notify_membership_admins(result, identity)
            except Exception:
                logger.warning(
                    "Post-commit membership notification failed",
                    extra={
                        "event": "membership_notification_failed",
                        "signup_request_id": str(result.signup.signup_request_id),
                        "organisation_id": str(result.signup.organisation_id),
                    },
                    exc_info=True,
                )
        message = (
            "Organisation membership activated"
            if result.status == SignupRequestStatus.ACTIVE
            else "An administrator invitation is required"
        )
        return Response.accepted_response(
            message=message,
            data=_response(result.signup),
            response=response,
        )
    except SupabaseAuthError:
        return Response.error_response(
            status_code=401,
            message="Authentication failed",
            code="AUTHENTICATION_FAILED",
            response=response,
        )
    except (ExternalAuthError, OrganisationMembershipError) as exc:
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            code=exc.code,
            response=response,
        )


@router.post(
    "/{provider}/signup",
    response_model=GenericResponse[ExternalSignupResponse],
    status_code=202,
    tags=["user_signup"],
    dependencies=[Security(user_bearer)],
)
async def start_external_signup(
    provider: str,
    signup_request: ExternalSignupRequest,
    request: Request,
    response: FastAPIResponse,
    _rate_limit: None = Depends(rate_limit_signup),
) -> GenericResponse[ExternalSignupResponse]:
    """Finalize the verified subject's draft and dispatch it retry-safely."""
    if provider not in {"google", "microsoft"}:
        return Response.error_response(
            status_code=400,
            message="Unsupported signup provider",
            code="SIGNUP_PROVIDER_INVALID",
            response=response,
        )
    try:
        identity = _verify_identity(
            request,
            provider=provider,
        )
        async with master_async_session_scope() as session:
            signup = await SignupService(session).finalize_signup(
                identity=identity,
                request=signup_request,
            )
        # A request may commit before queue delivery fails or times out. Re-dispatch
        # while it remains RECEIVED; the worker's row locks make duplicates safe.
        if signup.status == SignupRequestStatus.RECEIVED:
            await send_signup_provisioning_task(signup.signup_request_id)
        return Response.accepted_response(
            message="Organisation signup accepted",
            data=_response(signup),
            response=response,
        )
    except SupabaseAuthError:
        return Response.error_response(
            status_code=401,
            message="Authentication failed",
            code="AUTHENTICATION_FAILED",
            response=response,
        )
    except (ExternalAuthError, SignupServiceError) as exc:
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            code=exc.code,
            response=response,
        )
    except Exception:
        logger.error("Signup verification or dispatch failed", exc_info=True)
        return Response.error_response(
            status_code=503,
            message="Signup is temporarily unavailable; retry with the same key",
            code="SIGNUP_DISPATCH_FAILED",
            response=response,
        )


@router.get(
    "/signup-requests/{signup_request_id}",
    response_model=GenericResponse[ExternalSignupResponse],
    tags=["user_signup"],
    dependencies=[Security(user_bearer)],
)
async def get_signup_status(
    signup_request_id: UUID,
    request: Request,
    response: FastAPIResponse,
    _rate_limit: None = Depends(rate_limit_signup_status),
) -> GenericResponse[ExternalSignupResponse]:
    """Return durable status only to the same remotely verified provider identity."""
    try:
        token = _bearer_token(request)
        if token is None:
            raise ExternalAuthError("Authentication is required")
        verified = supabase_user_client.verify_session(token)
        provider = (
            "google"
            if verified.authentication_method == UserAuthenticationMethod.GOOGLE_WORKSPACE
            else "microsoft"
            if verified.authentication_method == UserAuthenticationMethod.MICROSOFT_ENTRA
            else ""
        )
        claims = ExternalAuthService().extract_claims(
            verified.user,
            requested_provider=provider,
        )
        identity = ExternalSignupIdentity(claims=claims, primary_domain="")
        async with master_async_session_scope() as session:
            signup = await SignupService(session).get_owned_request(
                signup_request_id,
                identity,
            )
        if signup is None:
            return Response.error_response(
                status_code=404,
                message="Signup request not found",
                code="SIGNUP_REQUEST_NOT_FOUND",
                response=response,
            )
        return Response.success_response(
            message="Signup status retrieved",
            data=_response(signup),
        )
    except SupabaseAuthError:
        return Response.error_response(
            status_code=401,
            message="Authentication failed",
            code="AUTHENTICATION_FAILED",
            response=response,
        )
    except ExternalAuthError as exc:
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            code=exc.code,
            response=response,
        )
    except Exception:
        logger.error("Signup status retrieval failed", exc_info=True)
        return Response.error_response(
            status_code=503,
            message="Signup status is temporarily unavailable",
            code="SIGNUP_STATUS_UNAVAILABLE",
            response=response,
        )
