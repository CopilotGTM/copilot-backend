from datetime import datetime, timezone
from typing import Dict, Any
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.utils.http_response import Response
from src.domain.models.admin_models import AdminLoginState
from src.domain.models.master_tables import SuperAdmin
from src.infra.auth.supabase_admin_client import supabase_admin_client
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class AdminAuthService:
    """Service class for admin authentication and management."""

    async def get_super_admin_by_email(
        self, session: AsyncSession, email: str
    ) -> SuperAdmin | None:
        """Fetches a super admin from the database by email."""
        statement = select(SuperAdmin).where(SuperAdmin.email == email)
        result = await session.execute(statement)
        return result.scalars().first()

    def handle_pending_setup_login(self, email: str) -> Response:
        """Handles the login flow for a user who needs to set their password."""
        try:
            password_reset_link = supabase_admin_client.generate_password_reset_link(
                email
            )
            response_data = {
                "state": AdminLoginState.PENDING_SETUP,
                "password_reset_link": password_reset_link,
            }
            return Response.success_response(
                message="Login successful", data=response_data
            )
        except Exception as e:
            logger.error(
                f"Failed to generate recovery token for user {email}: {str(e)}"
            )
            return Response.internal_server_error_response(
                message="Failed to generate password change token"
            )

    async def handle_active_user_login(
        self,
        session: AsyncSession,
        super_admin: SuperAdmin,
        tokens: Dict[str, Any],
        email_verified: bool,
    ) -> Response:
        """Handles the login flow for an active user."""
        logger.info("Updating last_login_at for super_admin: %s", super_admin.email)
        super_admin.last_login_at = datetime.now(timezone.utc)
        session.add(super_admin)
        await session.commit()
        await session.refresh(super_admin)
        logger.info(f"Updated last_login_at for super_admin: {super_admin.email}")

        response_data = {
            "email": super_admin.email,
            "first_name": super_admin.first_name,
            "last_name": super_admin.last_name,
            "role": super_admin.role,
            "state": AdminLoginState.ACTIVE,
            "created_at": super_admin.created_at,
            "email_verified": email_verified,
            "last_login_at": super_admin.last_login_at,
            "tokens": tokens,
        }
        return Response.success_response(message="Login successful", data=response_data)


admin_auth_service = AdminAuthService()
