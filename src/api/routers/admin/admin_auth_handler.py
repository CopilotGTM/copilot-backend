from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, Request
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.rate_limit import rate_limit_admin_login, rate_limit_password_reset
from src.api.schemas.base_models import GenericResponse
from src.api.utils.http_response import Response
from src.domain.models.admin_models import (
    AdminChangePasswordRequest,
    AdminForgotPasswordRequest,
    AdminLoginRequest,
    AdminLoginResponse,
    AdminLogoutRequest,
    AdminRefreshTokenRequest,
    AdminRefreshTokenResponse,
    AdminResetPasswordRequest,
)
from src.domain.models.master_tables import SuperAdmin
from src.infra.auth.admin_auth import admin_auth
from src.infra.auth.email_service import email_service
from src.infra.auth.supabase_admin_client import (
    SupabaseAuthError,
    supabase_admin_client,
)
from src.infra.config.constant import AdminAuthErrors
from src.infra.utils.logger import get_logger
from src.service.admin.admin_auth_service import (
    ActiveLoginResult,
    AdminAuthService,
    AdminAuthServiceError,
    AdminNotActiveError,
    AdminNotApprovedError,
    AdminNotFoundError,
    InvalidCredentialsError,
    InvalidRecoveryTokenError,
    PasswordMismatchError,
    PendingSetupResult,
)

logger = get_logger("admin_auth_handler")
router = APIRouter()


