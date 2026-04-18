from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Depends, Request
from fastapi import Response as FastAPIResponse
from sqlalchemy import select
from src.api.dependencies.rate_limit import rate_limit_login, rate_limit_password_reset
from src.api.schemas.base_models import GenericResponse
from src.api.utils.http_response import Response
from src.domain.models.admin_models import PasswordResetAction
from src.domain.models.master_tables import SupabaseUserStatus, User
from src.domain.models.user_models import (
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

logger = get_logger("user_auth_handler")
router = APIRouter()

if TYPE_CHECKING:
    from sqlalchemy.sql.schema import Table

_user_table = getattr(User, "__table__", None)
if _user_table is None:
    raise RuntimeError("User table metadata is not initialized")
UserTable = cast("Table", _user_table)


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
            else:
                user_email = None

        if not user_email:
            logger.warning(f"User not found for email: {login_request.email}")
            return Response.unauthorized_error_response(
                UserAuthErrors.USER_NOT_FOUND, response=response
            )

        logger.debug("User found in database")
        request_timezone = validate_timezone_name(request.headers.get(USER_TIMEZONE_HEADER))
        effective_timezone = request_timezone or getattr(user, "timezone", None)

        # External call to Supabase — no DB session held
        try:
            supabase_response = supabase_user_client.sign_in_with_password(
                email=login_request.email, password=login_request.password
            )
        except Exception:
            logger.warning(f"Invalid credentials for user: {login_request.email}")
            return Response.unauthorized_error_response(
                UserAuthErrors.INVALID_CREDENTIALS, response=response
            )

        user_data = supabase_response.get("user", {})
        session_data = supabase_response.get("session", {})

        access_token = session_data.get("access_token")
        refresh_token = session_data.get("refresh_token")

        if not access_token:
            logger.warning(f"Invalid credentials for user: {login_request.email}")
            return Response.unauthorized_error_response(
                UserAuthErrors.INVALID_CREDENTIALS, response=response
            )

        requires_password_change = user_data.get("user_metadata", {}).get(
            "requires_password_change", False
        )
        email_verified = user_data.get("user_metadata", {}).get("email_verified", False)

        if requires_password_change:
            # External call to Supabase — no DB session held
            try:
                last_login_at = datetime.now(UTC)
                if request_timezone:
                    async with master_async_session_scope() as session:
                        statement = select(User).where(UserTable.c.user_id == user_id)
                        result = await session.execute(statement)
                        user_obj = result.scalars().first()
                        if user_obj:
                            user_obj.last_login_at = last_login_at
                            user_obj.timezone = request_timezone
                            session.add(user_obj)
                            await session.commit()
                else:
                    async with master_async_session_scope() as session:
                        statement = select(User).where(UserTable.c.user_id == user_id)
                        result = await session.execute(statement)
                        user_obj = result.scalars().first()
                        if user_obj:
                            user_obj.last_login_at = last_login_at
                            session.add(user_obj)
                            await session.commit()

                password_reset_link = supabase_user_client.generate_password_reset_link(
                    email=login_request.email
                )

                response_data = UserLoginResponse(
                    state=UserLoginState.PENDING_SETUP,
                    last_login_at=last_login_at,
                    timezone=effective_timezone,
                    password_reset_link=password_reset_link,
                )
                return Response.success_response(
                    message="Login successful, password change required",
                    data=response_data,
                )
            except Exception as e:
                logger.error(f"Failed to generate recovery token for user {user_email}: {str(e)}")
                return Response.internal_server_error_response(
                    message=UserAuthErrors.PASSWORD_CHANGE_TOKEN_FAILED,
                    data=None,
                    response=response,
                )
        else:
            # Re-acquire session briefly to update last_login_at
            last_login_at = datetime.now(UTC)
            async with master_async_session_scope() as session:
                statement = select(User).where(UserTable.c.user_id == user_id)
                result = await session.execute(statement)
                user_obj = result.scalars().first()
                if user_obj:
                    user_obj.last_login_at = last_login_at
                    if request_timezone:
                        user_obj.timezone = request_timezone
                    session.add(user_obj)
                    await session.commit()

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


@router.post("/reset-password", response_model=GenericResponse[None], tags=["user_auth"])
async def reset_password(
    password_request: UserResetPasswordRequest,
    response: FastAPIResponse,
) -> GenericResponse[None]:
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

        if password_request.action not in [
            PasswordResetAction.FORCE_CHANGE,
            PasswordResetAction.FORGOT,
        ]:
            return Response.bad_request_error_response(
                UserAuthErrors.INVALID_ACTION, response=response
            )

        # External call to Supabase — no DB session held
        try:
            user_id = supabase_user_client.reset_password_with_token(
                token_hash=password_request.token_hash,
                new_password=password_request.new_password,
            )
        except Exception as e:
            logger.error(f"Failed to reset password with recovery token: {str(e)}")
            return Response.bad_request_error_response(
                UserAuthErrors.INVALID_RECOVERY_TOKEN, response=response
            )

        if password_request.action != PasswordResetAction.FORCE_CHANGE:
            logger.info(
                "Password reset completed for user_id %s (forgot action - no metadata update)",
                user_id,
            )
            return Response.success_response(
                message="Password changed successfully. You can now log in with your new password."
            )

        # Look up user and extract fields needed for Supabase metadata update
        async with master_async_session_scope() as session:
            statement = select(User).where(UserTable.c.supabase_user_id == user_id)
            result = await session.execute(statement)
            current_user = result.scalars().first()
            if current_user:
                cu_email = current_user.email
                cu_first_name = current_user.first_name
                cu_last_name = current_user.last_name
                cu_user_role = current_user.user_role
                cu_supabase_user_id = current_user.supabase_user_id
                cu_id = current_user.user_id
            else:
                cu_email = None

        if not cu_email:
            logger.warning(f"User not found for user_id: {user_id}")
            return Response.unauthorized_error_response(
                UserAuthErrors.UNAUTHORIZED, response=response
            )

        # External call to Supabase — no DB session held
        if cu_supabase_user_id:
            updated_metadata = {
                "requires_password_change": False,
                "email_verified": True,
                "first_name": cu_first_name,
                "last_name": cu_last_name,
                "role": cu_user_role,
            }
            try:
                supabase_user_client.update_user_metadata(
                    user_id=cu_supabase_user_id,
                    metadata=updated_metadata,
                )
            except Exception:
                logger.error(
                    "Failed to update user metadata in Supabase for user: %s",
                    cu_email,
                )
                return Response.internal_server_error_response(
                    message=UserAuthErrors.UPDATE_METADATA_FAILED,
                    data=None,
                    response=response,
                )

        # Re-acquire session briefly to update local DB
        logger.info(f"Updating local DB for user_id: {user_id}")
        async with master_async_session_scope() as session:
            statement = select(User).where(UserTable.c.user_id == cu_id)
            result = await session.execute(statement)
            user_obj = result.scalars().first()
            if user_obj:
                user_obj.email_verified = True
                user_obj.status = SupabaseUserStatus.ACTIVE
                user_obj.last_login_at = datetime.now(UTC)
                session.add(user_obj)
                await session.commit()
        logger.info(f"Local DB updated for user_id: {user_id}")

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
    "/refresh-token",
    response_model=GenericResponse[UserRefreshTokenResponse],
    tags=["user_auth"],
)
async def refresh_token(
    refresh_request: UserRefreshTokenRequest,
    response: FastAPIResponse,
) -> GenericResponse[UserRefreshTokenResponse]:
    try:
        session_data = supabase_user_client.refresh_access_token(refresh_request.refresh_token)

        if session_data.get("session", {}).get("access_token"):
            return Response.success_response(
                message="Token refreshed successfully",
                data=UserRefreshTokenResponse(
                    access_token=session_data["session"]["access_token"],
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
            else:
                user_email = None

        if not user_email:
            logger.warning(
                f"Password reset requested for non-existent user: {forgot_password_request.email}"
            )
            return Response.success_response(
                message="If an account with this email exists, a password reset link has been sent."
            )

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
            ),
        )
    except Exception as e:
        logger.error("Error fetching current user profile: %s", str(e), exc_info=True)
        return Response.internal_server_error_response(
            message=UserAuthErrors.AUTHENTICATION_FAILED, data=None, response=response
        )


@router.post("/logout", response_model=GenericResponse[None], tags=["user_auth"])
async def user_logout(
    logout_request: UserLogoutRequest,
    response: FastAPIResponse,
    current_user: User = Depends(user_auth),
) -> GenericResponse[None]:
    """Logout the current user. If `global_logout` is true, sign out from all sessions."""
    try:
        if current_user.supabase_user_id:
            supabase_user_client.sign_out_user(
                user_id=current_user.supabase_user_id,
                global_signout=logout_request.global_logout,
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
