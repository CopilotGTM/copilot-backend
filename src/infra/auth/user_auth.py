import secrets
import string

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from src.domain.models.master_tables import AdminUserStatus, SupabaseUserStatus, User
from src.infra.auth.supabase_user_client import SupabaseAuthError, supabase_user_client
from src.infra.config.constant import UserAuthErrors
from src.infra.database.sessions import master_async_session_scope
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
    ) -> str | None:
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

    async def get_current_user(self, request: Request) -> User:
        """
        Get the current authenticated user.

        Acquires and releases a master DB session internally so the connection
        is returned to the pool immediately after the user lookup — not held
        for the entire request lifetime.

        Args:
            request: FastAPI request object

        Returns:
            User object

        Raises:
            HTTPException: If authentication fails
        """
        try:
            # Get authorization header
            auth_header = request.headers.get("Authorization")
            if not auth_header or not auth_header.startswith("Bearer "):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=UserAuthErrors.MISSING_AUTH_HEADER,
                )

            token = auth_header.split(" ")[1]

            # Verify token with Supabase (external call — no DB session held)
            user_data = self.supabase_user_client.verify_token(token)

            # Get user ID from token
            user_id = user_data.get("user", {}).get("id")
            if not user_id:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=UserAuthErrors.INVALID_TOKEN_NO_ID,
                )

            # Acquire master session, look up user, detach, release
            async with master_async_session_scope() as session:
                statement = select(User).where(User.supabase_user_id == user_id)
                result = await session.execute(statement)
                user = result.scalars().first()
                if user:
                    session.expunge(user)

            if not user:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=UserAuthErrors.USER_NOT_FOUND,
                )

            # Check if user is active
            if (
                user.status != SupabaseUserStatus.ACTIVE
                or user.admin_status != AdminUserStatus.APPROVED
            ):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=UserAuthErrors.ACCOUNT_INACTIVE,
                )

            return user

        except HTTPException:
            raise
        except SupabaseAuthError as e:
            logger.warning(
                "Authentication failed",
                extra={
                    "event": "auth_failure",
                    "auth_operation": "get_current_user",
                    "failure_reason": e.message,
                    "status_code": e.status_code,
                },
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=UserAuthErrors.AUTHENTICATION_FAILED,
            ) from e
        except Exception as e:
            logger.error(
                "Authentication error",
                extra={
                    "event": "auth_failure",
                    "auth_operation": "get_current_user",
                    "failure_reason": str(e),
                },
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=UserAuthErrors.AUTHENTICATION_FAILED,
            ) from e

    async def __call__(self, request: Request) -> User:
        """FastAPI dependency to get current authenticated user from Supabase token."""
        return await self.get_current_user(request)


# Create a global instance
user_auth = UserAuth()


