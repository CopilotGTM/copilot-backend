from collections.abc import AsyncGenerator
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.schemas.base_models import GenericResponse
from src.api.utils.http_response import Response
from src.domain.models.admin_user_models import (
    CreateUserRequest,
    CreateUserResponse,
    PaginatedUserActionHistoryResponse,
    PaginatedUsers,
    UpdateUserRequest,
    UpdateUserStatusRequest,
    UpdateUserStatusResponse,
    UserResponse,
)
from src.domain.models.master_tables import (
    AdminRole,
    SuperAdmin,
    UserAppRole,
)
from src.infra.auth.admin_auth import admin_auth
from src.infra.config.constant import AdminUserErrors
from src.infra.utils.logger import get_logger
from src.service.admin.admin_user_service import (
    AdminUserService,
    AdminUserServiceError,
    InvalidUserDataError,
    OrganisationNotFoundError,
    UserAlreadyExistsError,
    UserNotApprovedError,
    UserNotFoundError,
)

logger = get_logger("admin_users_handler")
router = APIRouter()


async def get_admin_user_service(
    session: AsyncSession = Depends(get_master_async_session),
) -> AsyncGenerator[AdminUserService, None]:
    """Dependency that provides AdminUserService with session ownership.

    The service owns the session and closes it in the finally block.
    """
    service = AdminUserService(session)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("AdminUserService close failed", exc_info=True)


@router.post(
    "/create_user",
    response_model=GenericResponse[CreateUserResponse],
    tags=["admin_users"],
)
async def create_user(
    request: CreateUserRequest,
    response: FastAPIResponse,
    current_admin: SuperAdmin = Depends(admin_auth),
    service: AdminUserService = Depends(get_admin_user_service),
) -> GenericResponse[CreateUserResponse]:
    """
    Create a new user within an organization.
    This endpoint is restricted to admins and super admins.
    """
    try:
        logger.info(
            f"User creation request for email '{request.email}' by admin: {current_admin.email}"
        )

        if current_admin.role not in [AdminRole.SUPER_ADMIN, AdminRole.ADMIN]:
            logger.warning(f"Unauthorized attempt to create user by: {current_admin.email}.")
            return Response.forbidden_error_response(
                AdminUserErrors.ADMIN_ACCESS_REQUIRED, response=response
            )

        # Validate request data
        if not request.email or not request.first_name or not request.last_name:
            logger.warning("Invalid user creation request: Missing required fields.")
            return Response.bad_request_error_response(
                AdminUserErrors.CREATE_USER_MISSING_FIELDS, response=response
            )
        if request.organisation_id is None:
            logger.warning("Invalid user creation request: Missing organisation ID.")
            return Response.bad_request_error_response(
                AdminUserErrors.CREATE_USER_MISSING_ORG_ID, response=response
            )
        if request.user_role not in [UserAppRole.MEMBER, UserAppRole.ADMIN]:
            logger.warning(f"Invalid user role '{request.user_role}' in user creation request.")
            return Response.bad_request_error_response(
                AdminUserErrors.CREATE_USER_INVALID_ROLE, response=response
            )

        result = await service.create_user(
            admin_id=current_admin.admin_id,
            user_data=request,
        )

        response_data = CreateUserResponse(
            user_id=result.user_id,
            email=result.email,
            created_at=result.created_at,
        )

        logger.info(f"Successfully created user: {result.email} with ID: {result.user_id}")
        return Response.success_response(message="User created successfully", data=response_data)

    except OrganisationNotFoundError:
        return Response.bad_request_error_response(
            AdminUserErrors.ORGANISATION_NOT_FOUND, response=response
        )
    except UserAlreadyExistsError:
        return Response.bad_request_error_response(
            AdminUserErrors.USER_ALREADY_EXISTS, response=response
        )
    except AdminUserServiceError as e:
        logger.warning(f"Service error while creating user: {e.message}")
        return Response.bad_request_error_response(message=e.message, response=response)
    except Exception as e:
        logger.error(
            f"Error creating user '{request.email}': {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message=f"Failed to create user - {e}", data={}, response=response
        )


