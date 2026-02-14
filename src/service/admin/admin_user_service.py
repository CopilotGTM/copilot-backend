"""Admin User Service - Service layer with session ownership.

This service follows the session dependency pattern from service-architecture-base.md:
- Session is passed to __init__ and owned by the service
- close() must be called when done (use dependency injection with try/finally)
- All business logic and database operations are in the service
"""

from datetime import UTC, datetime
from typing import cast as typing_cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import col
from src.domain.models.admin_user_models import (
    CreateUserRequest,
    PaginatedUserActionHistoryResponse,
    PaginatedUsers,
    UpdateUserRequest,
    UserActionHistoryResponse,
    UserResponse,
)
from src.domain.models.master_tables import (
    AdminUserStatus,
    Organisation,
    SuperAdmin,
    User,
    UserActionHistory,
    UserEventType,
)
from src.infra.auth.user_auth import user_auth
from src.infra.config.constant import AdminUserErrors
from src.infra.utils.cursor import decode_cursor, encode_cursor
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class AdminUserServiceError(Exception):
    """Base exception for AdminUserService."""

    def __init__(self, message: str, error_code: str | None = None):
        self.message = message
        self.error_code = error_code
        super().__init__(message)


class UserNotFoundError(AdminUserServiceError):
    """Raised when user is not found."""

    pass


class UserNotApprovedError(AdminUserServiceError):
    """Raised when user is not approved."""

    pass


class OrganisationNotFoundError(AdminUserServiceError):
    """Raised when organisation is not found."""

    pass


class UserAlreadyExistsError(AdminUserServiceError):
    """Raised when user already exists."""

    pass


class InvalidUserDataError(AdminUserServiceError):
    """Raised when user data is invalid."""

    pass


# -----------------------------------------------------------------------------
# Result Classes
# -----------------------------------------------------------------------------


class CreateUserResult:
    """Result for user creation."""

    def __init__(self, user: User, temp_password: str):
        self.user_id = user.user_id
        self.email = user.email
        self.first_name = user.first_name
        self.created_at = user.created_at
        self.temp_password = temp_password


class UpdateUserStatusResult:
    """Result for user status update."""

    def __init__(self, user: User):
        self.user_id = user.user_id
        self.status = user.status
        self.admin_status = user.admin_status
        self.updated_at = user.updated_at
        self.approved_at = user.approved_at
        self.approved_by = user.approved_by


# -----------------------------------------------------------------------------
# Service
# -----------------------------------------------------------------------------


