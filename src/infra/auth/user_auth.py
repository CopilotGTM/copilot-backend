import secrets
import string
from typing import Dict, Any, Optional

from src.infra.auth.supabase_user_client import supabase_user_client
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class UserAuth:
    """User authentication and management service using Supabase"""

    def __init__(self):
        self.supabase_user_client = supabase_user_client

    def generate_temporary_password(self, length: int = 12) -> str:
        """
        Generate a secure temporary password.

        Args:
            length: Length of the password.

        Returns:
            Generated temporary password.
        """
        alphabet = string.ascii_letters + string.digits + string.punctuation
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        return password

    def create_supabase_user(
        self, email: str, password: str, first_name: str, last_name: str, user_role: str
    ) -> Optional[str]:
        """
        Create a user in Supabase auth.

        Args:
            email: User's email.
            password: User's password.
            first_name: User's first name.
            last_name: User's last name.
            user_role: The role assigned to the user.

        Returns:
            Supabase user ID if successful, None otherwise.
        """
        try:
            user_metadata = {
                "first_name": first_name,
                "last_name": last_name,
                "role": user_role,
                "requires_password_change": True,
                "email_verified": True,
            }
            response = self.supabase_user_client.create_user(
                email=email,
                password=password,
                metadata=user_metadata,
            )

            user = response.get("user")
            if not user:
                logger.error(f"Could not extract user from Supabase response for email {email}")
                return None

            user_id = user.get("id")
            if not user_id:
                logger.error(f"Could not extract user ID from Supabase response for email {email}")
                return None

            logger.info(f"Successfully created user in Supabase: {user_id}")
            return user_id
        except Exception as e:
            logger.error(f"Failed to create user in Supabase for email {email}: {e}")
            return None


# Create a global instance
user_auth = UserAuth()