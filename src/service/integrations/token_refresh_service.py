"""
Token Refresh Service for Integration Management.

Handles automatic token refresh for OAuth integrations to ensure
continuous connectivity without requiring user reauthorization.

Uses the "fresh session pattern" - manages its own database sessions
internally to avoid holding connections open during long-running operations.
"""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.infra.config.nango_provider_mapping import to_nango_provider_key
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.nango_client import NangoClient

logger = get_logger(__name__)

# Maximum consecutive refresh failures before deactivating an integration
TOKEN_REFRESH_MAX_FAILURES = 3


class TokenRefreshService:
    """
    Service for managing OAuth token refresh operations.

    Uses the "fresh session pattern":
    - For org-level operations: fetches IDs first, then processes each item with a fresh session
    - This prevents holding database connections open during long-running Nango API calls
    """

    def __init__(self, organisation_id: UUID):
        """
        Initialize token refresh service.

        Args:
            organisation_id: Organization UUID for database session acquisition
        """
        self.organisation_id = organisation_id

    async def refresh_org_tokens_async(self, buffer_hours: int = 2) -> dict[str, Any]:
        """
        Refresh tokens that need refreshing for the organization.

        Refreshes tokens that:
        1. Have no expiry set (NULL) - to populate expiry from Nango
        2. Are expiring within buffer_hours - to refresh before expiration

        Uses fresh session pattern:
        1. Fetch integration IDs needing refresh (close session)
        2. For each integration, open fresh session, refresh token, close session

        Args:
            buffer_hours: How many hours ahead to check for expiring tokens

        Returns:
            Dict with summary of refresh results
        """
        expiry_threshold = datetime.utcnow() + timedelta(hours=buffer_hours)

        logger.info(
            "Checking tokens for org: %s (expiry_threshold=%s)",
            self.organisation_id,
            expiry_threshold.isoformat(),
        )

        # Step 1: Fetch integration IDs that need refresh (then close session)
        # Refresh if: expiry is NULL OR expiry <= threshold
        integration_ids: list[UUID] = []
        async for session in acquire_org_async_session(self.organisation_id):
            stmt = select(col(KnowledgeIntegration.integration_id)).where(
                and_(
                    col(KnowledgeIntegration.is_active) == True,  # noqa: E712
                    col(KnowledgeIntegration.connection_id).isnot(None),
                    or_(
                        col(KnowledgeIntegration.access_token_expires_at).is_(None),
                        col(KnowledgeIntegration.access_token_expires_at) <= expiry_threshold,
                    ),
                )
            )
            result = await session.execute(stmt)
            integration_ids = [row[0] for row in result.all()]
        # Session closed here

        if not integration_ids:
            logger.info("No integrations need token refresh for org: %s", self.organisation_id)
            return {
                "status": "completed",
                "summary": {
                    "total_integrations": 0,
                    "successful": 0,
                    "failed": 0,
                    "deactivated": 0,
                },
                "integration_results": [],
            }

        logger.info(
            "Found %d integration(s) with expiring tokens for org: %s",
            len(integration_ids),
            self.organisation_id,
        )

        # Step 2: Process each integration with a fresh session
        integration_results = []
        successful = 0
        failed = 0
        deactivated = 0

        for integration_id in integration_ids:
            result = await self._refresh_single_integration(integration_id)
            integration_results.append(result)

            if result.get("success"):
                successful += 1
            else:
                failed += 1
                if result.get("deactivated"):
                    deactivated += 1

        # Determine overall status
        if failed == 0:
            status = "completed"
        elif successful == 0:
            status = "failed"
        else:
            status = "partial"

        logger.info(
            "Token refresh complete for org %s: successful=%d failed=%d deactivated=%d",
            self.organisation_id,
            successful,
            failed,
            deactivated,
        )

        return {
            "status": status,
            "summary": {
                "total_integrations": len(integration_ids),
                "successful": successful,
                "failed": failed,
                "deactivated": deactivated,
            },
            "integration_results": integration_results,
        }

    async def refresh_single_token_async(self, integration_id: UUID) -> dict[str, Any]:
        """
        Refresh token for a single integration (for manual/on-demand use).

        Args:
            integration_id: Integration UUID to refresh

        Returns:
            Dict with refresh result
        """
        return await self._refresh_single_integration(integration_id)

    async def _refresh_single_integration(self, integration_id: UUID) -> dict[str, Any]:
        """
        Refresh token for a single integration with fresh session.

        Opens a new session, fetches integration, calls Nango, updates DB, closes session.

        Args:
            integration_id: Integration UUID to refresh

        Returns:
            Dict with refresh result
        """
        nango = NangoClient()
        try:
            async for session in acquire_org_async_session(self.organisation_id):
                # Fetch integration
                stmt = select(KnowledgeIntegration).where(
                    col(KnowledgeIntegration.integration_id) == integration_id
                )
                result = await session.execute(stmt)
                integration = result.scalar_one_or_none()

                if not integration:
                    return {
                        "success": False,
                        "integration_id": str(integration_id),
                        "error": "Integration not found",
                        "deactivated": False,
                    }

                if not integration.connection_id:
                    return {
                        "success": False,
                        "integration_id": str(integration_id),
                        "error": "Integration has no connection_id",
                        "deactivated": False,
                    }

                try:
                    # Get the correct Nango provider key from our mapping
                    integration_type = integration.integration_type
                    if isinstance(integration_type, str):
                        integration_type = IntegrationType(integration_type)
                    provider_key = to_nango_provider_key(integration_type)

                    logger.info(
                        "Refreshing token: org=%s integration=%s type=%s provider=%s",
                        self.organisation_id,
                        integration_id,
                        integration_type.value,
                        provider_key,
                    )

                    # Call Nango to refresh the token
                    connection = await nango.get_connection(
                        provider=provider_key,
                        connection_id=integration.connection_id,
                        force_refresh=True,
                    )

                    # Extract new token expiry
                    credentials = connection.get("credentials", {})
                    new_expiry = None
                    if "expires_at" in credentials:
                        new_expiry = nango.parse_token_expiry(credentials["expires_at"])

                    # Update integration record
                    if new_expiry:
                        integration.access_token_expires_at = new_expiry

                    # Reset failure count on success
                    self._reset_failure_count(integration)
                    integration.updated_at = datetime.utcnow()
                    session.add(integration)
                    await session.commit()

                    logger.info(
                        "Token refreshed: integration=%s new_expiry=%s",
                        integration_id,
                        new_expiry,
                    )

                    return {
                        "success": True,
                        "integration_id": str(integration_id),
                        "new_expiry": new_expiry.isoformat() if new_expiry else None,
                        "deactivated": False,
                    }

                except Exception as e:
                    logger.error(
                        "Token refresh failed: org=%s integration=%s error=%s",
                        self.organisation_id,
                        integration_id,
                        str(e),
                        exc_info=True,
                    )

                    # Increment failure count
                    failure_count = self._increment_failure_count(integration)
                    integration.last_sync_status = "FAILED"
                    integration.last_sync_error = f"Token refresh failed: {str(e)[:200]}"
                    integration.updated_at = datetime.utcnow()

                    # Deactivate after max failures
                    was_deactivated = False
                    if failure_count >= TOKEN_REFRESH_MAX_FAILURES:
                        integration.is_active = False
                        was_deactivated = True
                        logger.warning(
                            "Deactivated integration %s after %d consecutive failures",
                            integration_id,
                            failure_count,
                        )

                    session.add(integration)
                    await session.commit()

                    return {
                        "success": False,
                        "integration_id": str(integration_id),
                        "error": str(e)[:200],
                        "failure_count": failure_count,
                        "deactivated": was_deactivated,
                    }

            # Session not acquired
            return {
                "success": False,
                "integration_id": str(integration_id),
                "error": "Failed to acquire database session",
                "deactivated": False,
            }
        finally:
            await nango.close()

    def _increment_failure_count(self, integration: KnowledgeIntegration) -> int:
        """Increment the failure count and return the new value."""
        metadata = dict(integration.connection_metadata or {})
        current_count = metadata.get("token_refresh_failures", 0)
        new_count = current_count + 1
        metadata["token_refresh_failures"] = new_count
        metadata["last_refresh_failure_at"] = datetime.utcnow().isoformat()
        integration.connection_metadata = metadata
        return new_count

    def _reset_failure_count(self, integration: KnowledgeIntegration) -> None:
        """Reset the failure count on successful refresh."""
        metadata = dict(integration.connection_metadata or {})
        if "token_refresh_failures" in metadata:
            del metadata["token_refresh_failures"]
        if "last_refresh_failure_at" in metadata:
            del metadata["last_refresh_failure_at"]
        integration.connection_metadata = metadata
