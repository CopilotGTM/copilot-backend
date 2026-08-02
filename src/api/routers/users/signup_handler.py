"""Thin public endpoints for verified provider organisation signup and status."""

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi import Response as FastAPIResponse
from src.api.dependencies.rate_limit import rate_limit_signup, rate_limit_signup_status
from src.api.schemas.base_models import GenericResponse
from src.api.utils.http_response import Response
from src.domain.models.master_tables import SignupRequestStatus, UserAuthenticationMethod
from src.domain.models.user_signup_models import ExternalSignupRequest, ExternalSignupResponse
from src.infra.auth.supabase_user_client import SupabaseAuthError, supabase_user_client
from src.infra.database.sessions import master_async_session_scope
from src.infra.utils.logger import get_logger
from src.service.users.external_auth_service import (
    ExternalAuthError,
    ExternalAuthService,
    ExternalSignupIdentity,
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
    return ExternalSignupResponse(
        signup_request_id=signup.signup_request_id,
        status=signup.status,
        organisation_id=signup.organisation_id,
        user_id=signup.user_id,
    )


def _verify_identity(
    request: Request,
    *,
    provider: str | None,
    requested_primary_domain: str,
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
    return ExternalAuthService().validate_signup_identity(
        claims,
        requested_primary_domain=requested_primary_domain,
    )


@router.post(
    "/{provider}/signup",
    response_model=GenericResponse[ExternalSignupResponse],
    status_code=202,
    tags=["user_signup"],
)
async def start_external_signup(
    provider: str,
    signup_request: ExternalSignupRequest,
    request: Request,
    response: FastAPIResponse,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    _rate_limit: None = Depends(rate_limit_signup),
) -> GenericResponse[ExternalSignupResponse]:
    """Verify provider authority, persist intent, and dispatch its UUID."""
    if provider not in {"google", "microsoft"}:
        return Response.error_response(
            status_code=400,
            message="Unsupported signup provider",
            code="SIGNUP_PROVIDER_INVALID",
            response=response,
        )
    if (
        idempotency_key is None
        or not 16 <= len(idempotency_key) <= 255
        or any(character.isspace() for character in idempotency_key)
    ):
        return Response.error_response(
            status_code=400,
            message="A valid Idempotency-Key is required",
            code="IDEMPOTENCY_KEY_REQUIRED",
            response=response,
        )
    try:
        identity = _verify_identity(
            request,
            provider=provider,
            requested_primary_domain=signup_request.primary_domain,
        )
        async with master_async_session_scope() as session:
            signup = await SignupService(session).start_signup(
                identity=identity,
                request=signup_request,
                idempotency_key=idempotency_key,
            )
        if signup.status != SignupRequestStatus.ACTIVE:
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
