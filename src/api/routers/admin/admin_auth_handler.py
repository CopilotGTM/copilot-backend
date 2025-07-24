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
    AdminLoginState,
    AdminLoginResponse,
    AdminCreateRequest,
    AdminUpdateRequest,
    AdminStatus,
    SuperAdmin,
)
from src.infra.utils.database import get_master_session
from src.infra.auth.admin_auth import admin_auth
from src.infra.auth.email_service import email_service
from src.infra.auth.supabase_auth import supabase_auth
from src.infra.config.configs import supabase_config
from src.infra.utils.logger import get_logger
from src.api.utils.http_response import Response


logger = get_logger("admin_auth_handler")
router = APIRouter()


@router.post("/login", response_model=AdminLoginResponse, tags=["admin_auth"])
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
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                },
                "session_token": session_token,
            }
            return response_data
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
            return response_data

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
    current_admin: SuperAdmin = Depends(admin_auth),
    master_session: Session = Depends(get_master_session),
):
    """
    First-time password change endpoint for Super Admin.
    This endpoint is used after logging in with a temporary password to set a new permanent password.
    It requires authentication via the access_token obtained from the login endpoint.
    Updates the password in Supabase, sets requires_password_change to False in user metadata,
    updates the local database status to active, marks email as verified if not already,
    and updates the last_login_at timestamp.

    - Validates that new_password and confirm_password match (if confirm_password is provided).
    - Checks if the user is in a state requiring password change (based on Supabase metadata).
    - This endpoint is intended only for first-time changes; regular password changes should use a separate endpoint if needed.
    """
    try:
        if not current_admin:
            logger.warning("Unauthorized access attempt to change-password endpoint")
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
        return {"message": "Password changed successfully. You can now log in with your new password."}

    except Exception as e:
        logger.error(
            "Error during first-time password change for email %s: %s",
            current_admin.email if current_admin else "unknown",
            str(e),
            exc_info=True,
        )
        return Response.internal_server_error_response("Password change failed")
