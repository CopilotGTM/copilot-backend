from typing import Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session
from uuid import UUID

from src.domain.models.authentication import Authentication
from src.infra.utils.database import get_master_session
from src.infra.utils.logger import get_logger
from src.service.user_management import user_management_service
from src.infra.auth.supabase_auth import supabase_auth
from src.api.utils.http_response import Response

auth = Authentication()
logger = get_logger()
auth_router = APIRouter()


@auth_router.post("/register")
async def register_user(
    supabase_user_data: Dict[str, Any],
    organisation_id: Optional[UUID] = None,
    master_session: Session = Depends(get_master_session),
) -> Dict[str, Any]:
    """
    Register a new user from Supabase authentication.

    This endpoint is typically called after a user signs up through Supabase.
    It creates a corresponding user record in our database.
    """
    try:
        logger.info("Registering new user from Supabase")

        # Create user in database
        user = user_management_service.create_user_from_supabase(
            supabase_user_data=supabase_user_data, session=master_session, organisation_id=organisation_id
        )

        if not user:
            logger.error("Failed to create user from Supabase data")
            return Response.bad_request_error_response("Failed to create user")

        logger.info(f"User registered successfully: {user.user_id}")
        return Response.success_response(
            "User registered successfully",
            {
                "user_id": str(user.user_id),
                "organisation_id": str(user.organisation_id),
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
            },
        )

    except Exception as e:
        logger.error(f"Error during user registration: {str(e)}")
        return Response.internal_server_error_response("Registration failed")


@auth_router.post("/login")
async def login_user(
    auth_data: Dict[str, Any] = Depends(auth.verify_session),
    master_session: Session = Depends(get_master_session),
) -> Dict[str, Any]:
    """
    Handle user login and return user information.

    This endpoint verifies the Supabase token and returns user details.
    """
    try:
        logger.info("User login successful")

        user = auth_data.get("user")
        if not user:
            return Response.bad_request_error_response("User not found")

        # Update last login timestamp
        user_management_service.update_user_last_login(user.user_id, master_session)

        return Response.success_response(
            "Login successful",
            {
                "user_id": str(user.user_id),
                "organisation_id": str(user.organisation_id),
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "organisation_role": user.organisation_role,
                "is_admin": user.is_admin,
            },
        )

    except Exception as e:
        logger.error(f"Error during login: {str(e)}")
        return Response.internal_server_error_response("Login failed")


@auth_router.get("/me")
async def get_current_user(
    auth_data: Dict[str, Any] = Depends(auth.verify_session),
) -> Dict[str, Any]:
    """
    Get current user information.
    """
    try:
        user = auth_data.get("user")
        if not user:
            return Response.bad_request_error_response("User not found")

        return Response.success_response(
            "User information retrieved",
            {
                "user_id": str(user.user_id),
                "organisation_id": str(user.organisation_id),
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "organisation_role": user.organisation_role,
                "is_admin": user.is_admin,
                "title": user.title,
                "is_active": user.is_active,
                "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
            },
        )

    except Exception as e:
        logger.error(f"Error getting user information: {str(e)}")
        return Response.internal_server_error_response("Failed to get user information")


@auth_router.post("/assign-organisation")
async def assign_user_to_organisation(
    user_id: UUID,
    organisation_id: UUID,
    auth_data: Dict[str, Any] = Depends(auth.verify_session),
    master_session: Session = Depends(get_master_session),
) -> Dict[str, Any]:
    """
    Assign a user to an organisation.
    Only admins can perform this operation.
    """
    try:
        # Check if current user is admin
        current_user = auth_data.get("user")
        if not current_user or not current_user.is_admin:
            return Response.forbidden_error_response("Admin access required")

        # Assign user to organisation
        success = user_management_service.assign_user_to_organisation(
            user_id=user_id, organisation_id=organisation_id, session=master_session
        )

        if not success:
            return Response.bad_request_error_response("Failed to assign user to organisation")

        logger.info(f"User {user_id} assigned to organisation {organisation_id}")
        return Response.success_response("User assigned to organisation successfully")

    except Exception as e:
        logger.error(f"Error assigning user to organisation: {str(e)}")
        return Response.internal_server_error_response("Failed to assign user to organisation")
