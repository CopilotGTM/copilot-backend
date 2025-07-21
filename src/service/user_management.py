from typing import Optional, Dict, Any
from sqlmodel import Session, select
from uuid import UUID, uuid4

from src.domain.models.db_models import User, Organisation
from src.infra.auth.supabase_auth import supabase_auth
from src.infra.utils.logger import get_logger

logger = get_logger()


class UserManagementService:
    """Service for managing users with Supabase authentication"""

    def __init__(self):
        self.supabase_auth = supabase_auth

    def create_user_from_supabase(
        self, supabase_user_data: Dict[str, Any], session: Session, organisation_id: Optional[UUID] = None
    ) -> Optional[User]:
        """
        Create a new user in the database from Supabase user data.

        Args:
            supabase_user_data: User data from Supabase
            session: Database session
            organisation_id: Optional organisation ID to assign user to

        Returns:
            Created User object or None if creation failed
        """
        try:
            # Extract user information from Supabase data
            supabase_user_id = supabase_user_data.get("id")
            email = supabase_user_data.get("email")

            if not supabase_user_id or not email:
                logger.error("Missing required user data from Supabase")
                return None

            # Check if user already exists
            existing_user = session.exec(select(User).where(User.supabase_user_id == supabase_user_id)).one_or_none()

            if existing_user:
                logger.info("User already exists: %s", existing_user.user_id)
                return existing_user

            # If no organisation_id provided, create a default one or assign to existing
            if not organisation_id:
                organisation_id = self._get_or_create_default_organisation(session)

            # Create new user
            new_user = User(
                supabase_user_id=supabase_user_id,
                email=email,
                first_name=supabase_user_data.get("user_metadata", {}).get("first_name", ""),
                last_name=supabase_user_data.get("user_metadata", {}).get("last_name", ""),
                organisation_role="user",  # Default role
                organisation_id=organisation_id,
                is_active=True,
            )

            session.add(new_user)
            session.commit()
            session.refresh(new_user)

            logger.info("Created new user: %s", new_user.user_id)
            return new_user

        except Exception as e:
            logger.error("Failed to create user from Supabase data: %s", str(e))
            session.rollback()
            return None

    def _get_or_create_default_organisation(self, session: Session) -> UUID:
        """
        Get or create a default organisation for new users.
        This is a placeholder - implement your business logic here.
        """
        try:
            # Try to find an existing default organisation
            default_org = session.exec(
                select(Organisation).where(Organisation.organisation_name == "Default Organisation")
            ).one_or_none()

            if default_org:
                return default_org.organisation_id

            # Create a new default organisation
            new_org = Organisation(organisation_name="Default Organisation", is_active=True)

            session.add(new_org)
            session.commit()
            session.refresh(new_org)

            logger.info("Created default organisation: %s", new_org.organisation_id)
            return new_org.organisation_id

        except Exception as e:
            logger.error("Failed to create default organisation: %s", str(e))
            session.rollback()
            # Return a fallback UUID - you might want to handle this differently
            return uuid4()

    def get_user_by_supabase_id(self, supabase_user_id: str, session: Session) -> Optional[User]:
        """
        Get user by Supabase user ID.

        Args:
            supabase_user_id: Supabase user ID
            session: Database session

        Returns:
            User object if found, None otherwise
        """
        try:
            user = session.exec(select(User).where(User.supabase_user_id == supabase_user_id)).one_or_none()

            return user

        except Exception as e:
            logger.error("Failed to get user by Supabase ID: %s", str(e))
            return None

    def update_user_last_login(self, user_id: UUID, session: Session) -> bool:
        """
        Update user's last login timestamp.

        Args:
            user_id: User ID
            session: Database session

        Returns:
            True if successful, False otherwise
        """
        try:
            user = session.exec(select(User).where(User.user_id == user_id)).one_or_none()

            if not user:
                logger.warning("User not found for last login update: %s", user_id)
                return False

            from datetime import datetime

            user.last_login_at = datetime.utcnow()
            session.commit()

            logger.info("Updated last login for user: %s", user_id)
            return True

        except Exception as e:
            logger.error("Failed to update user last login: %s", str(e))
            session.rollback()
            return False

    def assign_user_to_organisation(self, user_id: UUID, organisation_id: UUID, session: Session) -> bool:
        """
        Assign a user to an organisation.

        Args:
            user_id: User ID
            organisation_id: Organisation ID
            session: Database session

        Returns:
            True if successful, False otherwise
        """
        try:
            user = session.exec(select(User).where(User.user_id == user_id)).one_or_none()

            if not user:
                logger.warning("User not found: %s", user_id)
                return False

            # Verify organisation exists
            org = session.exec(
                select(Organisation).where(Organisation.organisation_id == organisation_id)
            ).one_or_none()

            if not org:
                logger.warning("Organisation not found: %s", organisation_id)
                return False

            user.organisation_id = organisation_id
            session.commit()

            logger.info("Assigned user %s to organisation %s", user_id, organisation_id)
            return True

        except Exception as e:
            logger.error("Failed to assign user to organisation: %s", str(e))
            session.rollback()
            return False


# Create a global instance
user_management_service = UserManagementService()
