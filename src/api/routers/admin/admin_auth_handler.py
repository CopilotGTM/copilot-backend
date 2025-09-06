from datetime import datetime, timezone
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from src.domain.models.admin_models import (
    AdminLoginRequest,
    AdminChangePasswordRequest,
    AdminLogoutRequest,
    AdminLoginResponse,
    AdminStatus,
    AdminRefreshTokenRequest,
    AdminRefreshTokenResponse,
    AdminForgotPasswordRequest,
    AdminResetPasswordRequest,
    PasswordResetAction,
)
from src.domain.models.master_tables import SuperAdmin
from src.api.dependencies.master_dependencies import get_master_async_session
from src.infra.auth.admin_auth import admin_auth
from src.infra.auth.email_service import email_service
from src.infra.auth.supabase_admin_client import supabase_admin_client, SupabaseAuthError
from src.infra.utils.logger import get_logger
from src.api.utils.http_response import Response
from src.api.schemas.base_models import GenericResponse
from src.infra.config.constant import AdminAuthErrors
from src.service.admin.admin_auth_service import admin_auth_service

logger = get_logger("admin_auth_handler")
router = APIRouter()


@router.post(
    "/login", response_model=GenericResponse[AdminLoginResponse], tags=["admin_auth"]
)
async def admin_login(
    login_request: AdminLoginRequest,
    master_session: AsyncSession = Depends(get_master_async_session),
):
    """
    Super Admin login endpoint.
    Verifies credentials and returns admin details and session tokens.
    """
    try:
        super_admin = await admin_auth_service.get_super_admin_by_email(
            master_session, 
            login_request.email
        )
        if not super_admin:
            logger.warning(f"Super admin not found for email: {login_request.email}")
            return Response.unauthorized_error_response(AdminAuthErrors.USER_NOT_FOUND)

        try:
            supabase_response = supabase_admin_client.sign_in_with_password(
                email=login_request.email, 
                password=login_request.password
            )
        except Exception:
            logger.warning(
                f"Invalid credentials for super admin: {login_request.email}"
            )
            return Response.unauthorized_error_response(AdminAuthErrors.INVALID_CREDENTIALS)

        session_data = supabase_response.get("session", {})
        if not session_data or not session_data.get("access_token"):
            logger.warning(
                f"Supabase login failed for super admin: {login_request.email}"
            )
            return Response.unauthorized_error_response(AdminAuthErrors.INVALID_CREDENTIALS)

        user_data = supabase_response.get("user", {})
        user_metadata = user_data.get("user_metadata", {})
        requires_password_change = user_metadata.get("requires_password_change", False)

        if requires_password_change:
            return admin_auth_service.handle_pending_setup_login(super_admin.email)

        tokens = {
            "access_token": session_data.get("access_token"),
            "refresh_token": session_data.get("refresh_token"),
        }
        email_verified = user_metadata.get("email_verified", False)

        return await admin_auth_service.handle_active_user_login(
            master_session, super_admin, tokens, email_verified
        )

    except Exception as e:
        logger.error(
            "Error during super admin login for email %s: %s",
            login_request.email,
            str(e),
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message=AdminAuthErrors.LOGIN_FAILED
        )


@router.post("/reset-password", tags=["admin_auth"])
async def reset_password(
    password_request: AdminResetPasswordRequest,
    master_session: AsyncSession = Depends(get_master_async_session),
):
    """
    First-time or forgot-password reset endpoint for Super Admin.
    Requires a Supabase recovery token.
    """
    try:
        if not all([password_request.token_hash, password_request.new_password]):
            return Response.bad_request_error_response(
                AdminAuthErrors.RECOVERY_TOKEN_REQUIRED
            )

        if (
            password_request.confirm_password
            and password_request.new_password != password_request.confirm_password
        ):
            return Response.bad_request_error_response(
                AdminAuthErrors.PASSWORD_MISMATCH
            )

        try:
            user_id = supabase_admin_client.reset_password_with_token(
                token_hash=password_request.token_hash,
                new_password=password_request.new_password,
            )
        except Exception as e:
            logger.error(f"Failed to reset password with recovery token: {str(e)}")
            return Response.bad_request_error_response(
                AdminAuthErrors.INVALID_RECOVERY_TOKEN
            )

        if password_request.action == PasswordResetAction.FORCE_CHANGE:
            statement = select(SuperAdmin).where(
                SuperAdmin.supabase_user_id == user_id
            )
            result = await master_session.execute(statement)
            current_admin = result.scalars().first()

            if not current_admin:
                logger.warning(f"Admin not found for user_id: {user_id}")
                return Response.unauthorized_error_response(AdminAuthErrors.UNAUTHORIZED)

            # Update Supabase metadata and local database
            updated_metadata = {
                "requires_password_change": False,
                "email_verified": True,
                "first_name": current_admin.first_name,
                "last_name": current_admin.last_name,
                "role": current_admin.role,
            }
            supabase_admin_client.update_user_metadata(
                user_id=current_admin.supabase_user_id, metadata=updated_metadata
            )

            current_admin.status = AdminStatus.ACTIVE
            current_admin.email_verified = True
            current_admin.last_login_at = datetime.now(timezone.utc)
            master_session.add(current_admin)
            await master_session.commit()

        return Response.success_response(
            message="Password changed successfully. You can now log in."
        )

    except Exception as e:
        logger.error("Error during password reset: %s", str(e), exc_info=True)
        return Response.internal_server_error_response(
            AdminAuthErrors.PASSWORD_RESET_FAILED
        )