class AdminUserService:
    """Service for managing users with session ownership.

    This service follows the session ownership pattern:
    - Session is passed to __init__ and owned by the service
    - close() must be called when done (use dependency injection with try/finally)
    """

    def __init__(self, session: AsyncSession):
        """Initialize the service with a database session.

        Args:
            session: AsyncSession for database operations
        """
        self._session = session

    async def close(self) -> None:
        """Close the database session.

        Should be called via dependency injection cleanup.
        """
        try:
            await self._session.close()
        except Exception:
            logger.debug("AdminUserService session close failed", exc_info=True)

    # -------------------------------------------------------------------------
    # User CRUD Operations
    # -------------------------------------------------------------------------

    async def create_user(
        self,
        admin_id: UUID,
        user_data: CreateUserRequest,
    ) -> CreateUserResult:
        """Create a new user.

        Args:
            admin_id: ID of the admin creating the user
            user_data: User creation request data

        Returns:
            CreateUserResult with new user info

        Raises:
            OrganisationNotFoundError: If organisation doesn't exist
            UserAlreadyExistsError: If user email already exists
            AdminUserServiceError: If Supabase user creation fails
        """
        logger.info(f"Attempting to create user: {user_data.email}")

        # Check if organisation exists
        organisation = await self._session.get(Organisation, user_data.organisation_id)
        if not organisation:
            raise OrganisationNotFoundError(
                AdminUserErrors.ORGANISATION_NOT_FOUND,
                error_code="ORGANISATION_NOT_FOUND",
            )

        # Check if user already exists in local DB
        result = await self._session.execute(
            select(User).where(typing_cast(ColumnElement[bool], User.email == user_data.email))
        )
        existing_user = result.scalars().first()
        if existing_user:
            raise UserAlreadyExistsError(
                AdminUserErrors.USER_ALREADY_EXISTS,
                error_code="USER_ALREADY_EXISTS",
            )

        # Create user in Supabase
        temp_password = user_auth.generate_temporary_password()

        supabase_user_id = user_auth.create_supabase_user(
            email=user_data.email,
            password=temp_password,
            first_name=user_data.first_name,
            last_name=user_data.last_name,
            user_role=user_data.user_role.value,
        )

        if not supabase_user_id:
            raise AdminUserServiceError(
                "Failed to create user in authentication system",
                error_code="SUPABASE_CREATE_FAILED",
            )

        # Create local user record
        new_user = User(
            **user_data.dict(exclude_none=True),
            supabase_user_id=supabase_user_id,
            created_by_auth_user_id=str(admin_id),
        )
        self._session.add(new_user)

        # Create history entry
        history_entry = UserActionHistory(
            user_id=new_user.user_id,
            action=UserEventType.CREATED,
            actor_admin_id=admin_id,
            note="User created by admin.",
            details={"admin_id": str(admin_id)},
        )
        self._session.add(history_entry)

        await self._session.commit()
        await self._session.refresh(new_user)

        logger.info(f"Successfully created user {new_user.email} with ID {new_user.user_id}")
        return CreateUserResult(new_user, temp_password=temp_password)

    async def get_user(self, user_id: UUID) -> UserResponse:
        """Get a user by ID.

        Args:
            user_id: User's UUID

        Returns:
            UserResponse with user data

        Raises:
            UserNotFoundError: If user doesn't exist
            UserNotApprovedError: If user is not approved
        """
        user = await self._session.get(User, user_id)

        if not user:
            raise UserNotFoundError(
                AdminUserErrors.USER_NOT_FOUND,
                error_code="USER_NOT_FOUND",
            )

        if user.admin_status != AdminUserStatus.APPROVED:
            logger.warning(
                f"Attempt to access non-approved user '{user_id}'. "
                f"Status is '{user.admin_status.value}'."
            )
            raise UserNotApprovedError(
                AdminUserErrors.USER_NOT_APPROVED,
                error_code="USER_NOT_APPROVED",
            )

        return UserResponse.from_orm(user)

    async def list_users(
        self,
        limit: int,
        cursor: str | None,
        organisation_id: UUID | None,
    ) -> PaginatedUsers:
        """List users with pagination.

        Args:
            limit: Maximum number of users to return
            cursor: Pagination cursor
            organisation_id: Optional filter by organisation

        Returns:
            PaginatedUsers with users and next cursor
        """
        statement = select(User)

        if organisation_id:
            statement = statement.where(
                typing_cast(ColumnElement[bool], User.organisation_id == organisation_id)
            )

        if cursor:
            ts, user_id_cursor = decode_cursor(cursor)
            statement = statement.where(
                typing_cast(
                    ColumnElement[bool],
                    (User.created_at < ts)
                    | ((User.created_at == ts) & (User.user_id < user_id_cursor)),
                )
            )

        statement = statement.order_by(
            col(User.created_at).desc(),
            col(User.user_id).desc(),
        ).limit(limit + 1)

        result = await self._session.execute(statement)
        users = result.scalars().all()

        has_more = len(users) == limit + 1
        items = users[:limit]

        user_responses = [UserResponse.from_orm(user) for user in items]
        next_cursor = encode_cursor(items[-1].created_at, items[-1].user_id) if has_more else None

        return PaginatedUsers(users=user_responses, cursor=next_cursor)

    async def update_user(
        self,
        user_id: UUID,
        admin_id: UUID,
        user_data: UpdateUserRequest,
    ) -> UserResponse:
        """Update a user's details.

        Args:
            user_id: User's UUID
            admin_id: Admin performing the update
            user_data: Update request data

        Returns:
            UserResponse with updated user data

        Raises:
            UserNotFoundError: If user doesn't exist
            InvalidUserDataError: If no update data provided
        """
        user = await self._session.get(User, user_id)
        if not user:
            raise UserNotFoundError(
                AdminUserErrors.USER_NOT_FOUND,
                error_code="USER_NOT_FOUND",
            )

        update_data = user_data.dict(exclude_unset=True)
        if not update_data:
            raise InvalidUserDataError(
                AdminUserErrors.NO_UPDATE_DATA_PROVIDED,
                error_code="NO_UPDATE_DATA",
            )

        for key, value in update_data.items():
            setattr(user, key, value)

        history_entry = UserActionHistory(
            user_id=user.user_id,
            action=UserEventType.UPDATED,
            actor_admin_id=admin_id,
            note="User details updated by admin.",
            details={"updated_fields": list(update_data.keys())},
        )
        self._session.add(history_entry)
        self._session.add(user)

        await self._session.commit()
        await self._session.refresh(user)

        return UserResponse.from_orm(user)

    # -------------------------------------------------------------------------
    # User Status Operations
    # -------------------------------------------------------------------------

    async def update_user_admin_status(
        self,
        user_id: UUID,
        admin_id: UUID,
        new_admin_status: AdminUserStatus,
        note: str | None,
    ) -> UpdateUserStatusResult:
        """Update a user's admin status.

        Args:
            user_id: User's UUID
            admin_id: Admin performing the update
            new_admin_status: New status to set
            note: Optional note for history

        Returns:
            UpdateUserStatusResult with updated status info

        Raises:
            UserNotFoundError: If user doesn't exist
        """
        user = await self._session.get(User, user_id)
        if not user:
            raise UserNotFoundError(
                AdminUserErrors.USER_NOT_FOUND,
                error_code="USER_NOT_FOUND",
            )

        user.admin_status = new_admin_status
        if new_admin_status == AdminUserStatus.APPROVED:
            user.approved_at = datetime.now(UTC)
            user.approved_by = admin_id

        history_entry = UserActionHistory(
            user_id=user.user_id,
            action=UserEventType.STATUS_CHANGED,
            actor_admin_id=admin_id,
            note=note or f"Admin status changed to {new_admin_status.value}",
            details={"new_status": new_admin_status.value},
        )
        self._session.add(history_entry)
        self._session.add(user)

        await self._session.commit()
        await self._session.refresh(user)

        return UpdateUserStatusResult(user)

    # -------------------------------------------------------------------------
    # History Operations
    # -------------------------------------------------------------------------

    async def list_user_action_history(
        self,
        organisation_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> PaginatedUserActionHistoryResponse:
        """List user action history for an organisation.

        Args:
            organisation_id: Organisation's UUID
            limit: Maximum number of records to return
            cursor: Pagination cursor

        Returns:
            PaginatedUserActionHistoryResponse with history and next cursor
        """
        statement = (
            select(UserActionHistory, User, Organisation, SuperAdmin)
            .join(User, typing_cast(ColumnElement[bool], User.user_id == UserActionHistory.user_id))
            .join(
                Organisation,
                typing_cast(
                    ColumnElement[bool], Organisation.organisation_id == User.organisation_id
                ),
            )
            .outerjoin(
                SuperAdmin,
                typing_cast(
                    ColumnElement[bool], SuperAdmin.admin_id == UserActionHistory.actor_admin_id
                ),
            )
            .where(typing_cast(ColumnElement[bool], User.organisation_id == organisation_id))
        )

        if cursor:
            ts, history_id = decode_cursor(cursor)
            statement = statement.where(
                typing_cast(
                    ColumnElement[bool],
                    (UserActionHistory.created_at < ts)
                    | ((UserActionHistory.created_at == ts) & (UserActionHistory.id < history_id)),
                )
            )

        statement = statement.order_by(
            col(UserActionHistory.created_at).desc(),
            col(UserActionHistory.id).desc(),
        ).limit(limit + 1)

        result = await self._session.execute(statement)
        rows = result.all()

        has_more = len(rows) == limit + 1
        items = rows[:limit]

        history_responses: list[UserActionHistoryResponse] = []
        for history, user, org, actor_admin in items:
            history_responses.append(
                UserActionHistoryResponse(
                    id=history.id,
                    user_id=history.user_id,
                    user_email=user.email,
                    user_first_name=user.first_name,
                    user_last_name=user.last_name,
                    organisation_id=org.organisation_id,
                    organisation_name=org.organisation_name,
                    action=history.action.value,
                    note=history.note,
                    details=history.details,
                    created_at=history.created_at,
                    actor_admin_id=history.actor_admin_id,
                    actor_admin_name=(
                        f"{actor_admin.first_name} {actor_admin.last_name}"
                        if actor_admin is not None
                        else None
                    ),
                    actor_admin_email=(actor_admin.email if actor_admin is not None else None),
                )
            )

        next_cursor = encode_cursor(items[-1][0].created_at, items[-1][0].id) if has_more else None

        return PaginatedUserActionHistoryResponse(
            history=history_responses,
            cursor=next_cursor,
        )