@router.get(
    "/{user_id}/profile",
    response_model=GenericResponse[UserResponse],
    tags=["admin_users"],
)
async def get_user(
    user_id: UUID,
    response: FastAPIResponse,
    current_admin: SuperAdmin = Depends(admin_auth),
    service: AdminUserService = Depends(get_admin_user_service),
) -> GenericResponse[UserResponse]:
    """
    Retrieve a single user by their ID.
    This endpoint is restricted to admins and super admins, and will only return users
    who have been approved.
    """
    try:
        logger.info(f"Request to get user '{user_id}' by admin: {current_admin.email}")

        if current_admin.role not in [AdminRole.SUPER_ADMIN, AdminRole.ADMIN]:
            logger.warning(f"Unauthorized attempt to get user by: {current_admin.email}.")
            return Response.forbidden_error_response(
                AdminUserErrors.ADMIN_ACCESS_REQUIRED, response=response
            )

        user_response = await service.get_user(user_id=user_id)

        logger.info(f"Successfully retrieved user with ID: {user_id}")
        return Response.success_response(message="User retrieved successfully", data=user_response)

    except UserNotFoundError:
        logger.warning(f"User with ID '{user_id}' not found.")
        return Response.not_found_error_response(AdminUserErrors.USER_NOT_FOUND, response=response)
    except UserNotApprovedError:
        return Response.forbidden_error_response(
            AdminUserErrors.USER_NOT_APPROVED, response=response
        )
    except Exception as e:
        logger.error(
            f"Error retrieving user '{user_id}': {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message=f"Failed to retrieve user - {e}", data={}, response=response
        )


@router.get(
    "/list_users",
    response_model=GenericResponse[PaginatedUsers],
    tags=["admin_users"],
)
async def list_users(
    response: FastAPIResponse,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None),
    organisation_id: UUID | None = Query(None, description="Filter users by organization ID"),
    current_admin: SuperAdmin = Depends(admin_auth),
    service: AdminUserService = Depends(get_admin_user_service),
) -> GenericResponse[PaginatedUsers]:
    """
    Retrieve a list of users with pagination.
    Can be filtered by organization.
    This endpoint is restricted to admins and super admins.
    """
    try:
        logger.info(f"Request to list users by admin: {current_admin.email}")

        if current_admin.role not in [AdminRole.SUPER_ADMIN, AdminRole.ADMIN]:
            logger.warning(f"Unauthorized attempt to list users by: {current_admin.email}.")
            return Response.forbidden_error_response(
                AdminUserErrors.ADMIN_ACCESS_REQUIRED, response=response
            )

        paginated_users = await service.list_users(
            limit=limit,
            cursor=cursor,
            organisation_id=organisation_id,
        )

        logger.info(f"Successfully retrieved {len(paginated_users.users)} users.")
        return Response.success_response(
            message="Users retrieved successfully", data=paginated_users
        )

    except Exception as e:
        logger.error(
            f"Error listing users: {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message=f"Failed to list users - {e}", data={}, response=response
        )


@router.put(
    "/{user_id}/admin_status",
    response_model=GenericResponse[UpdateUserStatusResponse],
    tags=["admin_users"],
)
async def update_user_admin_status(
    user_id: UUID,
    request: UpdateUserStatusRequest,
    response: FastAPIResponse,
    current_admin: SuperAdmin = Depends(admin_auth),
    service: AdminUserService = Depends(get_admin_user_service),
) -> GenericResponse[UpdateUserStatusResponse]:
    """
    Update the application-side status of a user (e.g., approve, reject, suspend).
    This endpoint is restricted to super admins.
    """
    try:
        logger.info(
            f"Admin status update request for user '{user_id}' to '{request.admin_status}' "
            f"by admin: {current_admin.email}"
        )

        if current_admin.role not in [AdminRole.SUPER_ADMIN]:
            logger.warning(
                f"Unauthorized attempt to update user admin status by user: {current_admin.admin_id}."
            )
            return Response.forbidden_error_response(
                AdminUserErrors.ADMIN_ACCESS_REQUIRED, response=response
            )

        result = await service.update_user_admin_status(
            user_id=user_id,
            admin_id=current_admin.admin_id,
            new_admin_status=request.admin_status,
            note=request.note,
        )

        response_data = UpdateUserStatusResponse(
            user_id=result.user_id,
            status=result.status,
            admin_status=result.admin_status,
            updated_at=result.updated_at,
            approved_at=result.approved_at,
            approved_by=result.approved_by,
        )

        logger.info(
            f"Successfully updated admin status for user ID: {result.user_id} "
            f"to {result.admin_status}"
        )
        return Response.success_response(
            message=f"User admin status updated to '{result.admin_status.value}'",
            data=response_data,
        )

    except UserNotFoundError:
        return Response.bad_request_error_response(
            AdminUserErrors.USER_NOT_FOUND, response=response
        )
    except AdminUserServiceError as e:
        logger.warning(f"Service error while updating user admin status: {e.message}")
        return Response.bad_request_error_response(message=e.message, response=response)
    except Exception as e:
        logger.error(
            f"Error updating user admin status for '{user_id}': {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message="Failed to update user admin status", data={}, response=response
        )


