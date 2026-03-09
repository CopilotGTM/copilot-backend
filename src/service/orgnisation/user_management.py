from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.admin_user_models import (
    CreateUserRequest,
    UpdateUserRequest,
)
from src.domain.models.master_tables import (
    AdminUserStatus,
    Organisation,
    User,
    UserActionHistory,
    UserEventType,
)
from src.infra.auth.email_service import email_service
from src.infra.auth.user_auth import user_auth
from src.infra.config.constant import AdminUserErrors
from src.infra.utils.cursor import decode_cursor
from src.infra.utils.logger import get_logger

logger = get_logger("user_management_service")


class UserManagementService:
    """Service for managing users with Supabase authentication"""

    async def create_user(
        self, session: AsyncSession, admin_id: UUID, user_data: CreateUserRequest
    ) -> User:
        logger.info(f"Attempting to create user: {user_data.email}")

        # Check if organisation exists
        organisation = await session.get(Organisation, user_data.organisation_id)
        if not organisation:
            raise ValueError(AdminUserErrors.ORGANISATION_NOT_FOUND)

        # Check if user already exists in local DB
        result = await session.execute(select(User).where(User.email == user_data.email))
        existing_user = result.scalars().first()
        if existing_user:
            raise ValueError(AdminUserErrors.USER_ALREADY_EXISTS)

        # Attempt to create user in Supabase
        temp_password = user_auth.generate_temporary_password()

        supabase_user_id = user_auth.create_supabase_user(
            email=user_data.email,
            password=temp_password,
            first_name=user_data.first_name,
            last_name=user_data.last_name,
            user_role=user_data.user_role.value,
        )

        if not supabase_user_id:
            raise ValueError("Failed to create user in authentication system")

        new_user = User(
            **user_data.dict(exclude_none=True),
            supabase_user_id=supabase_user_id,
            created_by_auth_user_id=str(admin_id),
        )

        session.add(new_user)

        history_entry = UserActionHistory(
            user_id=new_user.user_id,
            action=UserEventType.CREATED,
            actor_admin_id=admin_id,
            note="User created by admin.",
            details={"admin_id": str(admin_id)},
        )
        session.add(history_entry)

        await session.commit()
        await session.refresh(new_user)

        # Send the temporary password to the user
        email_sent = await email_service.send_user_creation_email(
            to_email=new_user.email,
            first_name=new_user.first_name,
            temp_password=temp_password,
        )

        if email_sent:
            logger.info(f"Successfully sent creation email to {new_user.email}")
        else:
            logger.warning(
                f"Failed to send creation email to {new_user.email}. Please send credentials manually."
            )

        logger.info(f"Successfully created user {new_user.email} with ID {new_user.user_id}")
        return new_user

    async def get_user(self, session: AsyncSession, user_id: UUID) -> User | None:
        return await session.get(User, user_id)

    async def list_users(
        self,
        session: AsyncSession,
        limit: int,
        cursor: str | None,
        organisation_id: UUID | None,
    ) -> list[User]:
        statement = select(User)
        if organisation_id:
            statement = statement.where(User.organisation_id == organisation_id)

        if cursor:
            ts, user_id = decode_cursor(cursor)
            statement = statement.where(
                (User.created_at < ts) | ((User.created_at == ts) & (User.user_id < user_id))
            )

        statement = statement.order_by(User.created_at.desc(), User.user_id.desc()).limit(limit + 1)
        result = await session.execute(statement)
        return result.scalars().all()

    async def update_user(
        self,
        session: AsyncSession,
        user_id: UUID,
        admin_id: UUID,
        user_data: UpdateUserRequest,
    ) -> User:
        user = await session.get(User, user_id)
        if not user:
            raise ValueError(AdminUserErrors.USER_NOT_FOUND)

        update_data = user_data.dict(exclude_unset=True)
        if not update_data:
            raise ValueError(AdminUserErrors.NO_UPDATE_DATA_PROVIDED)

        for key, value in update_data.items():
            setattr(user, key, value)

        history_entry = UserActionHistory(
            user_id=user.user_id,
            action=UserEventType.UPDATED,
            actor_admin_id=admin_id,
            note="User details updated by admin.",
            details={"updated_fields": list(update_data.keys())},
        )
        session.add(history_entry)

        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user

    async def update_user_admin_status(
        self,
        session: AsyncSession,
        user_id: UUID,
        admin_id: UUID,
        new_admin_status: AdminUserStatus,
        note: str | None,
    ) -> User:
        user = await session.get(User, user_id)
        if not user:
            raise ValueError(AdminUserErrors.USER_NOT_FOUND)

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
        session.add(history_entry)

        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user

    async def list_user_action_history_for_organisation(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> list[UserActionHistory]:
        statement = (
            select(UserActionHistory).join(User).where(User.organisation_id == organisation_id)
        )

        if cursor:
            ts, history_id = decode_cursor(cursor)
            statement = statement.where(
                (UserActionHistory.created_at < ts)
                | ((UserActionHistory.created_at == ts) & (UserActionHistory.id < history_id))
            )

        statement = statement.order_by(
            UserActionHistory.created_at.desc(), UserActionHistory.id.desc()
        ).limit(limit + 1)
        result = await session.execute(statement)
        return result.scalars().all()


# Create a global instance
user_management_service = UserManagementService()
