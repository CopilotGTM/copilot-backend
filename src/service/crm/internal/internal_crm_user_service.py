"""Internal CRM user mapping service.

For organizations using the internal CRM (no HubSpot/Salesforce), this service
manages the sync of app users (public.users) to CRM users (org_*.crm_users).

MVP Flow:
- On first deal creation with owner, bulk sync ALL org users to crm_users
- Subsequent operations only query crm_users table (org schema)

Future Flow (admin-controlled):
- Admin manually manages which users are available in CRM
- If user not in crm_users, return error prompting admin to add users
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.master_tables import SupabaseUserStatus, User
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.infra.utils.logger import get_logger
from src.service.crm.access.crm_user_service import build_user_display_name
from src.service.crm.internal.internal_crm_setup_service import InternalCRMSetupService

logger = get_logger(__name__)


class InternalCRMUserServiceError(Exception):
    """Custom exception for internal CRM user errors."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class InternalCRMUserService:
    """Manages CRM user records for internal CRM (no external CRM integration).

    This service syncs app users from public.users to the org's crm_users table,
    allowing them to be assigned as deal owners.
    """

    # MVP: Auto-sync users when none exist. Set to False for admin-controlled flow.
    AUTO_SYNC_ENABLED = True

    def __init__(
        self,
        org_session: AsyncSession,
        master_session: AsyncSession,
        organisation_id: UUID,
        actor_user_id: UUID,
    ):
        """Initialize the service with both org and master sessions.

        Args:
            org_session: Org-scoped session for CRM user operations (org schema).
            master_session: Master session for reading public.users (initial sync only).
            organisation_id: Organisation UUID.
            actor_user_id: User UUID performing the action.
        """
        self.session = org_session
        self.master_session = master_session
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id
        self._integration_id: UUID | None = None

    async def _get_internal_integration_id(self) -> UUID:
        """Get the internal CRM integration ID (cached)."""
        if self._integration_id:
            return self._integration_id

        setup_service = InternalCRMSetupService(
            self.session, self.organisation_id, self.actor_user_id
        )
        integration = await setup_service.get_internal_integration()
        if not integration:
            raise InternalCRMUserServiceError("Internal CRM is not configured", status_code=409)
        self._integration_id = integration.integration_id
        return self._integration_id

    async def has_crm_users(self) -> bool:
        """Check if any CRM users exist for the internal CRM integration."""
        integration_id = await self._get_internal_integration_id()
        stmt = (
            select(func.count())
            .select_from(CRMUser)
            .where(
                col(CRMUser.organisation_id) == self.organisation_id,
                col(CRMUser.integration_id) == integration_id,
                col(CRMUser.is_deleted).is_not(True),
            )
        )
        result = await self.session.execute(stmt)
        count = result.scalar() or 0
        return count > 0

    async def get_crm_user_by_app_user_id(self, user_id: UUID) -> CRMUser | None:
        """Look up a CRM user by their app user ID (external_id)."""
        integration_id = await self._get_internal_integration_id()
        stmt = select(CRMUser).where(
            col(CRMUser.organisation_id) == self.organisation_id,
            col(CRMUser.integration_id) == integration_id,
            col(CRMUser.external_id) == str(user_id),
            col(CRMUser.is_deleted).is_not(True),
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def sync_org_users_to_crm(self) -> int:
        """Bulk sync all organisation users from public.users to crm_users.

        This creates or updates CRM user records for all app users in the
        organisation regardless of their activation or approval status.
        Used for initial setup or refresh.

        Returns:
            Number of users synced.
        """
        integration_id = await self._get_internal_integration_id()

        # Fetch all org users from public.users (master session).
        # No status filter — allow any existing user to be a deal owner even if
        # their Supabase account is still pending setup or admin approval is pending.
        # This is intentional; we want CSV import to succeed when the owner email
        # belongs to a real org member regardless of activation state.
        stmt = select(User).where(
            col(User.organisation_id) == self.organisation_id,
        )
        result = await self.master_session.execute(stmt)
        app_users = list(result.scalars().all())

        if not app_users:
            logger.info("No users to sync for org %s", self.organisation_id)
            return 0

        # Get existing CRM users for this integration
        existing_stmt = select(CRMUser).where(
            col(CRMUser.organisation_id) == self.organisation_id,
            col(CRMUser.integration_id) == integration_id,
        )
        existing_result = await self.session.execute(existing_stmt)
        existing_crm_users = {cu.external_id: cu for cu in existing_result.scalars().all()}

        now = datetime.now(UTC)
        synced_count = 0

        for app_user in app_users:
            external_id = str(app_user.user_id)
            crm_user = existing_crm_users.get(external_id)

            if not crm_user:
                crm_user = CRMUser(
                    organisation_id=self.organisation_id,
                    integration_id=integration_id,
                    external_id=external_id,
                )

            # Update fields from app user
            crm_user.email = str(app_user.email) if app_user.email else None
            crm_user.first_name = app_user.first_name
            crm_user.last_name = app_user.last_name
            crm_user.full_name = build_user_display_name(app_user)
            crm_user.user_role = (
                app_user.user_role.value if hasattr(app_user.user_role, "value") else None
            )
            crm_user.job_title = app_user.job_title
            crm_user.is_active = app_user.status == SupabaseUserStatus.ACTIVE
            crm_user.source_created_at = crm_user.source_created_at or now
            crm_user.source_modified_at = now
            crm_user.synced_at = now
            crm_user.last_seen_at = now
            crm_user.is_deleted = False

            self.session.add(crm_user)
            synced_count += 1

        await self.session.flush()
        logger.info(
            "Synced %d users to internal CRM for org %s",
            synced_count,
            self.organisation_id,
        )
        return synced_count

    async def ensure_crm_user(self, user_id: UUID) -> CRMUser:
        """Ensure the app user exists as a CRM user and return it.

        MVP Flow:
        1. Check if user exists in crm_users
        2. If not found and no CRM users exist → bulk sync all org users
        3. Look up user again
        4. If still not found → error

        Future Flow (when AUTO_SYNC_ENABLED=False):
        1. Check if user exists in crm_users
        2. If not found → error (admin must add users first)

        Args:
            user_id: App user UUID (from public.users)

        Returns:
            CRMUser record for the user

        Raises:
            InternalCRMUserServiceError: If user not found or not available
        """
        # Step 1: Check if user already exists in crm_users
        crm_user = await self.get_crm_user_by_app_user_id(user_id)
        if crm_user:
            return crm_user

        # Step 2: User not found - check if we should auto-sync
        if self.AUTO_SYNC_ENABLED:
            # Check if ANY CRM users exist
            has_users = await self.has_crm_users()
            if not has_users:
                # First time - bulk sync all org users
                logger.info(
                    "No CRM users found for org %s, triggering initial sync",
                    self.organisation_id,
                )
                await self.sync_org_users_to_crm()

                # Try to find the user again
                crm_user = await self.get_crm_user_by_app_user_id(user_id)
                if crm_user:
                    return crm_user

        # Step 3: User still not found - return appropriate error
        if self.AUTO_SYNC_ENABLED:
            # User might be new (added after initial sync) or inactive
            raise InternalCRMUserServiceError(
                "User not found in CRM. The user may be inactive or was added "
                "after CRM setup. Please contact support.",
                status_code=404,
            )
        else:
            # Admin-controlled mode
            raise InternalCRMUserServiceError(
                "User not available in CRM. Please add the user via CRM Settings "
                "before assigning them to deals.",
                status_code=400,
            )
