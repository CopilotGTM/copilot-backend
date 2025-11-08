import secrets
import string
from typing import Any
from typing import cast as typing_cast

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from src.api.dependencies.master_dependencies import get_master_async_session
from src.domain.models.master_tables import (
    AdminRole,
    AdminUserStatus,
    SupabaseAdminStatus,
    SuperAdmin,
)
from src.infra.auth.supabase_admin_client import supabase_admin_client
from src.infra.config.constant import SupabaseErrors
from src.infra.security.token_revocation import token_revocation_service
from src.infra.utils.logger import get_logger

logger = get_logger("admin_auth")


class AdminAuth:
    """Admin authentication and management service using Supabase"""

    async def __call__(
        self,
        request: Request,
        session: AsyncSession = Depends(get_master_async_session),
    ) -> SuperAdmin:
        """
        FastAPI dependency to get current authenticated admin from Supabase token

        Args:
            request: The request object
            session: Database session

        Returns:
            SuperAdmin object

        Raises:
            HTTPException: If not authenticated
        """
        return await self.get_current_admin(request, session)

    def verify_token(self, token: str) -> dict[str, Any]:
        """
        Verify a JWT token and return user information.

        Args:
            token: JWT token to verify

        Returns:
            User information from token
        """
        try:
            # Use Supabase client to verify token
            response = supabase_admin_client.verify_token(token)
            return response
        except Exception as e:
            logger.error(f"Token verification failed: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=SupabaseErrors.INVALID_TOKEN,
            ) from e

    async def get_current_admin(self, request: Request, session: AsyncSession) -> SuperAdmin:
        """
        Get the current authenticated super admin.

        Args:
            request: FastAPI request object
            session: Database session

        Returns:
            SuperAdmin object

        Raises:
            HTTPException: If authentication fails
        """
        try:
            # Get authorization header
            auth_header = request.headers.get("Authorization")
            if not auth_header or not auth_header.startswith("Bearer "):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=SupabaseErrors.MISSING_AUTHORIZATION_HEADER,
                )

            token = auth_header.split(" ")[1]

            # Verify token with Supabase
            user_data = self.verify_token(token)

            # Get user ID from token
            user_id = user_data.get("user", {}).get("id")
            if not user_id:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=SupabaseErrors.MISSING_USER_ID_IN_TOKEN,
                )

            if token_revocation_service.is_token_revoked(token, user_id):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=SupabaseErrors.INVALID_TOKEN,
                )

            # Find admin in database
            statement = select(SuperAdmin).where(
                typing_cast(ColumnElement[bool], SuperAdmin.supabase_user_id == user_id)
            )
            result = await session.execute(statement)
            admin = result.scalars().first()

            if not admin:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=SupabaseErrors.ADMIN_NOT_FOUND,
                )

            # Check if admin is active (unless allow_pending_setup is True)
            if admin.status != SupabaseAdminStatus.ACTIVE:
                token_revocation_service.revoke_user(admin.supabase_user_id)
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=SupabaseErrors.ADMIN_ACCOUNT_NOT_ACTIVE,
                )

            if admin.admin_status != AdminUserStatus.APPROVED:
                token_revocation_service.revoke_user(admin.supabase_user_id)
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=SupabaseErrors.ADMIN_ACCOUNT_NOT_APPROVED,
                )

            return admin

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Authentication error: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=SupabaseErrors.AUTHENTICATION_FAILED,
            ) from e

    def generate_temp_password(self, length: int = 12) -> str:
        """
        Generate a secure temporary password

        Args:
            length: Length of the password

        Returns:
            Generated temporary password
        """
        characters = string.ascii_letters + string.digits + "!@#$%^&*"
        password = "".join(secrets.choice(characters) for _ in range(length))
        return password

    def create_supabase_user(
        self, email: str, password: str, first_name: str, last_name: str
    ) -> str | None:
        """
        Create user in Supabase auth using service role

        Args:
            email: User email
            password: User password
            first_name: User first name
            last_name: User last name

        Returns:
            Supabase user ID if successful, None otherwise
        """
        try:
            user_metadata = {
                "first_name": first_name,
                "last_name": last_name,
                "role": "super_admin",
                "requires_password_change": True,
                "email_verified": True,
            }

            response = supabase_admin_client.create_user(
                email=email, password=password, metadata=user_metadata
            )

            user = response.get("user")
            user_id: str | None = None
            if isinstance(user, dict):
                possible_id = user.get("id")
                if isinstance(possible_id, str):
                    user_id = possible_id
            return user_id

        except Exception as e:
            logger.error(f"Error creating Supabase user: {str(e)}")
            return None

    def update_supabase_user_metadata(
        self, supabase_user_id: str, metadata: dict[str, Any]
    ) -> bool:
        """
        Update user metadata in Supabase

        Args:
            supabase_user_id: Supabase user ID
            metadata: Metadata to update

        Returns:
            True if successful, False otherwise
        """
        try:
            supabase_admin_client.update_user_metadata(user_id=supabase_user_id, metadata=metadata)
            return True
        except Exception as e:
            logger.error(f"Failed to update metadata for user {supabase_user_id}: {str(e)}")
            return False

    async def create_super_admin(
        self, email: str, first_name: str, last_name: str, session: AsyncSession
    ) -> tuple[SuperAdmin, str]:
        """
        Create a new super admin account

        Args:
            email: Admin email
            first_name: Admin first name
            last_name: Admin last name
            session: Database session

        Returns:
            Tuple of (SuperAdmin object, temporary password)

        Raises:
            HTTPException: If admin already exists
        """
        result = await session.execute(
            select(SuperAdmin).where(typing_cast(ColumnElement[bool], SuperAdmin.email == email))
        )
        existing_admin = result.scalars().one_or_none()

        if existing_admin:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=SupabaseErrors.ADMIN_EMAIL_ALREADY_EXISTS,
            )

        temp_password = self.generate_temp_password()
        supabase_user_id = self.create_supabase_user(email, temp_password, first_name, last_name)

        if not supabase_user_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=SupabaseErrors.FAILED_TO_CREATE_SUPABASE_USER,
            )

        admin = SuperAdmin(
            supabase_user_id=supabase_user_id,
            email=email,
            first_name=first_name,
            last_name=last_name,
            role=AdminRole.SUPER_ADMIN,
            status=SupabaseAdminStatus.PENDING_SETUP,
            admin_status=AdminUserStatus.APPROVED,
        )

        session.add(admin)
        await session.commit()
        await session.refresh(admin)

        return admin, temp_password

    async def get_admin_by_supabase_id(
        self, supabase_user_id: str, session: AsyncSession
    ) -> SuperAdmin | None:
        """
        Get admin by Supabase user ID

        Args:
            supabase_user_id: Supabase user ID
            session: Database session

        Returns:
            SuperAdmin object if found, None otherwise
        """
        result = await session.execute(
            select(SuperAdmin).where(
                typing_cast(ColumnElement[bool], SuperAdmin.supabase_user_id == supabase_user_id)
            )
        )
        return result.scalars().one_or_none()

    async def get_admin_by_email(self, email: str, session: AsyncSession) -> SuperAdmin | None:
        """
        Get admin by email

        Args:
            email: Admin email
            session: Database session

        Returns:
            SuperAdmin object if found, None otherwise
        """
        result = await session.execute(
            select(SuperAdmin).where(typing_cast(ColumnElement[bool], SuperAdmin.email == email))
        )
        return result.scalars().one_or_none()

    def check_requires_password_change(self, supabase_user_id: str) -> bool:
        """
        Check if user requires password change

        Args:
            supabase_user_id: Supabase user ID

        Returns:
            True if password change required, False otherwise
        """
        try:
            user_info = supabase_admin_client.get_user_by_id(supabase_user_id)
            user_metadata = user_info.get("user", {}).get("user_metadata", {})
            return bool(user_metadata.get("requires_password_change", False))
        except Exception as e:
            logger.error(f"Error checking password change requirement: {str(e)}")
            return False


# Create instances for different use cases
admin_auth = AdminAuth()
