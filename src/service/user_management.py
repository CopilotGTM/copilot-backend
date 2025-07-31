import secrets
import string
from typing import Optional, Dict, Any, List
from sqlmodel import Session, select
from uuid import UUID
from datetime import datetime

from src.domain.models.master_tables import User, Organisation, UserActionHistory, UserEventType, AdminUserStatus
from src.domain.models.admin_user_models import CreateUserRequest, UpdateUserRequest, UpdateUserStatusRequest
from src.infra.auth.supabase_user_client import supabase_user_client
from src.infra.utils.logger import get_logger
from src.infra.utils.cursor import decode_cursor

logger = get_logger("user_management_service")


class UserManagementService:
    """Service for managing users with Supabase authentication"""

    def __init__(self):
        self.supabase_user_client = supabase_user_client

    def _generate_temporary_password(self, length=12):
        alphabet = string.ascii_letters + string.digits + string.punctuation
        password = "".join(secrets.choice(alphabet) for i in range(length))
        return password

    def create_user(self, session: Session, admin_id: UUID, user_data: CreateUserRequest) -> User:
        logger.info(f"Attempting to create user: {user_data.email}")

        # Check if organisation exists
        organisation = session.get(Organisation, user_data.organisation_id)
        if not organisation:
            raise ValueError("Organisation not found")

        # Check if user already exists in local DB
        existing_user = session.exec(select(User).where(User.email == user_data.email)).first()
        if existing_user:
            raise ValueError("User with this email already exists")

        # Attempt to create user in Supabase (will fail if user already exists)
        temp_password = self._generate_temporary_password()
        user_metadata = {
            "first_name": user_data.first_name,
            "last_name": user_data.last_name,
            "role": user_data.user_role.value,
            "requires_password_change": True,
        }

        try:
            supabase_user = self.supabase_user_client.sign_up(
                email=user_data.email,
                password=temp_password,
                metadata=user_metadata,
            )
            logger.info(f"Successfully created user in Supabase: {supabase_user['user']['id']}")
        except Exception as e:
            logger.error(f"Failed to create user in Supabase for email {user_data.email}: {e}")
            raise

        new_user = User(
            **user_data.dict(exclude_none=True),
            supabase_user_id=supabase_user["user"]["id"],
            created_by_auth_user_id=str(admin_id)
        )

        session.add(new_user)
        
        history_entry = UserActionHistory(
            user_id=new_user.user_id,
            action=UserEventType.CREATED,
            actor_admin_id=admin_id,
            note=f"User created by admin.",
            details={"admin_id": str(admin_id)}
        )
        session.add(history_entry)

        session.commit()
        session.refresh(new_user)

        logger.info(f"Successfully created user {new_user.email} with ID {new_user.user_id}")
        return new_user

    def get_user(self, session: Session, user_id: UUID) -> Optional[User]:
        return session.get(User, user_id)

    def list_users(self, session: Session, limit: int, cursor: Optional[str], organisation_id: Optional[UUID]) -> List[User]:
        statement = select(User)
        if organisation_id:
            statement = statement.where(User.organisation_id == organisation_id)

        if cursor:
            ts, user_id = decode_cursor(cursor)
            statement = statement.where(
                (User.created_at < ts) | ((User.created_at == ts) & (User.user_id < user_id))
            )

        statement = statement.order_by(User.created_at.desc(), User.user_id.desc()).limit(limit + 1)
        return session.exec(statement).all()

    def update_user(self, session: Session, user_id: UUID, admin_id: UUID, user_data: UpdateUserRequest) -> User:
        user = session.get(User, user_id)
        if not user:
            raise ValueError("User not found")

        update_data = user_data.dict(exclude_unset=True)
        if not update_data:
            raise ValueError("No update data provided")

        for key, value in update_data.items():
            setattr(user, key, value)

        history_entry = UserActionHistory(
            user_id=user.user_id,
            action=UserEventType.UPDATED,
            actor_admin_id=admin_id,
            note=f"User details updated by admin.",
            details={"updated_fields": list(update_data.keys())}
        )
        session.add(history_entry)

        session.add(user)
        session.commit()
        session.refresh(user)
        return user

    def update_user_admin_status(self, session: Session, user_id: UUID, admin_id: UUID, new_admin_status: AdminUserStatus, note: Optional[str]) -> User:
        user = session.get(User, user_id)
        if not user:
            raise ValueError("User not found")

        user.admin_status = new_admin_status
        if new_admin_status == AdminUserStatus.APPROVED:
            user.approved_at = datetime.utcnow()
            user.approved_by = admin_id

        history_entry = UserActionHistory(
            user_id=user.user_id,
            action=UserEventType.STATUS_CHANGED,
            actor_admin_id=admin_id,
            note=note or f"Admin status changed to {new_admin_status.value}",
            details={"new_status": new_admin_status.value}
        )
        session.add(history_entry)

        session.add(user)
        session.commit()
        session.refresh(user)
        return user

    def list_user_action_history_for_organisation(self, session: Session, organisation_id: UUID, limit: int, cursor: Optional[str]) -> List[UserActionHistory]:
        statement = select(UserActionHistory).join(User).where(User.organisation_id == organisation_id)
        
        if cursor:
            ts, history_id = decode_cursor(cursor)
            statement = statement.where(
                (UserActionHistory.created_at < ts) | ((UserActionHistory.created_at == ts) & (UserActionHistory.id < history_id))
            )
            
        statement = statement.order_by(UserActionHistory.created_at.desc(), UserActionHistory.id.desc()).limit(limit + 1)
        return session.exec(statement).all()


# Create a global instance
user_management_service = UserManagementService()