@router.post(
    "/refresh-token",
    response_model=GenericResponse[AdminRefreshTokenResponse],
    tags=["admin_auth"],
)
async def refresh_token(refresh_request: AdminRefreshTokenRequest):
    """Refresh the access token using a refresh token."""
    try:
        session_data = supabase_admin_client.refresh_access_token(
            refresh_request.refresh_token
        )

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
            AdminAuthErrors.REFRESH_TOKEN_INVALID
        )
    except SupabaseAuthError as e:
        logger.error(f"Token refresh failed: {e.message}")
        # Use the status code and message from the custom exception
        if e.status_code == 400:
            return Response.bad_request_error_response(e.message)
        elif e.status_code == 401:
            return Response.unauthorized_error_response(e.message)
        elif e.status_code == 404:
            return Response.not_found_error_response(e.message)
        elif e.status_code == 409:
            return Response.conflict_error_response(e.message)
        else:
            return Response.internal_server_error_response(message=e.message)
    except Exception as e:
        logger.error("Error during token refresh: %s", str(e), exc_info=True)
        return Response.internal_server_error_response(
            AdminAuthErrors.REFRESH_TOKEN_FAILED
        )


@router.post("/forgot-password", tags=["admin_auth"])
async def forgot_password(
    forgot_password_request: AdminForgotPasswordRequest,
    master_session: AsyncSession = Depends(get_master_async_session),
):
    """Triggers a password reset email for a super admin."""
    try:
        super_admin = await admin_auth_service.get_super_admin_by_email(
            master_session, forgot_password_request.email
        )

        if not super_admin:
            logger.warning(
                "Password reset requested for non-existent user: %s",
                forgot_password_request.email,
            )
            # Return a generic success response to avoid user enumeration
            return Response.success_response(
                message="If an account with this email exists, a password reset link has been sent."
            )

        password_reset_link = supabase_admin_client.generate_password_reset_link(
            email=super_admin.email
        )
        
        email_service.send_password_reset_email(
            to_email=super_admin.email,
            first_name=super_admin.first_name,
            reset_url=password_reset_link,
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
            AdminAuthErrors.FORGOT_PASSWORD_FAILED
        )


@router.post("/logout", tags=["admin_auth"])
async def admin_logout(
    logout_request: AdminLogoutRequest,
    current_admin: SuperAdmin = Depends(admin_auth),
    master_session: AsyncSession = Depends(get_master_async_session),
):
    """Unified logout endpoint for super admin."""
    try:
        user_to_logout = current_admin
        if logout_request.global_logout:
            if not logout_request.email:
                return Response.bad_request_error_response(
                    AdminAuthErrors.EMAIL_REQUIRED_FOR_LOGOUT
                )
            user_to_logout = await admin_auth_service.get_super_admin_by_email(
                master_session, logout_request.email
            )
            if not user_to_logout:
                return Response.not_found_error_response(AdminAuthErrors.USER_NOT_FOUND)

        supabase_admin_client.sign_out_user(
            user_id=user_to_logout.supabase_user_id,
            global_signout=logout_request.global_logout,
        )

        message = (
            f"Successfully logged out user {user_to_logout.email} from all sessions."
            if logout_request.global_logout
            else "Successfully logged out."
        )
        return Response.success_response(message=message)

    except Exception as e:
        logger.error("Error during logout: %s", str(e), exc_info=True)
        return Response.internal_server_error_response(AdminAuthErrors.LOGOUT_FAILED)


@router.post("/change-password", tags=["admin_auth"])
async def change_password(
    password_request: AdminChangePasswordRequest,
    current_admin: SuperAdmin = Depends(admin_auth),
):
    """Change password for an active, logged-in Super Admin."""
    try:
        # Verify the current password first
        try:
            supabase_admin_client.sign_in_with_password(
                email=current_admin.email,
                password=password_request.current_password,
            )
        except Exception:
            logger.warning(
                "Incorrect current password for user: %s", current_admin.email
            )
            return Response.unauthorized_error_response(
                AdminAuthErrors.INCORRECT_CURRENT_PASSWORD
            )

        # Update the password
        supabase_admin_client.update_user_password(
            user_id=current_admin.supabase_user_id,
            new_password=password_request.new_password,
        )

        logger.info("Password changed successfully for user: %s", current_admin.email)
        return Response.success_response(message="Password changed successfully.")

    except Exception as e:
        logger.error(
            "Error during password change for email %s: %s",
            current_admin.email,
            str(e),
            exc_info=True,
        )
        return Response.internal_server_error_response(
            AdminAuthErrors.PASSWORD_CHANGE_FAILED
        )
