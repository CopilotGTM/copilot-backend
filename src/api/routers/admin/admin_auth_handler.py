from typing import Optional
from datetime import datetime, timedelta
from uuid import UUID
import logging
import jwt
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlmodel import Session, select

from src.domain.models.admin_models import (
    AdminLoginRequest,
    AdminFirstTimePasswordChangeRequest,
    AdminChangePasswordRequest,
    AdminGlobalLogoutRequest,
    AdminLoginState,
    AdminLoginResponse,
    AdminStatus,
    AdminRefreshTokenRequest,
    AdminRefreshTokenResponse,
    AdminForgotPasswordRequest,
    AdminResetPasswordRequest,
)
from src.domain.models.master_db_models import SuperAdmin
from src.infra.utils.database import get_master_session
from src.infra.auth.admin_auth import admin_auth, admin_auth_allow_pending
from src.infra.auth.email_service import email_service
from src.infra.auth.supabase_auth import supabase_auth
from src.infra.config.configs import supabase_config
from src.infra.utils.logger import get_logger
from src.api.utils.http_response import Response
from src.api.schemas.base_models import GenericResponse


logger = get_logger("admin_auth_handler")
router = APIRouter()


@router.post("/login", response_model=GenericResponse[AdminLoginResponse], tags=["admin_auth"])
async def admin_login(
    login_request: AdminLoginRequest,
    master_session: Session = Depends(get_master_session),
):
    """
    Super Admin login endpoint.
    Accepts email and password, verifies credentials, and returns super admin details,
    Supabase session token (access_token), and requires_password_change flag.
    The access_token must be used for authenticated requests such as first-time password change.

    Handles two states:
    - First stage: Login with temporary password (status == "pending_setup").
      In this case, requires_password_change is True, forcing the user to change password.
      Last login timestamp is NOT updated, as this is a temporary login for password reset purposes.
    - Second stage: Login after first-time password change (status != "pending_setup").
      In this case, requires_password_change is False, and last login timestamp is updated.
    """
    try:
        # Fetch super admin by email
        statement = select(SuperAdmin).where(SuperAdmin.email == login_request.email)
        result = master_session.exec(statement)
        super_admin = result.first()
        logger.info(f"User found in database: {super_admin.email if super_admin else 'None'}")

        if not super_admin:
            logger.warning(f"Super admin not found for email: {login_request.email}")
            return Response.unauthorized_error_response("User not found")

        # Verify password using Supabase Auth
        supabase_response = supabase_auth.verify_email_password(
            email=login_request.email, password=login_request.password
        )

        logger.info(f"Supabase response: {supabase_response}")
        access_token = supabase_response.get("access_token")
        refresh_token = supabase_response.get("refresh_token")

        if not access_token:
            logger.warning(f"Invalid credentials for super admin: {login_request.email}")
            return Response.unauthorized_error_response("Invalid credentials")

        requires_password_change = supabase_response["user"]["user_metadata"]["requires_password_change"]
        email_verified = supabase_response["user"]["user_metadata"]["email_verified"]
        if requires_password_change:
            # session token only for first time users
            payload = {
                "sub": supabase_response["user"]["id"],
                "exp": datetime.utcnow() + timedelta(minutes=10),
                "iat": datetime.utcnow(),
                "scope": "password_change",
            }
            session_token = jwt.encode(payload, supabase_config.SUPABASE_JWT_SECRET, algorithm="HS256")
            response_data = {
                "email": super_admin.email,
                "first_name": super_admin.first_name,
                "last_name": super_admin.last_name,
                "role": super_admin.role,
                "state": AdminLoginState.PENDING_SETUP,
                "created_at": super_admin.created_at,
                "email_verified": email_verified,
                "tokens": {
                    "access_token": "",
                    "refresh_token": "",
                },
                "session_token": session_token,
            }
            return Response.success_response(message="Login successful", data=response_data)
        else:
            logger.info("Updating last_login_at for super_admin: %s", super_admin.email)
            super_admin.last_login_at = datetime.utcnow()
            master_session.add(super_admin)
            master_session.commit()
            master_session.refresh(super_admin)
            logger.info(f"Updated last_login_at for super_admin: {super_admin.email}")
            response_data = {
                "email": super_admin.email,
                "first_name": super_admin.first_name,
                "last_name": super_admin.last_name,
                "role": super_admin.role,
                "state": AdminLoginState.ACTIVE,
                "created_at": super_admin.created_at,
                "email_verified": email_verified,
                "last_login_at": super_admin.last_login_at,
                "tokens": {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                },
            }
            return Response.success_response(message="Login successful", data=response_data)

    except Exception as e:
        logger.error(
            "Error during super admin login for email %s: %s",
            login_request.email,
            str(e),
            exc_info=True,
        )
        return Response.internal_server_error_response(message="login failed", data=None)