def _extract_bearer_token(request: Request) -> str | None:
    """Extract bearer token from request Authorization header."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    return auth_header.split(" ", 1)[1].strip() or None


async def get_admin_auth_service(
    session: AsyncSession = Depends(get_master_async_session),
) -> AsyncGenerator[AdminAuthService, None]:
    """Dependency that provides AdminAuthService with session ownership.

    The service owns the session and closes it in the finally block.
    """
    service = AdminAuthService(session)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("AdminAuthService close failed", exc_info=True)


@router.post("/login", response_model=GenericResponse[AdminLoginResponse], tags=["admin_auth"])
async def admin_login(
    login_request: AdminLoginRequest,
    response: FastAPIResponse,
    service: AdminAuthService = Depends(get_admin_auth_service),
    _rl_admin_login: None = Depends(rate_limit_admin_login),
) -> GenericResponse[AdminLoginResponse]:
    """
    Super Admin login endpoint.
    Verifies credentials and returns admin details and session tokens.
    Rate limited to 3 attempts per minute, 20 per hour.
    """
    try:
        result = await service.login(login_request.email, login_request.password)

        if isinstance(result, PendingSetupResult):
            response_data = {
                "state": result.state,
                "password_reset_link": result.password_reset_link,
            }
            return Response.success_response(message="Login successful", data=response_data)

        if isinstance(result, ActiveLoginResult):
            return Response.success_response(
                message="Login successful", data=result.to_response_data()
            )

        # Fallback for unexpected result type
        logger.error("Unexpected login result type: %s", type(result))
        return Response.internal_server_error_response(
            message=AdminAuthErrors.LOGIN_FAILED, response=response
        )

    except AdminNotFoundError:
        return Response.unauthorized_error_response(
            AdminAuthErrors.USER_NOT_FOUND, response=response
        )
    except AdminNotApprovedError:
        return Response.forbidden_error_response(
            AdminAuthErrors.ACCOUNT_NOT_APPROVED, response=response
        )
    except AdminNotActiveError:
        return Response.forbidden_error_response(
            AdminAuthErrors.ACCOUNT_NOT_ACTIVE, response=response
        )
    except InvalidCredentialsError:
        return Response.unauthorized_error_response(
            AdminAuthErrors.INVALID_CREDENTIALS, response=response
        )
    except AdminAuthServiceError as e:
        logger.error("Login service error: %s", e.message)
        return Response.internal_server_error_response(message=e.message, response=response)
    except Exception as e:
        logger.error(
            "Error during super admin login for email %s: %s",
            login_request.email,
            str(e),
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message=AdminAuthErrors.LOGIN_FAILED, response=response
        )


@router.post("/reset-password", response_model=GenericResponse[None], tags=["admin_auth"])
async def reset_password(
    password_request: AdminResetPasswordRequest,
    response: FastAPIResponse,
    service: AdminAuthService = Depends(get_admin_auth_service),
) -> GenericResponse[None]:
    """
    First-time or forgot-password reset endpoint for Super Admin.
    Requires a Supabase recovery token.
    """
    try:
        await service.reset_password(
            token_hash=password_request.token_hash,
            new_password=password_request.new_password,
            confirm_password=password_request.confirm_password,
            action=password_request.action,
        )
        return Response.success_response(
            message="Password changed successfully. You can now log in."
        )

    except PasswordMismatchError:
        return Response.bad_request_error_response(
            AdminAuthErrors.PASSWORD_MISMATCH, response=response
        )
    except InvalidRecoveryTokenError:
        return Response.bad_request_error_response(
            AdminAuthErrors.INVALID_RECOVERY_TOKEN, response=response
        )
    except AdminNotFoundError:
        return Response.unauthorized_error_response(AdminAuthErrors.UNAUTHORIZED, response=response)
    except AdminAuthServiceError as e:
        return Response.bad_request_error_response(e.message, response=response)
    except Exception as e:
        logger.error("Error during password reset: %s", str(e), exc_info=True)
        return Response.internal_server_error_response(
            AdminAuthErrors.PASSWORD_RESET_FAILED, response=response
        )


@router.post(
    "/refresh-token",
    response_model=GenericResponse[AdminRefreshTokenResponse],
    tags=["admin_auth"],
)
async def refresh_token(
    refresh_request: AdminRefreshTokenRequest,
    response: FastAPIResponse,
) -> GenericResponse[AdminRefreshTokenResponse]:
    """Refresh the access token using a refresh token."""
    try:
        session_data = supabase_admin_client.refresh_access_token(refresh_request.refresh_token)

        new_session = session_data.get("session")
        if new_session and new_session.get("access_token"):
            return Response.success_response(
                message="Token refreshed successfully",
                data=AdminRefreshTokenResponse(
                    access_token=new_session["access_token"],
                    refresh_token=new_session["refresh_token"],
                ),
            )

        logger.warning("Refresh token is invalid or expired.")
        return Response.unauthorized_error_response(
            AdminAuthErrors.REFRESH_TOKEN_INVALID, response=response
        )
    except SupabaseAuthError as e:
        logger.error(f"Token refresh failed: {e.message}")
        if e.status_code == 400:
            return Response.bad_request_error_response(e.message, response=response)
        elif e.status_code == 401:
            return Response.unauthorized_error_response(e.message, response=response)
        elif e.status_code == 404:
            return Response.not_found_error_response(e.message, response=response)
        elif e.status_code == 409:
            return Response.conflict_error_response(e.message, response=response)
        else:
            return Response.internal_server_error_response(message=e.message, response=response)
    except Exception as e:
        logger.error("Error during token refresh: %s", str(e), exc_info=True)
        return Response.internal_server_error_response(
            AdminAuthErrors.REFRESH_TOKEN_FAILED, response=response
        )


@router.post("/forgot-password", response_model=GenericResponse[None], tags=["admin_auth"])
async def forgot_password(
    forgot_password_request: AdminForgotPasswordRequest,
    response: FastAPIResponse,
    service: AdminAuthService = Depends(get_admin_auth_service),
    _rl_password_reset: None = Depends(rate_limit_password_reset),
) -> GenericResponse[None]:
    """Triggers a password reset email for a super admin.
    Rate limited to 3 requests per 15 minutes, 10 per hour.
    """
    try:
        result = await service.forgot_password(forgot_password_request.email)

        # Release DB session before the email API call
        await service.close()

        if result.reset_link and result.admin_email:
            email_service.send_password_reset_email(
                to_email=result.admin_email,
                first_name=result.admin_first_name or "",
                reset_url=result.reset_link,
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
            AdminAuthErrors.FORGOT_PASSWORD_FAILED, response=response
        )


@router.post("/logout", response_model=GenericResponse[None], tags=["admin_auth"])
async def admin_logout(
    logout_request: AdminLogoutRequest,
    request: Request,
    response: FastAPIResponse,
    current_admin: SuperAdmin = Depends(admin_auth),
    service: AdminAuthService = Depends(get_admin_auth_service),
) -> GenericResponse[None]:
    """Unified logout endpoint for super admin."""
    try:
        bearer_token = _extract_bearer_token(request)
        message = await service.logout(
            current_admin=current_admin,
            logout_email=logout_request.email,
            global_logout=logout_request.global_logout,
            bearer_token=bearer_token,
        )
        return Response.success_response(message=message)

    except AdminNotFoundError:
        return Response.not_found_error_response(AdminAuthErrors.USER_NOT_FOUND, response=response)
    except AdminAuthServiceError as e:
        if e.error_code == "EMAIL_REQUIRED":
            return Response.bad_request_error_response(e.message, response=response)
        if e.error_code == "UNAUTHORIZED":
            return Response.unauthorized_error_response(e.message, response=response)
        return Response.internal_server_error_response(e.message, response=response)
    except Exception as e:
        logger.error("Error during logout: %s", str(e), exc_info=True)
        return Response.internal_server_error_response(
            AdminAuthErrors.LOGOUT_FAILED, response=response
        )


@router.post("/change-password", response_model=GenericResponse[None], tags=["admin_auth"])
async def change_password(
    password_request: AdminChangePasswordRequest,
    request: Request,
    response: FastAPIResponse,
    current_admin: SuperAdmin = Depends(admin_auth),
    service: AdminAuthService = Depends(get_admin_auth_service),
) -> GenericResponse[None]:
    """Change password for an active, logged-in Super Admin."""
    try:
        bearer_token = _extract_bearer_token(request)
        await service.change_password(
            current_admin=current_admin,
            current_password=password_request.current_password,
            new_password=password_request.new_password,
            bearer_token=bearer_token,
        )
        return Response.success_response(
            message="Password changed successfully. Please sign in again."
        )

    except InvalidCredentialsError:
        return Response.unauthorized_error_response(
            AdminAuthErrors.INCORRECT_CURRENT_PASSWORD, response=response
        )
    except Exception as e:
        logger.error(
            "Error during password change for email %s: %s",
            current_admin.email,
            str(e),
            exc_info=True,
        )
        return Response.internal_server_error_response(
            AdminAuthErrors.PASSWORD_CHANGE_FAILED, response=response
        )
