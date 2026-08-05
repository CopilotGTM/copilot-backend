"""User authentication endpoints for password and provider-backed sessions.

The frontend calls these routes for login, external identity completion,
recovery, refresh, profile bootstrap, and logout. Local user and organisation
policy is checked around Supabase operations so provider authentication alone
never grants application access.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Depends, Request
from fastapi import Response as FastAPIResponse
from sqlalchemy import and_, func, select
from src.api.dependencies.rate_limit import (
    rate_limit_login,
    rate_limit_password_reset,
    rate_limit_token_refresh,
)
from src.api.schemas.base_models import GenericResponse
from src.api.utils.http_response import Response
from src.domain.models.master_tables import (
    AdminUserStatus,
    Organisation,
    OrganisationStatus,
    SupabaseUserStatus,
    User,
    UserActionHistory,
    UserAuthenticationMethod,
    UserEventType,
)
from src.domain.models.master_tables.external_identity_models import (
    ExternalIdentityMapping,
    ExternalIdentityProviderType,
)
from src.domain.models.user_models import (
    UserCompletePasswordSetupRequest,
    UserForgotPasswordRequest,
    UserLoginRequest,
    UserLoginResponse,
    UserLoginState,
    UserLogoutRequest,
    UserRefreshTokenRequest,
    UserRefreshTokenResponse,
    UserResetPasswordRequest,
)
from src.infra.auth.email_service import email_service
from src.infra.auth.supabase_user_client import SupabaseAuthError, supabase_user_client
from src.infra.auth.user_auth import user_auth
from src.infra.config.constant import UserAuthErrors
from src.infra.database.sessions import master_async_session_scope
from src.infra.utils.logger import get_logger
from src.infra.utils.timezone import USER_TIMEZONE_HEADER, validate_timezone_name
from src.service.orgnisation.organisation_preferences import normalize_organisation_preferences
from src.service.users.auth_access_service import AuthAccessError, AuthAccessService
from src.service.users.external_auth_service import ExternalAuthError, ExternalAuthService
from src.service.users.user_preferences import user_preferences_for_auth

logger = get_logger("user_auth_handler")
router = APIRouter()

if TYPE_CHECKING:
    from sqlalchemy.sql.schema import Table

_user_table = getattr(User, "__table__", None)
if _user_table is None:
    raise RuntimeError("User table metadata is not initialized")
UserTable = cast("Table", _user_table)
_external_identity_table = getattr(ExternalIdentityMapping, "__table__", None)
if _external_identity_table is None:
    raise RuntimeError("External identity table metadata is not initialized")
ExternalIdentityTable = cast("Table", _external_identity_table)


async def _load_organisation_preferences(organisation_id):
    async with master_async_session_scope() as session:
        organisation = await session.get(Organisation, organisation_id)
        return normalize_organisation_preferences(
            organisation.preferences if organisation else None
        )


def _bearer_token_from_request(request: Request) -> str | None:
    authorization = request.headers.get("Authorization")
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization[7:].strip() or None


def _organisation_is_approved_and_active(organisation: Organisation) -> bool:
    status = getattr(organisation.status, "value", organisation.status)
    return bool(organisation.is_active) and status == OrganisationStatus.APPROVED.value


async def _record_successful_login(user_id, timezone: str | None) -> datetime:
    """Persist login metadata without turning an analytics write into an auth outage."""
    last_login_at = datetime.now(UTC)
    try:
        async with master_async_session_scope() as session:
            user = (
                (await session.execute(select(User).where(UserTable.c.user_id == user_id)))
                .scalars()
                .first()
            )
            if user:
                user.last_login_at = last_login_at
                if timezone:
                    user.timezone = timezone
                session.add(user)
                await session.commit()
    except Exception:
        logger.warning("Failed to persist successful login metadata", exc_info=True)
    return last_login_at


async def _record_auth_event(
    user_id, action: UserEventType, note: str, details: dict | None = None
) -> None:
    """Keep operational audit failures from changing an authentication result."""
    try:
        async with master_async_session_scope() as session:
            session.add(
                UserActionHistory(
                    user_id=user_id,
                    action=action,
                    note=note,
                    details=details,
                )
            )
            await session.commit()
    except Exception:
        logger.error("Failed to persist authentication audit event", exc_info=True)


@router.post("/login", response_model=GenericResponse[UserLoginResponse], tags=["user_auth"])
async def user_login(
    request: Request,
    login_request: UserLoginRequest,
    response: FastAPIResponse,
    _rl_login: None = Depends(rate_limit_login),
) -> GenericResponse[UserLoginResponse]:
    """
    User login endpoint.
    """
    try:
        # Look up user and extract fields, then release the session
        async with master_async_session_scope() as session:
            statement = select(User).where(UserTable.c.email == login_request.email)
            result = await session.execute(statement)
            user = result.scalars().first()
            if user:
                user_email = user.email
                user_first_name = user.first_name
                user_last_name = user.last_name
                user_role = user.user_role
                user_created_at = user.created_at
                user_id = user.user_id
                user_organisation_id = user.organisation_id
                user_profile_image_url = user.profile_image_url
                user_authentication_method = UserAuthenticationMethod(user.authentication_method)
                user_preferences = user_preferences_for_auth(user.preferences)
                organisation = await session.get(Organisation, user.organisation_id)
            else:
                user_email = None
                user_preferences = None
                organisation = None

        if user is None or not user_email:
            logger.warning(f"User not found for email: {login_request.email}")
            return Response.error_response(
                status_code=401,
                message="Invalid email or password",
                code="INVALID_CREDENTIALS",
                response=response,
            )

        if user_authentication_method != UserAuthenticationMethod.PASSWORD:
            method_error = AuthAccessService.sign_in_required_error(user_authentication_method)
            await _record_auth_event(
                user_id,
                UserEventType.AUTH_METHOD_REJECTED,
                "Password sign-in rejected for external-auth account",
                {"configured_method": user_authentication_method.value},
            )
            return Response.error_response(
                status_code=method_error.status_code,
                message=method_error.message,
                code=method_error.code,
                response=response,
            )

        logger.debug("User found in database")
        user_organisation_preferences = await _load_organisation_preferences(user_organisation_id)
        request_timezone = validate_timezone_name(request.headers.get(USER_TIMEZONE_HEADER))
        effective_timezone = request_timezone or getattr(user, "timezone", None)

        # External call to Supabase — no DB session held
        try:
            supabase_response = supabase_user_client.sign_in_with_password(
                email=login_request.email, password=login_request.password
            )
        except Exception:
            logger.warning(f"Invalid credentials for user: {login_request.email}")
            return Response.error_response(
                status_code=401,
                message="Invalid email or password",
                code="INVALID_CREDENTIALS",
                response=response,
            )

        user_data = supabase_response.get("user", {})
        session_data = supabase_response.get("session", {})

        access_token = session_data.get("access_token")
        refresh_token = session_data.get("refresh_token")

        if not access_token:
            logger.warning(f"Invalid credentials for user: {login_request.email}")
            return Response.error_response(
                status_code=401,
                message="Invalid email or password",
                code="INVALID_CREDENTIALS",
                response=response,
            )

        requires_password_change = user_data.get("user_metadata", {}).get(
            "requires_password_change", False
        )
        email_verified = user_data.get("user_metadata", {}).get("email_verified", False)

        if user_data.get("id") != user.supabase_user_id or not organisation:
            return Response.error_response(
                status_code=401,
                message="Invalid email or password",
                code="INVALID_CREDENTIALS",
                response=response,
            )
        try:
            if requires_password_change:
                if (
                    user.admin_status != AdminUserStatus.APPROVED
                    or not _organisation_is_approved_and_active(organisation)
                    or user.status
                    not in {SupabaseUserStatus.PENDING_SETUP, SupabaseUserStatus.ACTIVE}
                ):
                    raise AuthAccessError(
                        "ACCOUNT_INACTIVE",
                        "User account is not active",
                    )
            else:
                AuthAccessService.ensure_access_allowed(
                    user,
                    organisation,
                    UserAuthenticationMethod.PASSWORD,
                )
        except AuthAccessError as error:
            return Response.error_response(
                status_code=error.status_code,
                message=error.message,
                code=error.code,
                response=response,
            )

        if requires_password_change:
            last_login_at = await _record_successful_login(user_id, request_timezone)
            response_data = UserLoginResponse(
                state=UserLoginState.PENDING_SETUP,
                last_login_at=last_login_at,
                timezone=effective_timezone,
                profile_image_url=user_profile_image_url,
                organisation_id=user_organisation_id,
                organisation_preferences=user_organisation_preferences,
                user_preferences=user_preferences,
                tokens={
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                },
            )
            return Response.success_response(
                message="Login successful, password change required",
                data=response_data,
            )
        else:
            last_login_at = await _record_successful_login(user_id, request_timezone)

            response_data = UserLoginResponse(
                email=user_email,
                first_name=user_first_name,
                last_name=user_last_name,
                role=user_role,
                state=UserLoginState.ACTIVE,
                created_at=user_created_at,
                email_verified=email_verified,
                last_login_at=last_login_at,
                timezone=effective_timezone,
                profile_image_url=user_profile_image_url,
                organisation_id=user_organisation_id,
                organisation_preferences=user_organisation_preferences,
                user_preferences=user_preferences,
                tokens={
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                },
            )
            return Response.success_response(message="Login successful", data=response_data)

    except Exception as e:
        logger.error(
            "Error during user login for email %s: %s",
            login_request.email,
            str(e),
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message=UserAuthErrors.LOGIN_FAILED, data=None, response=response
        )


@router.post(
    "/{provider}/complete",
    response_model=GenericResponse[UserLoginResponse],
    tags=["user_auth"],
)
async def user_external_auth_complete(
    provider: str,
    request: Request,
    response: FastAPIResponse,
    _rl_login: None = Depends(rate_limit_login),
) -> GenericResponse[UserLoginResponse]:
    """Complete an invited Google Workspace or Microsoft Entra sign-in."""
    token = _bearer_token_from_request(request)
    if not token:
        return Response.error_response(
            status_code=401,
            message=UserAuthErrors.MISSING_AUTH_HEADER,
            code="AUTHENTICATION_FAILED",
            response=response,
        )

    try:
        verified = supabase_user_client.verify_session(token)
        claims = ExternalAuthService().extract_claims(
            verified.user,
            requested_provider=provider,
        )
        if verified.authentication_method != claims.authentication_method:
            raise ExternalAuthError("The session provider does not match the requested provider")
    except ExternalAuthError as error:
        return Response.error_response(
            status_code=error.status_code,
            message=error.message,
            code=error.code,
            response=response,
        )
    except Exception:
        logger.warning("External authentication token verification failed", exc_info=True)
        return Response.error_response(
            status_code=401,
            message=UserAuthErrors.AUTHENTICATION_FAILED,
            code="AUTHENTICATION_FAILED",
            response=response,
        )

    try:
        async with master_async_session_scope() as session:
            existing_mapping = (
                (
                    await session.execute(
                        select(ExternalIdentityMapping).where(
                            and_(
                                ExternalIdentityTable.c.provider == claims.mapping_provider,
                                ExternalIdentityTable.c.external_user_id == claims.provider_user_id,
                                ExternalIdentityTable.c.external_tenant_id
                                == claims.provider_tenant_id,
                            )
                        )
                    )
                )
                .scalars()
                .first()
            )

            if existing_mapping:
                user = await session.get(User, existing_mapping.user_id)
            else:
                user = (
                    (
                        await session.execute(
                            select(User)
                            .where(func.lower(UserTable.c.email) == claims.email)
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .first()
                )

            if not user:
                return Response.error_response(
                    status_code=404,
                    message="We couldn't find a CopilotGTM account for this email.",
                    code="ACCOUNT_NOT_FOUND",
                    response=response,
                )

            organisation = await session.get(Organisation, user.organisation_id)
            try:
                if not organisation:
                    raise AuthAccessError(
                        "ORGANISATION_INACTIVE",
                        "Organisation is not active",
                    )
                AuthAccessService.ensure_access_allowed(
                    user,
                    organisation,
                    verified.authentication_method,
                )
                ExternalAuthService().validate_provider_policy(
                    claims, organisation_domains=organisation.domain
                )
            except (AuthAccessError, ExternalAuthError) as error:
                return Response.error_response(
                    status_code=error.status_code,
                    message=error.message,
                    code=error.code,
                    response=response,
                )

            supabase_owner = (
                (
                    await session.execute(
                        select(User).where(UserTable.c.supabase_user_id == claims.supabase_user_id)
                    )
                )
                .scalars()
                .first()
            )
            if supabase_owner and supabase_owner.user_id != user.user_id:
                return Response.error_response(
                    status_code=409,
                    message="This external account is already linked to another user",
                    code="IDENTITY_CONFLICT",
                    response=response,
                )
            if user.supabase_user_id and user.supabase_user_id != claims.supabase_user_id:
                return Response.error_response(
                    status_code=409,
                    message="This user is already linked to another sign-in identity",
                    code="IDENTITY_CONFLICT",
                    response=response,
                )

            if existing_mapping and existing_mapping.user_id != user.user_id:
                return Response.error_response(
                    status_code=409,
                    message="This external account is already linked to another user",
                    code="IDENTITY_CONFLICT",
                    response=response,
                )

            now = datetime.now(UTC)
            user.supabase_user_id = claims.supabase_user_id
            user.email_verified = True
            user.last_login_at = now
            user.timezone = (
                validate_timezone_name(request.headers.get(USER_TIMEZONE_HEADER)) or user.timezone
            )
            user.profile_image_url = claims.profile_image_url or user.profile_image_url
            session.add(user)
            if existing_mapping:
                existing_mapping.is_active = True
                existing_mapping.last_used_at = now
                session.add(existing_mapping)
            else:
                session.add(
                    ExternalIdentityMapping(
                        user_id=user.user_id,
                        organisation_id=user.organisation_id,
                        provider=claims.mapping_provider,
                        provider_type=ExternalIdentityProviderType.OAUTH,
                        external_user_id=claims.provider_user_id,
                        external_tenant_id=claims.provider_tenant_id,
                        external_user_email=claims.email,
                        external_user_name=claims.full_name,
                        is_active=True,
                        linked_at=now,
                        last_used_at=now,
                    )
                )
                session.add(
                    UserActionHistory(
                        user_id=user.user_id,
                        action=UserEventType.IDENTITY_LINKED,
                        note="External sign-in identity linked",
                        details={"provider": claims.mapping_provider.value},
                    )
                )
            await session.commit()

            return Response.success_response(
                message="Login successful",
                data=UserLoginResponse(
                    email=user.email,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    role=user.user_role,
                    state=UserLoginState.ACTIVE,
                    created_at=user.created_at,
                    email_verified=user.email_verified,
                    last_login_at=user.last_login_at,
                    timezone=user.timezone,
                    profile_image_url=user.profile_image_url,
                    organisation_id=user.organisation_id,
                    organisation_preferences=normalize_organisation_preferences(
                        organisation.preferences
                    ),
                    user_preferences=user_preferences_for_auth(user.preferences),
                ),
            )
    except Exception:
        logger.error("Error during external authentication completion", exc_info=True)
        return Response.internal_server_error_response(
            message=UserAuthErrors.LOGIN_FAILED, data=None, response=response
        )


@router.post("/reset-password", response_model=GenericResponse[None], tags=["user_auth"])
async def reset_password(
    password_request: UserResetPasswordRequest,
    response: FastAPIResponse,
    _rl_password_reset: None = Depends(rate_limit_password_reset),
) -> GenericResponse[None]:
    """Replace a password only when recovery and local account policy both pass."""
    try:
        if not password_request.token_hash:
            return Response.bad_request_error_response(
                UserAuthErrors.RECOVERY_TOKEN_REQUIRED, response=response
            )

        if not password_request.new_password:
            return Response.bad_request_error_response(
                UserAuthErrors.NEW_PASSWORD_REQUIRED, response=response
            )

        if (
            password_request.confirm_password
            and password_request.new_password != password_request.confirm_password
        ):
            return Response.bad_request_error_response(
                UserAuthErrors.PASSWORD_MISMATCH, response=response
            )

        try:
            user_id = supabase_user_client.verify_recovery_token(password_request.token_hash)
        except Exception as e:
            logger.error(f"Failed to reset password with recovery token: {str(e)}")
            return Response.error_response(
                status_code=400,
                message=UserAuthErrors.INVALID_RECOVERY_TOKEN,
                code="INVALID_RECOVERY_TOKEN",
                response=response,
            )

        async with master_async_session_scope() as session:
            statement = select(User).where(UserTable.c.supabase_user_id == user_id)
            result = await session.execute(statement)
            current_user = result.scalars().first()
            organisation = (
                await session.get(Organisation, current_user.organisation_id)
                if current_user
                else None
            )

        if (
            not current_user
            or not organisation
            or UserAuthenticationMethod(current_user.authentication_method)
            != UserAuthenticationMethod.PASSWORD
            or current_user.admin_status != AdminUserStatus.APPROVED
            or not _organisation_is_approved_and_active(organisation)
        ):
            logger.warning("Recovery rejected by local authentication policy")
            return Response.error_response(
                status_code=400,
                message=UserAuthErrors.INVALID_RECOVERY_TOKEN,
                code="INVALID_RECOVERY_TOKEN",
                response=response,
            )

        try:
            supabase_user_client.update_password(user_id, password_request.new_password)
        except Exception:
            logger.warning("Recovery password update failed", exc_info=True)
            return Response.error_response(
                status_code=400,
                message=UserAuthErrors.INVALID_RECOVERY_TOKEN,
                code="INVALID_RECOVERY_TOKEN",
                response=response,
            )

        return Response.success_response(
            message="Password changed successfully. You can now log in with your new password."
        )

    except Exception as e:
        logger.error(
            "Error during first-time password change: %s",
            str(e),
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message=UserAuthErrors.PASSWORD_RESET_FAILED, data=None, response=response
        )


@router.post(
    "/complete-password-setup",
    response_model=GenericResponse[None],
    tags=["user_auth"],
)
async def complete_password_setup(
    request: Request,
    setup_request: UserCompletePasswordSetupRequest,
    response: FastAPIResponse,
    _rl_password_reset: None = Depends(rate_limit_password_reset),
) -> GenericResponse[None]:
    """Replace a temporary password from its authenticated password session."""
    token = _bearer_token_from_request(request)
    if not token:
        return Response.error_response(
            status_code=401,
            message=UserAuthErrors.MISSING_AUTH_HEADER,
            code="AUTHENTICATION_FAILED",
            response=response,
        )
    if setup_request.new_password != setup_request.confirm_password:
        return Response.bad_request_error_response(
            UserAuthErrors.PASSWORD_MISMATCH,
            response=response,
        )

    try:
        verified = supabase_user_client.verify_session(token)
        async with master_async_session_scope() as session:
            result = await session.execute(
                select(User).where(UserTable.c.supabase_user_id == verified.supabase_user_id)
            )
            user = result.scalars().first()
            organisation = await session.get(Organisation, user.organisation_id) if user else None

            if (
                not user
                or not organisation
                or verified.authentication_method != UserAuthenticationMethod.PASSWORD
                or UserAuthenticationMethod(user.authentication_method)
                != UserAuthenticationMethod.PASSWORD
                or user.status != SupabaseUserStatus.PENDING_SETUP
                or user.admin_status != AdminUserStatus.APPROVED
                or not _organisation_is_approved_and_active(organisation)
            ):
                return Response.error_response(
                    status_code=403,
                    message="Password setup is not available for this account",
                    code="AUTHENTICATION_FAILED",
                    response=response,
                )

            supabase_user_client.update_password(
                verified.supabase_user_id,
                setup_request.new_password,
            )
            supabase_user_client.update_user_metadata(
                user_id=verified.supabase_user_id,
                metadata={
                    "requires_password_change": False,
                    "email_verified": True,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "role": getattr(user.user_role, "value", user.user_role),
                },
            )
            user.status = SupabaseUserStatus.ACTIVE
            user.email_verified = True
            user.last_login_at = datetime.now(UTC)
            session.add(user)
            await session.commit()

        supabase_user_client.revoke_session(token)
        return Response.success_response(message="Password setup completed. Please sign in again.")
    except Exception:
        logger.warning("Authenticated password setup failed", exc_info=True)
        return Response.error_response(
            status_code=400,
            message="Password setup failed",
            code="AUTHENTICATION_FAILED",
            response=response,
        )


@router.post(
    "/refresh-token",
    response_model=GenericResponse[UserRefreshTokenResponse],
    tags=["user_auth"],
)
async def refresh_token(
    refresh_request: UserRefreshTokenRequest,
    response: FastAPIResponse,
    _rl_refresh: None = Depends(rate_limit_token_refresh),
) -> GenericResponse[UserRefreshTokenResponse]:
    """Refresh Supabase credentials and recheck current local access policy."""
    try:
        session_data = supabase_user_client.refresh_access_token(refresh_request.refresh_token)

        access_token = session_data.get("session", {}).get("access_token")
        if access_token:
            verified = supabase_user_client.verify_session(access_token)
            async with master_async_session_scope() as session:
                result = await session.execute(
                    select(User).where(UserTable.c.supabase_user_id == verified.supabase_user_id)
                )
                user = result.scalars().first()
                organisation = (
                    await session.get(Organisation, user.organisation_id) if user else None
                )
            if not user or not organisation:
                raise AuthAccessError(
                    "ACCOUNT_INACTIVE",
                    "User account is not active",
                )
            AuthAccessService.ensure_access_allowed(
                user,
                organisation,
                verified.authentication_method,
            )
            return Response.success_response(
                message="Token refreshed successfully",
                data=UserRefreshTokenResponse(
                    access_token=access_token,
                    refresh_token=session_data["session"]["refresh_token"],
                ),
            )
        else:
            logger.warning(
                "Refresh token is invalid or expired",
                extra={
                    "event": "auth_failure",
                    "auth_operation": "refresh_access_token",
                    "failure_reason": UserAuthErrors.REFRESH_TOKEN_INVALID,
                },
            )
            return Response.unauthorized_error_response(
                UserAuthErrors.REFRESH_TOKEN_INVALID, response=response
            )
    except AuthAccessError as error:
        return Response.error_response(
            status_code=error.status_code,
            message=error.message,
            code=error.code,
            response=response,
        )
    except SupabaseAuthError as e:
        log_method = logger.warning if e.status_code in {400, 401} else logger.error
        log_method(
            "Token refresh failed",
            extra={
                "event": "auth_failure",
                "auth_operation": "refresh_access_token",
                "failure_reason": e.message,
                "status_code": e.status_code,
            },
        )
        # Use the status code and message from the custom exception
        if e.status_code == 400:
            return Response.bad_request_error_response(e.message, response=response)
        elif e.status_code == 401:
            return Response.unauthorized_error_response(e.message, response=response)
        elif e.status_code == 404:
            return Response.not_found_error_response(e.message, response=response)
        elif e.status_code == 409:
            return Response.conflict_error_response(e.message, response=response)
        else:
            return Response.internal_server_error_response(
                message=e.message, data=None, response=response
            )
    except Exception as e:
        logger.error(
            "Error during token refresh: %s",
            str(e),
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message=UserAuthErrors.REFRESH_TOKEN_FAILED, data=None, response=response
        )


@router.post("/forgot-password", response_model=GenericResponse[None], tags=["user_auth"])
async def forgot_password(
    forgot_password_request: UserForgotPasswordRequest,
    response: FastAPIResponse,
    _rl_password_reset: None = Depends(rate_limit_password_reset),
) -> GenericResponse[None]:
    """
    Initiate password reset for a user.
    Rate limited to 3 requests per 15 minutes, 10 per hour.
    """
    try:
        # Look up user email and name, then release the session
        async with master_async_session_scope() as session:
            statement = select(User).where(UserTable.c.email == forgot_password_request.email)
            result = await session.execute(statement)
            user = result.scalars().first()
            if user:
                user_email = user.email
                user_first_name = user.first_name
                user_authentication_method = UserAuthenticationMethod(user.authentication_method)
            else:
                user_email = None

        if user is None or user_authentication_method != UserAuthenticationMethod.PASSWORD:
            logger.warning("Password recovery suppressed for unknown or non-password account")
            if user is not None:
                await _record_auth_event(
                    user.user_id,
                    UserEventType.RECOVERY_SUPPRESSED,
                    "Password recovery suppressed for external-auth account",
                )
            return Response.success_response(
                message="If an account with this email exists, a password reset link has been sent."
            )

        user_email = user.email
        # External call to Supabase — no DB session held
        password_reset_link = supabase_user_client.generate_password_reset_link(email=user_email)
        logger.info(f"Generated password reset link for: {forgot_password_request.email}")
        if not password_reset_link:
            logger.error(
                f"Failed to generate password reset link for: {forgot_password_request.email}"
            )
            return Response.internal_server_error_response(
                message=UserAuthErrors.RESET_LINK_FAILED, data=None, response=response
            )
        logger.info(f"Password reset link generated for user: {user_email}")

        # External call to email service — no DB session held
        email_sent = await email_service.send_password_reset_email(
            to_email=user_email,
            first_name=user_first_name,
            reset_url=password_reset_link,
        )

        if not email_sent:
            logger.error(
                f"Failed to send password reset email for: {forgot_password_request.email}. Email service returned False."
            )
            return Response.internal_server_error_response(
                message=UserAuthErrors.EMAIL_SEND_FAILED, data=None, response=response
            )

        return Response.success_response(
            message="If an account with this email exists, a password reset link has been sent."
        )

    except Exception as e:
        logger.error(
            "Error during forgot password request for email %s: %s",
            forgot_password_request.email,
            str(e),
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message=UserAuthErrors.FORGOT_PASSWORD_FAILED, data=None, response=response
        )


@router.get("/me", response_model=GenericResponse[UserLoginResponse], tags=["user_auth"])
async def get_current_user_profile(
    response: FastAPIResponse,
    current_user: User = Depends(user_auth),
) -> GenericResponse[UserLoginResponse]:
    """Return the current authenticated user profile for mobile session bootstrap."""
    try:
        organisation_preferences = await _load_organisation_preferences(
            current_user.organisation_id
        )
        return Response.success_response(
            message="Current user fetched successfully",
            data=UserLoginResponse(
                email=current_user.email,
                first_name=current_user.first_name,
                last_name=current_user.last_name,
                role=current_user.user_role,
                state=UserLoginState.ACTIVE,
                created_at=current_user.created_at,
                email_verified=current_user.email_verified,
                last_login_at=current_user.last_login_at,
                timezone=current_user.timezone,
                profile_image_url=current_user.profile_image_url,
                organisation_id=current_user.organisation_id,
                organisation_preferences=organisation_preferences,
                user_preferences=user_preferences_for_auth(current_user.preferences),
            ),
        )
    except Exception as e:
        logger.error("Error fetching current user profile: %s", str(e), exc_info=True)
        return Response.internal_server_error_response(
            message=UserAuthErrors.AUTHENTICATION_FAILED, data=None, response=response
        )


@router.post("/logout", response_model=GenericResponse[None], tags=["user_auth"])
async def user_logout(
    request: Request,
    logout_request: UserLogoutRequest,
    response: FastAPIResponse,
    current_user: User = Depends(user_auth),
) -> GenericResponse[None]:
    """Logout the current user. If `global_logout` is true, sign out from all sessions."""
    try:
        token = _bearer_token_from_request(request)
        if not token:
            return Response.error_response(
                status_code=401,
                message=UserAuthErrors.MISSING_AUTH_HEADER,
                code="AUTHENTICATION_FAILED",
                response=response,
            )
        supabase_user_client.revoke_session(
            token,
            global_signout=logout_request.global_logout,
        )
        await _record_auth_event(
            current_user.user_id,
            UserEventType.SESSION_REVOKED,
            "All sessions revoked" if logout_request.global_logout else "Current session revoked",
            {"scope": "global" if logout_request.global_logout else "local"},
        )

        message = (
            "Successfully logged out from all sessions."
            if logout_request.global_logout
            else "Successfully logged out."
        )
        return Response.success_response(message=message)
    except Exception as e:
        logger.error("Error during user logout: %s", str(e), exc_info=True)
        return Response.internal_server_error_response(
            UserAuthErrors.LOGOUT_FAILED, response=response
        )
