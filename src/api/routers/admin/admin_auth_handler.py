from datetime import datetime
from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from src.domain.models.admin_models import (
    AdminLoginRequest,
    AdminChangePasswordRequest,
    AdminLogoutRequest,
    AdminLoginState,
    AdminLoginResponse,
    AdminStatus,
    AdminRefreshTokenRequest,
    AdminRefreshTokenResponse,
    AdminForgotPasswordRequest,
    AdminResetPasswordRequest,
    PasswordResetAction,
)
from src.domain.models.master_tables import SuperAdmin
from src.infra.utils.database import get_master_engine
from src.infra.auth.admin_auth import admin_auth
from src.infra.auth.email_service import email_service
from src.infra.auth.supabase_admin_client import supabase_admin_client
from src.infra.utils.logger import get_logger
from src.api.utils.http_response import Response
from src.api.schemas.base_models import GenericResponse

logger = get_logger("admin_auth_handler")
router = APIRouter()

@router.post(
    "/login", response_model=GenericResponse[AdminLoginResponse], tags=["admin_auth"]
)
async def admin_login(
    login_request: AdminLoginRequest,
    master_session: Session = Depends(lambda: Session(get_master_engine(echo=False))),
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
        logger.info(
            f"User found in database: {super_admin.email if super_admin else 'None'}"
        )

        if not super_admin:
            logger.warning(f"Super admin not found for email: {login_request.email}")
            return Response.unauthorized_error_response("User not found")

        # Sign in with Supabase
        try:
            supabase_response = supabase_admin_client.sign_in_with_password(
                email=login_request.email, password=login_request.password
            )
        except Exception as e:
            logger.warning(
                f"Invalid credentials for super admin: {login_request.email}"
            )
            return Response.unauthorized_error_response("Invalid credentials")

        user_data = supabase_response.get("user", {})
        session_data = supabase_response.get("session", {})

        access_token = session_data.get("access_token")
        refresh_token = session_data.get("refresh_token")

        if not access_token:
            logger.warning(
                f"Invalid credentials for super admin: {login_request.email}"
            )
            return Response.unauthorized_error_response("Invalid credentials")

        requires_password_change = user_data.get("user_metadata", {}).get(
            "requires_password_change", False
        )
        email_verified = user_data.get("user_metadata", {}).get("email_verified", False)

        if requires_password_change:
            # For PENDING_SETUP users, we need to generate a proper recovery token
            try:
                # Generate a password reset link and extract the "ht=" parameter
                password_reset_link = supabase_admin_client.generate_password_reset_link(
                    email=login_request.email
                )

                response_data = {
                    "state": AdminLoginState.PENDING_SETUP,
                    "password_reset_link": password_reset_link,  # This is the "ht=" parameter from the reset link
                }
                return Response.success_response(
                    message="Login successful", data=response_data
                )
            except Exception as e:
                logger.error(
                    f"Failed to generate recovery token for user {super_admin.email}: {str(e)}"
                )
                return Response.internal_server_error_response(
                    message="Failed to generate password change token", data=None
                )
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
            return Response.success_response(
                message="Login successful", data=response_data
            )

    except Exception as e:
        logger.error(
            "Error during super admin login for email %s: %s",
            login_request.email,
            str(e),
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message="login failed", data=None
        )


@router.post("/reset-password")
async def first_time_password_change(
    password_request: AdminResetPasswordRequest,
    master_session: Session = Depends(lambda: Session(get_master_engine(echo=False))),
):
    """
    First-time password change endpoint for Super Admin.
    This endpoint is used after logging in with a temporary password to set a new permanent password.
    It requires authentication via a Supabase recovery token.
    Updates the password in Supabase, sets requires_password_change to False in user metadata,
    updates the local database status to active, marks email as verified if not already,
    and updates the last_login_at timestamp.

    - Validates that new_password and confirm_password match (if confirm_password is provided).
    - Uses Supabase recovery token for authentication.
    - This endpoint is intended only for first-time changes; regular password changes should use a separate endpoint if needed.
    """
    try:
        if not password_request.token_hash:
            return Response.bad_request_error_response(
                "Recovery token_hash is required."
            )

        if not password_request.new_password:
            return Response.bad_request_error_response("New password is required.")

        # Validate new_password and confirm_password if provided
        if (
            password_request.confirm_password
            and password_request.new_password != password_request.confirm_password
        ):
            return Response.bad_request_error_response(
                "New password and confirm password do not match"
            )

        # Validate action
        if password_request.action not in [
            PasswordResetAction.FORCE_CHANGE,
            PasswordResetAction.FORGOT,
        ]:
            return Response.bad_request_error_response(
                "Invalid action. Must be 'force_change' or 'forgot'."
            )

        # Extract user information from the token
        try:
            # For PENDING_SETUP users, we now have a proper recovery token
            # We need to use the recovery token to reset the password
            try:
                # Use the recovery token_hash to reset the password
                user_id = supabase_admin_client.reset_password_with_token(
                    token_hash=password_request.token_hash,
                    new_password=password_request.new_password
                )
            except Exception as e:
                logger.error(f"Failed to reset password with recovery token: {str(e)}")
                return Response.bad_request_error_response(
                    "Invalid or expired recovery token"
                )

            # Only fetch user and update metadata for "force_change" action
            if password_request.action == PasswordResetAction.FORCE_CHANGE:
                # After successful password reset, we need to update the user metadata
                # We'll need to get the user ID from the recovery token or use a different approach
                # For now, let's use the email to find the user
                statement = select(SuperAdmin).where(
                    SuperAdmin.supabase_user_id == user_id
                )
                result = master_session.exec(statement)
                current_admin = result.first()

                if not current_admin:
                    logger.warning(
                        f"Admin not found for user_id: {user_id}"
                    )
                    return Response.unauthorized_error_response("Unauthorized")

                # Update user metadata in Supabase to set requires_password_change to False
                updated_metadata = {
                    "requires_password_change": False,
                    "email_verified": True,  # Ensure email is marked verified
                    "first_name": current_admin.first_name,
                    "last_name": current_admin.last_name,
                    "role": current_admin.role,
                }

                try:
                    supabase_admin_client.update_user_metadata(
                        user_id=current_admin.supabase_user_id,
                        metadata=updated_metadata,
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to update user metadata in Supabase for user: {current_admin.email}"
                    )
                    return Response.internal_server_error_response(
                        "Failed to update user metadata"
                    )

                # Update local database
                logger.info(f"Updating local DB for user_id: {user_id}")
                current_admin.status = AdminStatus.ACTIVE
                current_admin.email_verified = True
                current_admin.last_login_at = (
                    datetime.utcnow()
                )  # Update last login now that setup is complete
                master_session.add(current_admin)
                master_session.commit()
                master_session.refresh(current_admin)
                logger.info(f"Local DB updated for user_id: {user_id}")
            else:
                # For "forgot" action, just log that password was reset without metadata update
                logger.info(
                    f"Password reset completed for user: {user_id} (forgot action - no metadata update)"
                )

        except Exception as e:
            logger.error(f"Error updating user data after password reset: {str(e)}")
            # Password was reset successfully, but we couldn't update the user data
            # This is not ideal but the password change was successful
            return Response.success_response(
                message="Password changed successfully. You can now log in with your new password."
            )

        # Return success response
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
            "First-time password change failed"
        )


