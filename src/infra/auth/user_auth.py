import secrets
import string

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.master_dependencies import get_master_async_session
from src.domain.models.master_tables import AdminUserStatus, SupabaseUserStatus, User
from src.infra.auth.supabase_user_client import supabase_user_client
from src.infra.config.constant import UserAuthErrors
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

    async def get_current_user(self, request: Request, session: AsyncSession) -> User:
        """
        Get the current authenticated user.

        Args:
            request: FastAPI request object
            session: Database session

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

            # Verify token with Supabase
            user_data = self.supabase_user_client.verify_token(token)

            # Get user ID from token
            user_id = user_data.get("user", {}).get("id")
            if not user_id:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=UserAuthErrors.INVALID_TOKEN_NO_ID,
                )

            # Find user in database
            statement = select(User).where(User.supabase_user_id == user_id)
            result = await session.execute(statement)
            user = result.scalars().first()

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
        except Exception as e:
            logger.error(f"Authentication error: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=UserAuthErrors.AUTHENTICATION_FAILED,
            ) from e

    async def __call__(
        self,
        request: Request,
        session: AsyncSession = Depends(get_master_async_session),
    ) -> User:
        """
        FastAPI dependency to get current authenticated user from Supabase token

        Args:
            request: The request object
            session: Database session

        Returns:
            User object

        Raises:
            HTTPException: If not authenticated
        """
        return await self.get_current_user(request, session)


# Create a global instance
user_auth = UserAuth()
