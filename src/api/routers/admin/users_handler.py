from typing import Optional, List
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from src.domain.models.master_tables import SuperAdmin, AdminRole, User, AdminUserStatus, SupabaseUserStatus, AdminUserStatus, UserAppRole
from src.domain.models.admin_user_models import (
    CreateUserRequest,
    CreateUserResponse,
    PaginatedUsers,
    UserResponse,
    UpdateUserStatusRequest,
    UpdateUserStatusResponse,
    UserActionHistoryResponse,
    UpdateUserRequest,
)
from src.infra.utils.app_state import fastapp
from src.infra.utils.logger import get_logger
from src.infra.utils.cursor import encode_cursor
from src.service.user_management import user_management_service
from src.api.utils.http_response import Response
from src.infra.auth.admin_auth import admin_auth
from src.api.schemas.base_models import GenericResponse

logger = get_logger("admin_users_handler")
router = APIRouter()


@router.post(
    "/create_user",
    # response_model=GenericResponse[CreateUserResponse],
    tags=["admin_users"],
)
async def create_user(
    request: CreateUserRequest,
    current_admin: SuperAdmin = Depends(admin_auth),
    master_session: Session = Depends(lambda: next(fastapp.state.master_session())),
):
    """
    Create a new user within an organization.
    This endpoint is restricted to admins and super admins.
    """
    try:
        logger.info(f"User creation request for email '{request.email}' by admin: {current_admin.email}")

        if current_admin.role not in [AdminRole.SUPER_ADMIN, AdminRole.ADMIN]:
            logger.warning(f"Unauthorized attempt to create user by: {current_admin.email}.")
            return Response.forbidden_error_response("Admin access required")
        # Validate request data
        if not request.email or not request.first_name or not request.last_name:
            logger.warning("Invalid user creation request: Missing required fields.")
            return Response.bad_request_error_response("Email, first name, and last name are required.")
        if request.organisation_id is None:
            logger.warning("Invalid user creation request: Missing organisation ID.")
            return Response.bad_request_error_response("Organisation ID is required.")
        if request.user_role not in [UserAppRole.MEMBER, UserAppRole.ADMIN]:
            logger.warning(f"Invalid user role '{request.user_role}' in user creation request.")
            return Response.bad_request_error_response("Invalid user role. Must be 'MEMBER' or 'ADMIN'.")

        new_user = user_management_service.create_user(
            session=master_session,
            admin_id=current_admin.admin_id,
            user_data=request,
        )

        response_data = CreateUserResponse(
            user_id=new_user.user_id,
            email=new_user.email,
            created_at=new_user.created_at,
        )
        
        logger.info(f"Successfully created user: {new_user.email} with ID: {new_user.user_id}")
        return Response.success_response(message="User created successfully", data=response_data)

    except ValueError as ve:
        logger.warning(f"Value error while creating user: {ve}")
        return Response.bad_request_error_response(message=str(ve))
    except Exception as e:
        logger.error(
            f"Error creating user '{request.email}': {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(message=f"Failed to create user - {e}", data={})


@router.get(
    "/{user_id}/profile",
    response_model=GenericResponse[UserResponse],
    tags=["admin_users"],
)
async def get_user(
    user_id: UUID,
    current_admin: SuperAdmin = Depends(admin_auth),
    master_session: Session = Depends(lambda: next(fastapp.state.master_session())),
):
    """
    Retrieve a single user by their ID.
    This endpoint is restricted to admins and super admins, and will only return users
    who have been approved.
    """
    try:
        logger.info(f"Request to get user '{user_id}' by admin: {current_admin.email}")

        if current_admin.role not in [AdminRole.SUPER_ADMIN, AdminRole.ADMIN]:
            logger.warning(f"Unauthorized attempt to get user by: {current_admin.email}.")
            return Response.forbidden_error_response("Admin access required")

        user = user_management_service.get_user(session=master_session, user_id=user_id)

        if not user:
            logger.warning(f"User with ID '{user_id}' not found.")
            return Response.not_found_error_response("User not found")

        if user.admin_status != AdminUserStatus.APPROVED:
            logger.warning(f"Attempt to access non-approved user '{user_id}'. Status is '{user.admin_status.value}'.")
            return Response.forbidden_error_response("User is not approved")

        response_data = UserResponse.from_orm(user)
        
        logger.info(f"Successfully retrieved user: {user.email} with ID: {user.user_id}")
        return Response.success_response(message="User retrieved successfully", data=response_data)

    except Exception as e:
        logger.error(
            f"Error retrieving user '{user_id}': {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(message=f"Failed to retrieve user - {e}", data={})


@router.get(
    "/list_users",
    response_model=GenericResponse[PaginatedUsers],
    tags=["admin_users"],
)
async def list_users(
    limit: int = Query(20, ge=1, le=100),
    cursor: Optional[str] = Query(None),
    organisation_id: Optional[UUID] = Query(None, description="Filter users by organization ID"),
    current_admin: SuperAdmin = Depends(admin_auth),
    master_session: Session = Depends(lambda: next(fastapp.state.master_session())),
):
    """
    Retrieve a list of users with pagination.
    Can be filtered by organization.
    This endpoint is restricted to admins and super admins.
    """
    try:
        logger.info(f"Request to list users by admin: {current_admin.email}")

        if current_admin.role not in [AdminRole.SUPER_ADMIN, AdminRole.ADMIN]:
            logger.warning(f"Unauthorized attempt to list users by: {current_admin.email}.")
            return Response.forbidden_error_response("Admin access required")

        users = user_management_service.list_users(
            session=master_session,
            limit=limit,
            cursor=cursor,
            organisation_id=organisation_id,
        )

        has_more = len(users) == limit + 1
        items = users[:limit]
        
        user_responses = [UserResponse.from_orm(user) for user in items]

        next_cursor = encode_cursor(items[-1].created_at, items[-1].user_id) if has_more else None
        
        response_data = PaginatedUsers(users=user_responses, cursor=next_cursor)
        
        logger.info(f"Successfully retrieved {len(items)} users.")
        return Response.success_response(message="Users retrieved successfully", data=response_data)

    except Exception as e:
        logger.error(
            f"Error listing users: {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(message=f"Failed to list users - {e}", data={})


@router.put(
    "/{user_id}/admin_status",
    response_model=GenericResponse[UpdateUserStatusResponse],
    tags=["admin_users"],
)
async def update_user_admin_status(
    user_id: UUID,
    request: UpdateUserStatusRequest,
    current_admin: SuperAdmin = Depends(admin_auth),
    master_session: Session = Depends(lambda: next(fastapp.state.master_session())),
):
    """
    Update the application-side status of a user (e.g., approve, reject, suspend).
    This endpoint is restricted to super admins.
    """
    try:
        logger.info(f"Admin status update request for user '{user_id}' to '{request.admin_status}' by admin: {current_admin.email}")

        if current_admin.role not in [AdminRole.SUPER_ADMIN]:
            logger.warning(f"Unauthorized attempt to update user admin status by user: {current_admin.admin_id}.")
            return Response.forbidden_error_response("Super admin access required")

        updated_user = user_management_service.update_user_admin_status(
            session=master_session,
            user_id=user_id,
            admin_id=current_admin.admin_id,
            new_admin_status=request.admin_status,
            note=request.note,
        )

        response_data = UpdateUserStatusResponse(
            user_id=updated_user.user_id,
            status=updated_user.status,
            admin_status=updated_user.admin_status,
            updated_at=updated_user.updated_at,
            approved_at=updated_user.approved_at,
            approved_by=updated_user.approved_by,
        )

        logger.info(f"Successfully updated admin status for user: {updated_user.email} to {updated_user.admin_status}")
        return Response.success_response(message=f"User admin status updated to '{updated_user.admin_status.value}'", data=response_data)

    except ValueError as ve:
        logger.warning(f"Value error while updating user admin status: {ve}")
        return Response.bad_request_error_response(message=str(ve))
    except Exception as e:
        logger.error(
            f"Error updating user admin status for '{user_id}': {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(message="Failed to update user admin status", data={})


@router.get(
    "/history/organisation/{organisation_id}",
    response_model=GenericResponse[List[UserActionHistoryResponse]],
    tags=["admin_users"],
)
async def list_user_action_history(
    organisation_id: UUID,
    limit: int = Query(20, ge=1, le=100),
    cursor: Optional[str] = Query(None),
    current_admin: SuperAdmin = Depends(admin_auth),
    master_session: Session = Depends(lambda: next(fastapp.state.master_session())),
):
    """
    Retrieve the action history for all users in an organisation.
    """
    try:
        logger.info(f"Request to list user action history for organisation '{organisation_id}' by admin: {current_admin.email}")

        if current_admin.role not in [AdminRole.SUPER_ADMIN, AdminRole.ADMIN]:
            logger.warning(f"Unauthorized attempt to list user action history by: {current_admin.email}.")
            return Response.forbidden_error_response("Admin access required")

        history = user_management_service.list_user_action_history_for_organisation(
            session=master_session,
            organisation_id=organisation_id,
            limit=limit,
            cursor=cursor,
        )

        has_more = len(history) == limit + 1
        items = history[:limit]
        
        history_responses = [UserActionHistoryResponse.from_orm(h) for h in items]

        next_cursor = encode_cursor(items[-1].created_at, items[-1].id) if has_more else None
        
        response_data = {
            "history": history_responses,
            "cursor": next_cursor,
        }
        
        logger.info(f"Successfully retrieved {len(items)} user action history records.")
        return Response.success_response(message="User action history retrieved successfully", data=response_data)

    except Exception as e:
        logger.error(
            f"Error listing user action history for organisation '{organisation_id}': {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(message=f"Failed to list user action history - {e}", data={})


@router.put(
    "/{user_id}/update",
    response_model=GenericResponse[UserResponse],
    tags=["admin_users"],
)
async def update_user(
    user_id: UUID,
    request: UpdateUserRequest,
    current_admin: SuperAdmin = Depends(admin_auth),
    master_session: Session = Depends(lambda: next(fastapp.state.master_session())),
):
    """
    Update a user's details.
    """
    try:
        logger.info(f"Request to update user '{user_id}' by admin: {current_admin.email}")

        if current_admin.role not in [AdminRole.SUPER_ADMIN, AdminRole.ADMIN]:
            logger.warning(f"Unauthorized attempt to update user by: {current_admin.email}.")
            return Response.forbidden_error_response("Admin access required")

        updated_user = user_management_service.update_user(
            session=master_session,
            user_id=user_id,
            admin_id=current_admin.admin_id,
            user_data=request,
        )

        response_data = UserResponse.from_orm(updated_user)
        
        logger.info(f"Successfully updated user: {updated_user.email} with ID: {updated_user.user_id}")
        return Response.success_response(message="User updated successfully", data=response_data)

    except ValueError as ve:
        logger.warning(f"Value error while updating user: {ve}")
        return Response.bad_request_error_response(message=str(ve))
    except Exception as e:
        logger.error(
            f"Error updating user '{user_id}': {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(message=f"Failed to update user - {e}", data={})