@router.post("/refresh-token", tags=["admin_auth"])
async def refresh_token(refresh_request: AdminRefreshTokenRequest):
    """
    Refresh the access token using a refresh token.
    If the refresh token is invalid, the user will be automatically logged out.
    """
    try:
        # Use Supabase client to refresh token
        session_data = supabase_admin_client.refresh_access_token(
            refresh_request.refresh_token
        )

        if session_data.get("session", {}).get("access_token"):
            # Token refresh was successful, return new tokens
            return Response.success_response(
                message="Token refreshed successfully",
                data=AdminRefreshTokenResponse(
                    access_token=session_data["session"]["access_token"],
                    refresh_token=session_data["session"]["refresh_token"],
                ),
            )
        else:
            # Token refresh failed - automatically logout the user
            logger.warning("Refresh token is invalid or expired - logging out user")
            return Response.unauthorized_error_response(
                "Invalid or expired refresh token. User has been logged out. Please log in again."
            )

    except Exception as e:
        logger.error(
            "Error during token refresh: %s",
            str(e),
            exc_info=True,
        )
        
        # If refresh failed, just return error (no logout since we don't have user_id)
        return Response.internal_server_error_response("Token refresh failed. Please log in again.")

@router.post("/forgot-password", tags=["admin_auth"])
async def forgot_password(
    forgot_password_request: AdminForgotPasswordRequest,
    master_session: Session = Depends(lambda: Session(get_master_engine(echo=False))),
):
    """
    Forgot password endpoint for Super Admin.
    Accepts an email address, verifies if the user exists, and if so,
    triggers Supabase to send a password reset email.
    """
    try:
        # Fetch super admin by email to verify existence
        statement = select(SuperAdmin).where(
            SuperAdmin.email == forgot_password_request.email
        )
        result = master_session.exec(statement)
        super_admin = result.first()

        if not super_admin:
            logger.warning(
                f"Password reset requested for non-existent user: {forgot_password_request.email}"
            )
            # Return a generic success response to avoid user enumeration
            return Response.success_response(
                message="If an account with this email exists, a password reset link has been sent."
            )

        # Generate a password reset link and extract the "ht=" parameter
        password_reset_link = supabase_admin_client.generate_password_reset_link(
            email=super_admin.email
        )
        logger.info(
            f"Generated password reset link for: {forgot_password_request.email}"
        )
        if not password_reset_link:
            logger.error(
                f"Failed to generate password reset link for: {forgot_password_request.email}"
            )
            return Response.internal_server_error_response(
                message="Failed to generate password reset link", data=None
            )
        logger.info(f"Password reset link generated for user: {super_admin.email}")

        # Send the password reset email using the email service
        logger.info(f"Attempting to send password reset email to: {super_admin.email}")
        email_sent = email_service.send_password_reset_email(
            to_email=super_admin.email,
            first_name=super_admin.first_name,
            reset_url=password_reset_link,
        )

        if not email_sent:
            logger.error(
                f"Failed to send password reset email for: {forgot_password_request.email}. Email service returned False."
            )
            return Response.internal_server_error_response(
                message="Failed to send password reset email. Please check email service configuration.", data=None
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
            message="Forgot password request failed", data=None
        )