@router.post("/force-change-password")
async def first_time_password_change(
    password_request: AdminFirstTimePasswordChangeRequest,
    master_session: Session = Depends(get_master_session),
):
    """
    First-time password change endpoint for Super Admin.
    This endpoint is used after logging in with a temporary password to set a new permanent password.
    It requires authentication via a short-lived session_token.
    Updates the password in Supabase, sets requires_password_change to False in user metadata,
    updates the local database status to active, marks email as verified if not already,
    and updates the last_login_at timestamp.

    - Validates that new_password and confirm_password match (if confirm_password is provided).
    - Checks if the user is in a state requiring password change (based on Supabase metadata).
    - This endpoint is intended only for first-time changes; regular password changes should use a separate endpoint if needed.
    """
    try:
        if not password_request.session_token:
            return Response.bad_request_error_response("Session token is required.")
        try:
            payload = jwt.decode(
                password_request.session_token,
                supabase_config.SUPABASE_JWT_SECRET,
                algorithms=["HS256"],
            )
            token_scope = payload.get("scope")
            supabase_user_id = payload.get("sub")
            if token_scope != "password_change":
                return Response.unauthorized_error_response("Invalid token scope.")
            if not supabase_user_id:
                return Response.unauthorized_error_response("Invalid token: missing user identifier.")
        except jwt.ExpiredSignatureError:
            return Response.unauthorized_error_response("Session token has expired.")
        except jwt.PyJWTError as e:
            logger.error(f"Invalid session token: {e}")
            return Response.unauthorized_error_response("Invalid session token.")

        # Fetch the admin from the local database using the supabase_user_id from the token
        statement = select(SuperAdmin).where(SuperAdmin.supabase_user_id == supabase_user_id)
        result = master_session.exec(statement)
        current_admin = result.first()

        if not current_admin:
            logger.warning(f"Admin not found for supabase_user_id: {supabase_user_id}")
            return Response.unauthorized_error_response("Unauthorized")

        # Fetch the user from Supabase to check current metadata
        supabase_user = supabase_auth.get_user_by_id(current_admin.supabase_user_id)
        if not supabase_user:
            logger.warning(f"Supabase user not found for ID: {current_admin.supabase_user_id}")
            return Response.not_found_error_response("User not found")

        requires_password_change = supabase_user.get("user_metadata", {}).get("requires_password_change", False)
        if not requires_password_change:
            logger.warning(f"Password change not required for user: {current_admin.email}")
            return Response.bad_request_error_response(
                "Password change is not required for this user. Use regular password change if needed."
            )

        # Validate new_password and confirm_password if provided
        if password_request.confirm_password and password_request.new_password != password_request.confirm_password:
            logger.warning(f"Password mismatch for user: {current_admin.email}")
            return Response.bad_request_error_response("New password and confirm password do not match")

        # Update password in Supabase
        logger.info(f"Updating password in Supabase for user: {current_admin.email}")
        password_updated = supabase_auth.update_user_password(
            user_id=current_admin.supabase_user_id, new_password=password_request.new_password
        )

        if not password_updated:
            logger.error(f"Failed to update password in Supabase for user: {current_admin.email}")
            return Response.internal_server_error_response("Failed to update password")

        # Update user metadata in Supabase to set requires_password_change to False
        updated_metadata = {
            "requires_password_change": False,
            "email_verified": True,  # Ensure email is marked verified
            "first_name": current_admin.first_name,
            "last_name": current_admin.last_name,
            "role": current_admin.role,
        }
        logger.info(f"Updating user metadata in Supabase for user: {current_admin.email}")
        metadata_updated = admin_auth.update_supabase_user_metadata(
            supabase_user_id=current_admin.supabase_user_id, metadata=updated_metadata
        )

        if not metadata_updated:
            logger.error(f"Failed to update user metadata in Supabase for user: {current_admin.email}")
            # Note: Password was already changed. This could leave the user in an inconsistent state.
            # Consider how to handle this scenario, e.g., by logging a critical error for manual intervention.
            return Response.internal_server_error_response("Failed to update user metadata")

        # Update local database
        logger.info(f"Updating local DB for user: {current_admin.email}")
        current_admin.status = AdminStatus.ACTIVE
        current_admin.email_verified = True
        current_admin.last_login_at = datetime.utcnow()  # Update last login now that setup is complete
        master_session.add(current_admin)
        master_session.commit()
        master_session.refresh(current_admin)
        logger.info(f"Local DB updated for user: {current_admin.email}")

        # Return success response
        return Response.success_response(
            message="Password changed successfully. You can now log in with your new password."
        )

    except Exception as e:
        logger.error(
            "Error during first-time password change for email %s: %s",
            current_admin.email if "current_admin" in locals() and current_admin else "unknown",
            str(e),
            exc_info=True,
        )
        return Response.internal_server_error_response("First-time password change failed")