@router.get(
    "/history/organisation/{organisation_id}",
    response_model=GenericResponse[PaginatedUserActionHistoryResponse],
    tags=["admin_users"],
)
async def list_user_action_history(
    organisation_id: UUID,
    response: FastAPIResponse,
    limit: int = Query(20, ge=1, le=20),
    cursor: str | None = Query(None),
    current_admin: SuperAdmin = Depends(admin_auth),
    service: AdminUserService = Depends(get_admin_user_service),
) -> GenericResponse[PaginatedUserActionHistoryResponse]:
    """
    Retrieve the action history for all users in an organisation.
    """
    try:
        logger.info(
            f"Request to list user action history for organisation '{organisation_id}' "
            f"by admin: {current_admin.email}"
        )

        if current_admin.role not in [AdminRole.SUPER_ADMIN, AdminRole.ADMIN]:
            logger.warning(
                f"Unauthorized attempt to list user action history by: {current_admin.email}."
            )
            return Response.forbidden_error_response(
                AdminUserErrors.ADMIN_ACCESS_REQUIRED, response=response
            )

        paginated_history = await service.list_user_action_history(
            organisation_id=organisation_id,
            limit=limit,
            cursor=cursor,
        )

        logger.info(
            f"Successfully retrieved {len(paginated_history.history)} user action history records."
        )
        return Response.success_response(
            message="User action history retrieved successfully", data=paginated_history
        )

    except Exception as e:
        logger.error(
            f"Error listing user action history for organisation '{organisation_id}': {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message=f"Failed to list user action history - {e}", data={}, response=response
        )


@router.put(
    "/{user_id}/update",
    response_model=GenericResponse[UserResponse],
    tags=["admin_users"],
)
async def update_user(
    user_id: UUID,
    request: UpdateUserRequest,
    response: FastAPIResponse,
    current_admin: SuperAdmin = Depends(admin_auth),
    service: AdminUserService = Depends(get_admin_user_service),
) -> GenericResponse[UserResponse]:
    """
    Update a user's details.
    """
    try:
        logger.info(f"Request to update user '{user_id}' by admin: {current_admin.email}")

        if current_admin.role not in [AdminRole.SUPER_ADMIN, AdminRole.ADMIN]:
            logger.warning(f"Unauthorized attempt to update user by: {current_admin.email}.")
            return Response.forbidden_error_response(
                AdminUserErrors.ADMIN_ACCESS_REQUIRED, response=response
            )

        user_response = await service.update_user(
            user_id=user_id,
            admin_id=current_admin.admin_id,
            user_data=request,
        )

        logger.info(f"Successfully updated user with ID: {user_id}")
        return Response.success_response(message="User updated successfully", data=user_response)

    except UserNotFoundError:
        return Response.bad_request_error_response(
            AdminUserErrors.USER_NOT_FOUND, response=response
        )
    except InvalidUserDataError as e:
        return Response.bad_request_error_response(e.message, response=response)
    except AdminUserServiceError as e:
        logger.warning(f"Service error while updating user: {e.message}")
        return Response.bad_request_error_response(message=e.message, response=response)
    except Exception as e:
        logger.error(
            f"Error updating user '{user_id}': {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message=f"Failed to update user - {e}", data={}, response=response
        )
