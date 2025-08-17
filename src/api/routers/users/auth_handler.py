from datetime import datetime
from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from src.domain.models.user_models import (
    UserLoginRequest,
    UserLoginResponse,
    UserLoginState,
    UserResetPasswordRequest,
    UserRefreshTokenRequest,
    UserRefreshTokenResponse,
    UserForgotPasswordRequest,
)
from src.domain.models.admin_models import PasswordResetAction
from src.domain.models.master_tables import User
from src.infra.utils.database import get_master_engine
from src.infra.auth.supabase_user_client import supabase_user_client
from src.infra.utils.logger import get_logger
from src.api.utils.http_response import Response
from src.api.schemas.base_models import GenericResponse
from src.infra.auth.email_service import email_service

logger = get_logger("user_auth_handler")
router = APIRouter()


@router.post(
    "/login", response_model=GenericResponse[UserLoginResponse], tags=["user_auth"]
)
async def user_login(
    login_request: UserLoginRequest,
    master_session: Session = Depends(lambda: Session(get_master_engine(echo=False))),
):
    """
    User login endpoint.
    """
    try:
        statement = select(User).where(User.email == login_request.email)
        result = master_session.exec(statement)
        user = result.first()
        logger.info(f"User found in database: {user.email if user else 'None'}")

        if not user:
            logger.warning(f"User not found for email: {login_request.email}")
            return Response.unauthorized_error_response("User not found")

        try:
            supabase_response = supabase_user_client.sign_in_with_password(
                email=login_request.email, password=login_request.password
            )
        except Exception as e:
            logger.warning(f"Invalid credentials for user: {login_request.email}")
            return Response.unauthorized_error_response("Invalid credentials")

        user_data = supabase_response.get("user", {})
        session_data = supabase_response.get("session", {})

        access_token = session_data.get("access_token")
        refresh_token = session_data.get("refresh_token")

        if not access_token:
            logger.warning(f"Invalid credentials for user: {login_request.email}")
            return Response.unauthorized_error_response("Invalid credentials")

        requires_password_change = user_data.get("user_metadata", {}).get(
            "requires_password_change", False
        )
        email_verified = user_data.get("user_metadata", {}).get("email_verified", False)

        if requires_password_change:
            try:
                password_reset_link = supabase_user_client.generate_password_reset_link(
                    email=login_request.email
                )

                response_data = {
                    "state": UserLoginState.PENDING_SETUP,
                    "password_reset_link": password_reset_link,
                }
                return Response.success_response(
                    message="Login successful, password change required",
                    data=response_data,
                )
            except Exception as e:
                logger.error(
                    f"Failed to generate recovery token for user {user.email}: {str(e)}"
                )
                return Response.internal_server_error_response(
                    message="Failed to generate password change token", data=None
                )
        else:
            user.last_login_at = datetime.utcnow()
            master_session.add(user)
            master_session.commit()
            master_session.refresh(user)
            response_data = {
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "role": user.user_role,
                "state": UserLoginState.ACTIVE,
                "created_at": user.created_at,
                "email_verified": email_verified,
                "last_login_at": user.last_login_at,
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
            "Error during user login for email %s: %s",
            login_request.email,
            str(e),
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message="login failed", data=None
        )


@router.post("/reset-password", tags=["user_auth"])
async def reset_password(
    password_request: UserResetPasswordRequest,
    master_session: Session = Depends(lambda: Session(get_master_engine(echo=False))),
):
    try:
        if not password_request.token_hash:
            return Response.bad_request_error_response(
                "Recovery token_hash is required."
            )

        if not password_request.new_password:
            return Response.bad_request_error_response("New password is required.")

        if (
            password_request.confirm_password
            and password_request.new_password != password_request.confirm_password
        ):
            return Response.bad_request_error_response(
                "New password and confirm password do not match"
            )

        if password_request.action not in [
            PasswordResetAction.FORCE_CHANGE,
            PasswordResetAction.FORGOT,
        ]:
            return Response.bad_request_error_response(
                "Invalid action. Must be 'force_change' or 'forgot'."
            )

        try:
            try:
                user_id = supabase_user_client.reset_password_with_token(
                    token_hash=password_request.token_hash,
                    new_password=password_request.new_password,
                )
            except Exception as e:
                logger.error(f"Failed to reset password with recovery token: {str(e)}")
                return Response.bad_request_error_response(
                    "Invalid or expired recovery token"
                )

            if password_request.action == PasswordResetAction.FORCE_CHANGE:
                statement = select(User).where(User.supabase_user_id == user_id)
                result = master_session.exec(statement)
                current_user = result.first()

                if not current_user:
                    logger.warning(f"User not found for user_id: {user_id}")
                    return Response.unauthorized_error_response("Unauthorized")

                updated_metadata = {
                    "requires_password_change": False,
                    "email_verified": True,
                    "first_name": current_user.first_name,
                    "last_name": current_user.last_name,
                    "role": current_user.user_role,
                }

                try:
                    supabase_user_client.update_user_metadata(
                        user_id=current_user.supabase_user_id,
                        metadata=updated_metadata,
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to update user metadata in Supabase for user: {current_user.email}"
                    )
                    return Response.internal_server_error_response(
                        message="Failed to update user metadata", data=None
                    )

                logger.info(f"Updating local DB for user_id: {user_id}")
                current_user.email_verified = True
                current_user.last_login_at = datetime.utcnow()
                master_session.add(current_user)
                master_session.commit()
                master_session.refresh(current_user)
                logger.info(f"Local DB updated for user_id: {user_id}")
            else:
                logger.info(
                    f"Password reset completed for user: {user_id} (forgot action - no metadata update)"
                )

        except Exception as e:
            logger.error(f"Error updating user data after password reset: {str(e)}")
            return Response.internal_server_error_response(
                message="Error updating user data after password reset.", data=None
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
            message="First-time password change failed", data=None
        )


@router.post("/refresh-token", response_model=GenericResponse[UserRefreshTokenResponse], tags=["user_auth"])
async def refresh_token(refresh_request: UserRefreshTokenRequest):
    try:
        session_data = supabase_user_client.refresh_access_token(
            refresh_request.refresh_token
        )

        if session_data.get("session", {}).get("access_token"):
            return Response.success_response(
                message="Token refreshed successfully",
                data=UserRefreshTokenResponse(
                    access_token=session_data["session"]["access_token"],
                    refresh_token=session_data["session"]["refresh_token"],
                ),
            )
        else:
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
        return Response.internal_server_error_response(message="Token refresh failed. Please log in again.", data=None)


@router.post("/forgot-password", tags=["user_auth"])
async def forgot_password(
    forgot_password_request: UserForgotPasswordRequest,
    master_session: Session = Depends(lambda: Session(get_master_engine(echo=False))),
):
    try:
        statement = select(User).where(
            User.email == forgot_password_request.email
        )
        result = master_session.exec(statement)
        user = result.first()

        if not user:
            logger.warning(
                f"Password reset requested for non-existent user: {forgot_password_request.email}"
            )
            return Response.success_response(
                message="If an account with this email exists, a password reset link has been sent."
            )

        password_reset_link = supabase_user_client.generate_password_reset_link(
            email=user.email
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
        logger.info(f"Password reset link generated for user: {user.email}")

        email_sent = email_service.send_password_reset_email(
            to_email=user.email,
            first_name=user.first_name,
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