@router.post("/refresh-token", tags=["admin_auth"])
async def refresh_token(refresh_request: AdminRefreshTokenRequest):
    """
    Refresh the access token using a refresh token.
    """
    try:
        session_data = supabase_auth.refresh_access_token(refresh_request.refresh_token)

        if session_data.get("access_token"):
            # Token refresh was successful, return new tokens
            return Response.success_response(
                message="Token refreshed successfully",
                data=AdminRefreshTokenResponse(
                    access_token=session_data["access_token"],
                    refresh_token=session_data["refresh_token"],
                ),
            )
        else:
            # Token refresh failed, indicating a potentially compromised refresh token.
            # As a security measure, we will attempt to sign the user out globally.
            logger.warning(
                "Refresh token is invalid or expired. Attempting to sign out user globally as a security precaution."
            )
            try:
                # Decode the invalid token to get the user ID (sub) without verifying the signature
                payload = jwt.decode(refresh_request.refresh_token, options={"verify_signature": False})
                user_id = payload.get("sub")
                if user_id:
                    supabase_auth.sign_out_user_globally(user_id)
                    logger.info(f"Successfully triggered global sign-out for user {user_id}")
            except jwt.PyJWTError as jwt_error:
                logger.error(f"Could not decode the invalid refresh token: {jwt_error}")

            # Return a 401 Unauthorized error to force the user to re-authenticate
            return Response.unauthorized_error_response(
                "Invalid or expired refresh token. Please log in again."
            )

    except Exception as e:
        logger.error(
            "Error during token refresh: %s",
            str(e),
            exc_info=True,
        )
        return Response.internal_server_error_response("Token refresh failed")

@router.post("/forgot-password", tags=["admin_auth"])
async def forgot_password(
    forgot_password_request: AdminForgotPasswordRequest,
    master_session: Session = Depends(get_master_session),
):
    """
    Forgot password endpoint for Super Admin.
    Accepts an email address, verifies if the user exists, and if so,
    triggers Supabase to send a password reset email.
    """
    try:
        # Fetch super admin by email to verify existence
        statement = select(SuperAdmin).where(SuperAdmin.email == forgot_password_request.email)
        result = master_session.exec(statement)
        super_admin = result.first()

        if not super_admin:
            logger.warning(f"Password reset requested for non-existent user: {forgot_password_request.email}")
            # Return a generic success response to avoid user enumeration
            return Response.success_response(
                message="If an account with this email exists, a password reset link has been sent."
            )

        # Generate a password reset link
        reset_link = supabase_auth.generate_password_reset_link(
            user_id=super_admin.supabase_user_id,
            email=super_admin.email,
            redirect_to=forgot_password_request.redirect_to,
        )
        logger.info(f"Generated password reset link for: {forgot_password_request.email}")
        if not reset_link:
            logger.error(f"Failed to generate password reset link for: {forgot_password_request.email}")
            return Response.internal_server_error_response(
                message="Failed to generate password reset link", data=None
            )
        logger.info(f"Password reset link generated for user: {super_admin.email}")

        # Send the password reset email using the email service
        email_sent = email_service.send_password_reset_email(
            to_email=super_admin.email,
            first_name=super_admin.first_name,
            reset_url=reset_link,
        )

        if not email_sent:
            logger.error(f"Failed to send password reset email for: {forgot_password_request.email}")
            return Response.internal_server_error_response(message="Failed to send password reset email", data=None)

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
        return Response.internal_server_error_response(message="Forgot password request failed", data=None)

