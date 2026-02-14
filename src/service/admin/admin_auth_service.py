from datetime import UTC, datetime
from typing import Any
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from src.domain.models.admin_models import (
    AdminLoginResponse,
    AdminLoginState,
    PasswordResetAction,
)
from src.domain.models.master_tables import AdminUserStatus, SupabaseAdminStatus, SuperAdmin
from src.infra.auth.supabase_admin_client import supabase_admin_client
from src.infra.config.constant import AdminAuthErrors
from src.infra.security.token_revocation import token_revocation_service
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class AdminAuthServiceError(Exception):
    """Base exception for AdminAuthService."""

    def __init__(self, message: str, error_code: str | None = None):
        self.message = message
        self.error_code = error_code
        super().__init__(message)


class AdminNotFoundError(AdminAuthServiceError):
    """Raised when admin is not found."""

    pass


class AdminNotApprovedError(AdminAuthServiceError):
    """Raised when admin account is not approved."""

    pass


class AdminNotActiveError(AdminAuthServiceError):
    """Raised when admin account is not active."""

    pass


class InvalidCredentialsError(AdminAuthServiceError):
    """Raised when credentials are invalid."""

    pass


class InvalidRecoveryTokenError(AdminAuthServiceError):
    """Raised when recovery token is invalid."""

    pass


class PasswordMismatchError(AdminAuthServiceError):
    """Raised when passwords do not match."""

    pass


class ForgotPasswordResult:
    """Result for forgot password — carries data needed to send the reset email.

    Fields are None when admin not found (anti-enumeration: caller still returns success).
    """

    def __init__(
        self,
        admin_email: str | None = None,
        admin_first_name: str | None = None,
        reset_link: str | None = None,
    ):
        self.admin_email = admin_email
        self.admin_first_name = admin_first_name
        self.reset_link = reset_link


class PendingSetupResult:
    """Result for pending setup login flow."""

    def __init__(self, password_reset_link: str):
        self.state = AdminLoginState.PENDING_SETUP
        self.password_reset_link = password_reset_link


class ActiveLoginResult:
    """Result for successful active user login."""

    def __init__(
        self,
        super_admin: SuperAdmin,
        tokens: dict[str, Any],
        email_verified: bool,
    ):
        self.state = AdminLoginState.ACTIVE
        self.email = super_admin.email
        self.first_name = super_admin.first_name
        self.last_name = super_admin.last_name
        self.role = super_admin.role
        self.created_at = super_admin.created_at
        self.email_verified = email_verified
        self.last_login_at = super_admin.last_login_at
        self.tokens = tokens

    def to_response_data(self) -> AdminLoginResponse:
        """Convert to API response data."""
        return AdminLoginResponse(
            email=self.email,
            first_name=self.first_name,
            last_name=self.last_name,
            role=self.role,
            state=self.state,
            created_at=self.created_at,
            email_verified=self.email_verified,
            last_login_at=self.last_login_at,
            tokens=self.tokens,
        )


