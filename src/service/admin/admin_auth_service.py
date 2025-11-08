from datetime import UTC, datetime
from typing import Any
from typing import cast as typing_cast

from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from src.api.utils.http_response import Response
from src.domain.models.admin_models import AdminLoginState
from src.domain.models.master_tables import SuperAdmin
from src.infra.auth.supabase_admin_client import supabase_admin_client
from src.infra.security.token_revocation import token_revocation_service
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class AdminAuthService:
    """Service class for admin authentication and management."""

    async def get_super_admin_by_email(
        self, session: AsyncSession, email: str
    ) -> SuperAdmin | None:
        """Fetches a super admin from the database by email."""
        statement = select(SuperAdmin).where(
            typing_cast(ColumnElement[bool], SuperAdmin.email == email)
        )
        result = await session.execute(statement)
        return result.scalars().first()

    def handle_pending_setup_login(self, email: str) -> JSONResponse:
        """Handles the login flow for a user who needs to set their password."""
        try:
            password_reset_link = supabase_admin_client.generate_password_reset_link(email)
            response_data = {
                "state": AdminLoginState.PENDING_SETUP,
                "password_reset_link": password_reset_link,
            }
            return Response.success_response(message="Login successful", data=response_data)
        except Exception as e:
            logger.error(f"Failed to generate recovery token for user {email}: {str(e)}")
            return Response.internal_server_error_response(
                message="Failed to generate password change token"
            )

    async def handle_active_user_login(
        self,
        session: AsyncSession,
        super_admin: SuperAdmin,
        tokens: dict[str, Any],
        email_verified: bool,
    ) -> JSONResponse:
        """Handles the login flow for an active user."""
        logger.info("Updating last_login_at for super_admin: %s", super_admin.email)
        super_admin.last_login_at = datetime.now(UTC)
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
        token_revocation_service.clear_user_revocation(super_admin.supabase_user_id)
        return Response.success_response(message="Login successful", data=response_data)

    def invalidate_admin_sessions(
        self,
        super_admin: SuperAdmin,
        *,
        token: str | None = None,
    ) -> None:
        if not super_admin.supabase_user_id:
            return
        try:
            supabase_admin_client.sign_out_user(
                user_id=super_admin.supabase_user_id,
                global_signout=True,
            )
        except Exception as exc:  # noqa: BLE001 - Supabase SDK may raise multiple exceptions
            logger.warning(
                "Global sign-out failed for admin %s: %s",
                super_admin.email,
                exc,
            )
        if token:
            token_revocation_service.revoke_token(token)
        token_revocation_service.revoke_user(super_admin.supabase_user_id)

    def invalidate_admin_sessions_by_user_id(self, user_id: str) -> None:
        try:
            supabase_admin_client.sign_out_user(user_id=user_id, global_signout=True)
        except Exception as exc:  # noqa: BLE001 - Supabase SDK may raise multiple exceptions
            logger.warning(
                "Global sign-out failed for admin %s: %s",
                user_id,
                exc,
            )
        token_revocation_service.revoke_user(user_id)


admin_auth_service = AdminAuthService()