@router.post("/reset-password", tags=["admin_auth"])
async def reset_password(
    reset_password_request: AdminResetPasswordRequest,
):
    """
    Reset password endpoint for Super Admin.
    Accepts a recovery token and a new password, and updates the user's password in Supabase.
    """
    try:
        password_reset = supabase_auth.reset_password_with_token(
            token=reset_password_request.token,
            new_password=reset_password_request.new_password,
        )

        if not password_reset:
            return Response.bad_request_error_response("Invalid or expired token.")

        return Response.success_response(message="Password has been reset successfully.")

    except Exception as e:
        logger.error(
            "Error during password reset: %s",
            str(e),
            exc_info=True,
        )
        return Response.internal_server_error_response(message="Password reset failed", data=None)

@router.post("/logout", tags=["admin_auth"])
async def admin_logout(current_admin: SuperAdmin = Depends(admin_auth)):
    """
    Logs out the currently authenticated super admin.
    This will invalidate all sessions for the user.
    """
    try:
        logger.info(f"Attempting to log out admin: {current_admin.email}")
        
        # Invalidate all user sessions
        signed_out = supabase_auth.sign_out_user_globally(user_id=current_admin.supabase_user_id)

        if not signed_out:
            logger.error(f"Failed to sign out user {current_admin.email} from Supabase.")
            return Response.internal_server_error_response("Logout failed.")

        logger.info(f"Successfully logged out admin: {current_admin.email}")
        return Response.success_response(message="Successfully logged out.")

    except Exception as e:
        logger.error(
            "Error during admin logout for email %s: %s",
            current_admin.email if current_admin else "unknown",
            str(e),
            exc_info=True,
        )
        return Response.internal_server_error_response("An unexpected error occurred during logout.")

@router.post("/change-password", tags=["admin_auth"])
async def change_password(
    password_request: AdminChangePasswordRequest,
    current_admin: SuperAdmin = Depends(admin_auth),
):
    """
    Change password for an active, logged-in Super Admin.
    Requires the current password for verification.
    """
    try:
        if not current_admin:
            return Response.unauthorized_error_response("Unauthorized")

        # Verify the current password first
        verification_response = supabase_auth.verify_email_password(
            email=current_admin.email, password=password_request.current_password
        )

        if not verification_response.get("access_token"):
            logger.warning(f"Incorrect current password for user: {current_admin.email}")
            return Response.unauthorized_error_response("Incorrect current password")

        # If verification is successful, update the password
        password_updated = supabase_auth.update_user_password(
            user_id=current_admin.supabase_user_id, new_password=password_request.new_password
        )

        if not password_updated:
            logger.error(f"Failed to update password in Supabase for user: {current_admin.email}")
            return Response.internal_server_error_response("Failed to update password")

        logger.info(f"Password changed successfully for user: {current_admin.email}")
        return Response.success_response(message="Password changed successfully.")

    except Exception as e:
        logger.error(
            "Error during password change for email %s: %s",
            current_admin.email if current_admin else "unknown",
            str(e),
            exc_info=True,
        )
        return Response.internal_server_error_response("Password change failed")

@router.post("/global-logout", tags=["admin_auth"])
async def global_logout(
    logout_request: AdminGlobalLogoutRequest,
    current_admin: SuperAdmin = Depends(admin_auth),
    master_session: Session = Depends(get_master_session),
):
    """
    Allows a super admin to globally log out any user by email.
    This invalidates all active sessions for the specified user.
    """
    try:
        if not current_admin:
            return Response.unauthorized_error_response("Unauthorized")

        # Find the user to log out
        statement = select(SuperAdmin).where(SuperAdmin.email == logout_request.email)
        result = master_session.exec(statement)
        user_to_logout = result.first()

        if not user_to_logout:
            logger.warning(f"Global logout requested for non-existent user: {logout_request.email}")
            return Response.not_found_error_response("User not found")

        # Perform global sign-out
        signed_out = supabase_auth.sign_out_user_globally(user_id=user_to_logout.supabase_user_id)

        if not signed_out:
            logger.error(f"Failed to globally sign out user {logout_request.email} from Supabase.")
            return Response.internal_server_error_response("Global logout failed.")

        logger.info(f"Successfully performed global logout for user: {logout_request.email}")
        return Response.success_response(
            message=f"Successfully logged out user {logout_request.email} from all sessions."
        )

    except Exception as e:
        logger.error(
            "Error during global logout for email %s: %s",
            logout_request.email,
            str(e),
            exc_info=True,
        )
        return Response.internal_server_error_response("An unexpected error occurred during global logout.")