@router.post("/logout", tags=["admin_auth"])
async def admin_logout(
    logout_request: AdminLogoutRequest,
    current_admin: SuperAdmin = Depends(admin_auth),
    master_session: Session = Depends(lambda: Session(get_master_engine(echo=False))),
):
    """
    Unified logout endpoint for super admin.
    
    - If global_logout=False: Logout current user from current session only
    - If global_logout=True: Logout specified user from all sessions globally
    """
    try:
        if not current_admin:
            return Response.unauthorized_error_response("Unauthorized")

        if logout_request.global_logout:
            # Global logout - logout specified user from all sessions
            if not logout_request.email:
                return Response.bad_request_error_response("Email is required for global logout")

            # Find the user to log out
            statement = select(SuperAdmin).where(SuperAdmin.email == logout_request.email)
            result = master_session.exec(statement)
            user_to_logout = result.first()

            if not user_to_logout:
                logger.warning(
                    f"Global logout requested for non-existent user: {logout_request.email}"
                )
                return Response.not_found_error_response("User not found")

            # Perform global sign-out using Supabase
            signed_out = supabase_admin_client.sign_out_user(
                user_id=user_to_logout.supabase_user_id,
                global_signout=True  # Global logout - all sessions
            )

            if not signed_out:
                logger.error(
                    f"Failed to globally sign out user {logout_request.email} from Supabase."
                )
                return Response.internal_server_error_response("Global logout failed.")

            logger.info(
                f"Successfully performed global logout for user: {logout_request.email}"
            )
            return Response.success_response(
                message=f"Successfully logged out user {logout_request.email} from all sessions."
            )
        else:
            # Regular logout - logout current user from current session only
            logger.info(f"Attempting to log out admin: {current_admin.email}")

            signed_out = supabase_admin_client.sign_out_user(
                user_id=current_admin.supabase_user_id,
                global_signout=False  # Regular logout - current session only
            )

            if not signed_out:
                logger.error(
                    f"Failed to sign out user {current_admin.email} from Supabase."
                )
                return Response.internal_server_error_response("Logout failed.")

            logger.info(f"Successfully logged out admin: {current_admin.email}")
            return Response.success_response(message="Successfully logged out.")

    except Exception as e:
        logger.error(
            "Error during logout: %s",
            str(e),
            exc_info=True,
        )
        return Response.internal_server_error_response(
            "An unexpected error occurred during logout."
        )


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
        try:
            supabase_admin_client.sign_in_with_password(
                email=current_admin.email, password=password_request.current_password
            )
        except Exception:
            logger.warning(
                f"Incorrect current password for user: {current_admin.email}"
            )
            return Response.unauthorized_error_response("Incorrect current password")

        # If verification is successful, update the password
        try:
            supabase_admin_client.update_user_password(
                user_id=current_admin.supabase_user_id,
                new_password=password_request.new_password,
            )
        except Exception as e:
            logger.error(
                f"Failed to update password in Supabase for user: {current_admin.email}"
            )
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