class AdminAuthService:
    """Service class for admin authentication and management.

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
            logger.debug("AdminAuthService session close failed", exc_info=True)

    # -------------------------------------------------------------------------
    # Core Authentication Methods
    # -------------------------------------------------------------------------

    async def get_super_admin_by_email(self, email: str) -> SuperAdmin | None:
        """Fetches a super admin from the database by email.

        Args:
            email: The admin's email address

        Returns:
            SuperAdmin if found, None otherwise
        """
        statement = select(SuperAdmin).where(
            typing_cast(ColumnElement[bool], SuperAdmin.email == email)
        )
        result = await self._session.execute(statement)
        return result.scalars().first()

    async def get_super_admin_by_user_id(self, user_id: str) -> SuperAdmin | None:
        """Fetches a super admin from the database by Supabase user ID.

        Args:
            user_id: The Supabase user ID

        Returns:
            SuperAdmin if found, None otherwise
        """
        statement = select(SuperAdmin).where(
            typing_cast(ColumnElement[bool], SuperAdmin.supabase_user_id == user_id)
        )
        result = await self._session.execute(statement)
        return result.scalars().first()

    async def login(self, email: str, password: str) -> PendingSetupResult | ActiveLoginResult:
        """Authenticate a super admin and return login result.

        Args:
            email: Admin email
            password: Admin password

        Returns:
            PendingSetupResult if password change required, ActiveLoginResult otherwise

        Raises:
            AdminNotFoundError: If admin not found
            AdminNotApprovedError: If admin account not approved
            AdminNotActiveError: If admin account not active
            InvalidCredentialsError: If credentials are invalid
        """
        super_admin = await self.get_super_admin_by_email(email)
        if not super_admin:
            logger.warning(f"Super admin not found for email: {email}")
            raise AdminNotFoundError(AdminAuthErrors.USER_NOT_FOUND, error_code="USER_NOT_FOUND")

        # Check admin approval status
        if super_admin.admin_status != AdminUserStatus.APPROVED:
            logger.warning(
                "Login blocked for %s: admin_status=%s",
                super_admin.email,
                super_admin.admin_status,
            )
            token_revocation_service.revoke_user(super_admin.supabase_user_id)
            raise AdminNotApprovedError(
                AdminAuthErrors.ACCOUNT_NOT_APPROVED, error_code="ACCOUNT_NOT_APPROVED"
            )

        # Check Supabase status
        if super_admin.status == SupabaseAdminStatus.INACTIVE:
            logger.warning(
                "Login blocked for %s: supabase status inactive",
                super_admin.email,
            )
            token_revocation_service.revoke_user(super_admin.supabase_user_id)
            raise AdminNotActiveError(
                AdminAuthErrors.ACCOUNT_NOT_ACTIVE, error_code="ACCOUNT_NOT_ACTIVE"
            )

        # Authenticate with Supabase
        try:
            supabase_response = supabase_admin_client.sign_in_with_password(
                email=email, password=password
            )
        except Exception:
            logger.warning(f"Invalid credentials for super admin: {email}")
            raise InvalidCredentialsError(
                AdminAuthErrors.INVALID_CREDENTIALS, error_code="INVALID_CREDENTIALS"
            ) from None

        session_data = supabase_response.get("session", {})
        if not session_data or not session_data.get("access_token"):
            logger.warning(f"Supabase login failed for super admin: {email}")
            raise InvalidCredentialsError(
                AdminAuthErrors.INVALID_CREDENTIALS, error_code="INVALID_CREDENTIALS"
            )

        user_data = supabase_response.get("user", {})
        user_metadata = user_data.get("user_metadata", {})
        requires_password_change = user_metadata.get("requires_password_change", False)

        # Check if user needs to set password
        if requires_password_change:
            return self._handle_pending_setup_login(super_admin.email)

        # Final status check
        if super_admin.status != SupabaseAdminStatus.ACTIVE:
            logger.warning(
                "Login blocked for %s: supabase status %s",
                super_admin.email,
                super_admin.status,
            )
            token_revocation_service.revoke_user(super_admin.supabase_user_id)
            raise AdminNotActiveError(
                AdminAuthErrors.ACCOUNT_NOT_ACTIVE, error_code="ACCOUNT_NOT_ACTIVE"
            )

        tokens = {
            "access_token": session_data.get("access_token"),
            "refresh_token": session_data.get("refresh_token"),
        }
        email_verified = user_metadata.get("email_verified", False)

        return await self._handle_active_user_login(super_admin, tokens, email_verified)

    def _handle_pending_setup_login(self, email: str) -> PendingSetupResult:
        """Handles the login flow for a user who needs to set their password.

        Args:
            email: Admin email

        Returns:
            PendingSetupResult with password reset link

        Raises:
            AdminAuthServiceError: If generating reset link fails
        """
        try:
            password_reset_link = supabase_admin_client.generate_password_reset_link(email)
            return PendingSetupResult(password_reset_link=password_reset_link)
        except Exception as e:
            logger.error(f"Failed to generate recovery token for user {email}: {str(e)}")
            raise AdminAuthServiceError(
                "Failed to generate password change token",
                error_code="RECOVERY_TOKEN_GENERATION_FAILED",
            ) from None

    async def _handle_active_user_login(
        self,
        super_admin: SuperAdmin,
        tokens: dict[str, Any],
        email_verified: bool,
    ) -> ActiveLoginResult:
        """Handles the login flow for an active user.

        Args:
            super_admin: The SuperAdmin entity
            tokens: Authentication tokens from Supabase
            email_verified: Whether email is verified

        Returns:
            ActiveLoginResult with user data and tokens
        """
        logger.info("Updating last_login_at for super_admin: %s", super_admin.email)
        super_admin.last_login_at = datetime.now(UTC)
        self._session.add(super_admin)
        await self._session.commit()
        await self._session.refresh(super_admin)
        logger.info(f"Updated last_login_at for super_admin: {super_admin.email}")

        token_revocation_service.clear_user_revocation(super_admin.supabase_user_id)
        return ActiveLoginResult(super_admin, tokens, email_verified)

    # -------------------------------------------------------------------------
    # Password Reset Methods
    # -------------------------------------------------------------------------

    async def reset_password(
        self,
        token_hash: str,
        new_password: str,
        confirm_password: str | None,
        action: PasswordResetAction,
    ) -> str | None:
        """Reset password using a recovery token.

        Args:
            token_hash: The Supabase recovery token
            new_password: The new password
            confirm_password: Password confirmation (optional)
            action: Whether this is a force change or forgot password reset

        Returns:
            User ID if successful

        Raises:
            PasswordMismatchError: If passwords don't match
            InvalidRecoveryTokenError: If token is invalid
        """
        if not token_hash or not new_password:
            raise AdminAuthServiceError(
                AdminAuthErrors.RECOVERY_TOKEN_REQUIRED,
                error_code="RECOVERY_TOKEN_REQUIRED",
            )

        if confirm_password and new_password != confirm_password:
            raise PasswordMismatchError(
                AdminAuthErrors.PASSWORD_MISMATCH, error_code="PASSWORD_MISMATCH"
            )

        # Reset password via Supabase
        try:
            user_id = supabase_admin_client.reset_password_with_token(
                token_hash=token_hash,
                new_password=new_password,
            )
        except Exception as e:
            logger.error(f"Failed to reset password with recovery token: {str(e)}")
            raise InvalidRecoveryTokenError(
                AdminAuthErrors.INVALID_RECOVERY_TOKEN,
                error_code="INVALID_RECOVERY_TOKEN",
            ) from None

        # Handle force change (first-time password setup)
        if action == PasswordResetAction.FORCE_CHANGE and user_id:
            await self._complete_force_change_setup(user_id)
        else:
            logger.info(
                "Password reset completed for user_id %s (forgot action - no metadata update)",
                user_id,
            )

        # Sign out user after password reset
        if user_id:
            self._sign_out_after_password_reset(user_id)

        return user_id

    async def _complete_force_change_setup(self, user_id: str) -> None:
        """Complete the first-time password setup flow.

        Args:
            user_id: The Supabase user ID
        """
        current_admin = await self.get_super_admin_by_user_id(user_id)
        if not current_admin:
            logger.warning(f"Admin not found for user_id: {user_id}")
            raise AdminNotFoundError(AdminAuthErrors.UNAUTHORIZED, error_code="ADMIN_NOT_FOUND")

        # Update Supabase metadata
        updated_metadata = {
            "requires_password_change": False,
            "email_verified": True,
            "first_name": current_admin.first_name,
            "last_name": current_admin.last_name,
            "role": current_admin.role,
        }
        if current_admin.supabase_user_id:
            supabase_admin_client.update_user_metadata(
                user_id=current_admin.supabase_user_id, metadata=updated_metadata
            )

        # Update local database
        current_admin.status = SupabaseAdminStatus.ACTIVE
        current_admin.email_verified = True
        current_admin.last_login_at = datetime.now(UTC)
        self._session.add(current_admin)
        await self._session.commit()
        logger.info("Local DB updated for user_id: %s", user_id)

    def _sign_out_after_password_reset(self, user_id: str) -> None:
        """Sign out user after password reset.

        Args:
            user_id: The Supabase user ID
        """
        try:
            supabase_admin_client.sign_out_user(user_id=user_id, global_signout=True)
        except Exception as exc:  # noqa: BLE001 - Supabase SDK may raise multiple exceptions
            logger.warning(
                "Global sign-out failed for admin %s after password reset: %s",
                user_id,
                exc,
            )
        token_revocation_service.revoke_user(user_id)

    # -------------------------------------------------------------------------
    # Forgot Password Methods
    # -------------------------------------------------------------------------

    async def forgot_password(self, email: str) -> ForgotPasswordResult:
        """Look up admin and generate a password reset link.

        Returns a ForgotPasswordResult. When the admin is not found the result
        fields are None — caller should still return success to prevent user
        enumeration.
        """
        super_admin = await self.get_super_admin_by_email(email)
        if not super_admin:
            logger.warning(
                "Password reset requested for non-existent user: %s",
                email,
            )
            return ForgotPasswordResult()

        password_reset_link = supabase_admin_client.generate_password_reset_link(
            email=super_admin.email
        )

        return ForgotPasswordResult(
            admin_email=super_admin.email,
            admin_first_name=super_admin.first_name,
            reset_link=password_reset_link,
        )

    # -------------------------------------------------------------------------
    # Logout Methods
    # -------------------------------------------------------------------------

    async def logout(
        self,
        current_admin: SuperAdmin,
        logout_email: str | None,
        global_logout: bool,
        bearer_token: str | None,
    ) -> str:
        """Logout an admin user.

        Args:
            current_admin: The currently authenticated admin
            logout_email: Email of user to logout (for global logout)
            global_logout: Whether to logout from all sessions
            bearer_token: Current bearer token to revoke

        Returns:
            Success message

        Raises:
            AdminAuthServiceError: If email required but not provided
            AdminNotFoundError: If specified user not found
        """
        user_to_logout: SuperAdmin | None = current_admin

        if global_logout:
            if not logout_email:
                raise AdminAuthServiceError(
                    AdminAuthErrors.EMAIL_REQUIRED_FOR_LOGOUT,
                    error_code="EMAIL_REQUIRED",
                )
            user_to_logout = await self.get_super_admin_by_email(logout_email)
            if not user_to_logout:
                raise AdminNotFoundError(
                    AdminAuthErrors.USER_NOT_FOUND, error_code="USER_NOT_FOUND"
                )

        if user_to_logout and user_to_logout.supabase_user_id:
            supabase_admin_client.sign_out_user(
                user_id=user_to_logout.supabase_user_id,
                global_signout=global_logout,
            )
            if global_logout:
                token_revocation_service.revoke_user(user_to_logout.supabase_user_id)
        else:
            raise AdminAuthServiceError(AdminAuthErrors.UNAUTHORIZED, error_code="UNAUTHORIZED")

        if bearer_token:
            token_revocation_service.revoke_token(bearer_token)

        if global_logout:
            return f"Successfully logged out user {user_to_logout.email} from all sessions."
        return "Successfully logged out."

    # -------------------------------------------------------------------------
    # Change Password Methods
    # -------------------------------------------------------------------------

    async def change_password(
        self,
        current_admin: SuperAdmin,
        current_password: str,
        new_password: str,
        bearer_token: str | None,
    ) -> None:
        """Change password for an active admin.

        Args:
            current_admin: The currently authenticated admin
            current_password: Current password for verification
            new_password: New password to set
            bearer_token: Current bearer token to revoke

        Raises:
            InvalidCredentialsError: If current password is incorrect
        """
        # Verify current password
        try:
            supabase_admin_client.sign_in_with_password(
                email=current_admin.email,
                password=current_password,
            )
        except Exception:
            logger.warning("Incorrect current password for user: %s", current_admin.email)
            raise InvalidCredentialsError(
                AdminAuthErrors.INCORRECT_CURRENT_PASSWORD,
                error_code="INCORRECT_CURRENT_PASSWORD",
            ) from None

        # Update password
        supabase_admin_client.update_user_password(
            user_id=current_admin.supabase_user_id,
            new_password=new_password,
        )

        # Invalidate sessions
        self.invalidate_admin_sessions(current_admin, token=bearer_token)
        logger.info("Password changed for %s and existing sessions revoked", current_admin.email)

    # -------------------------------------------------------------------------
    # Session Invalidation Methods
    # -------------------------------------------------------------------------

    def invalidate_admin_sessions(
        self,
        super_admin: SuperAdmin,
        *,
        token: str | None = None,
    ) -> None:
        """Invalidate all sessions for an admin.

        Args:
            super_admin: The admin whose sessions to invalidate
            token: Optional specific token to revoke
        """
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
        """Invalidate all sessions for an admin by user ID.

        Args:
            user_id: The Supabase user ID
        """
        try:
            supabase_admin_client.sign_out_user(user_id=user_id, global_signout=True)
        except Exception as exc:  # noqa: BLE001 - Supabase SDK may raise multiple exceptions
            logger.warning(
                "Global sign-out failed for admin %s: %s",
                user_id,
                exc,
            )
        token_revocation_service.revoke_user(user_id)
