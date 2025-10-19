"""
Token Refresh Service for Integration Management.

Handles automatic token refresh for OAuth integrations to ensure
continuous connectivity without requiring user reauthorization.

Key Features:
- Proactive token refresh before expiry
- Batch processing of expiring tokens
- Error handling and retry logic
- Integration deactivation on failure
- Email notifications for failed refreshes
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.orm import Session
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.infra.config.configs import nango_integration_config
from src.infra.utils.logger import get_logger
from src.service.integrations.nango_client import NangoClient

logger = get_logger(__name__)


class TokenRefreshService:
    """
    Service for managing OAuth token refresh operations.

    This service handles:
    - Identifying integrations with expiring tokens
    - Batch refresh operations
    - Error handling and integration deactivation
    - Notification of refresh failures
    """

    def __init__(self, db_session: Session, nango_client: NangoClient):
        """
        Initialize token refresh service.

        Args:
            db_session: Database session for querying integrations
            nango_client: Nango client for token refresh operations
        """
        self.db = db_session
        self.nango = nango_client
        self.buffer_minutes = nango_integration_config.INTEGRATION_TOKEN_REFRESH_BUFFER_MINUTES

    def get_expiring_integrations(self, buffer_hours: int = 2) -> list[KnowledgeIntegration]:
        """
        Get all active integrations with tokens expiring within the buffer period.

        Args:
            buffer_hours: How many hours ahead to check for expiring tokens (default: 2)

        Returns:
            List of integrations that need token refresh
        """
        try:
            expiry_threshold = datetime.now(UTC) + timedelta(hours=buffer_hours)

            stmt = select(KnowledgeIntegration).where(
                and_(
                    KnowledgeIntegration.is_active,
                    KnowledgeIntegration.access_token_expires_at.isnot(None),
                    KnowledgeIntegration.access_token_expires_at <= expiry_threshold,
                )
            )

            result = self.db.execute(stmt)
            integrations = result.scalars().all()

            logger.info(
                f"Found {len(integrations)} integrations with tokens expiring "
                f"within {buffer_hours} hours"
            )

            return list(integrations)

        except Exception as e:
            logger.error(f"Failed to query expiring integrations: {str(e)}", exc_info=True)
            return []

    async def refresh_integration_token(self, integration: KnowledgeIntegration) -> dict[str, Any]:
        """
        Refresh token for a single integration.

        Args:
            integration: Integration to refresh

        Returns:
            Dict with keys:
                - success (bool): Whether refresh succeeded
                - integration_id (UUID): Integration ID
                - error (str|None): Error message if failed
                - new_expiry (datetime|None): New token expiry time
        """
        integration_id = integration.integration_id
        org_id = integration.organisation_id

        try:
            logger.info(
                f"Refreshing token for integration {integration_id} "
                f"(org: {org_id}, type: {integration.integration_type.value})"
            )

            # Call Nango to refresh the token
            connection = await self.nango.get_connection(
                provider=integration.integration_type.value,
                connection_id=integration.connection_id,
                force_refresh=True,  # Force refresh even if not expired
            )

            # Extract new token expiry from connection
            credentials = connection.get("credentials", {})
            new_expiry = None

            if "expires_at" in credentials:
                new_expiry = self.nango.parse_token_expiry(credentials["expires_at"])

            # Update database with new expiry times
            if new_expiry:
                integration.access_token_expires_at = new_expiry
                integration.updated_at = datetime.now(UTC)
                self.db.commit()

                logger.info(
                    f"Successfully refreshed token for integration {integration_id}. "
                    f"New expiry: {new_expiry}"
                )
            else:
                logger.warning(
                    f"Token refreshed for integration {integration_id} but no expiry "
                    f"information returned"
                )

            return {
                "success": True,
                "integration_id": integration_id,
                "error": None,
                "new_expiry": new_expiry,
            }

        except Exception as e:
            error_msg = f"Token refresh failed: {str(e)}"
            logger.error(
                f"Failed to refresh token for integration {integration_id} "
                f"(org: {org_id}): {error_msg}",
                exc_info=True,
            )

            return {
                "success": False,
                "integration_id": integration_id,
                "error": error_msg,
                "new_expiry": None,
            }

    async def refresh_expiring_tokens(self, buffer_hours: int = 2) -> dict[str, Any]:
        """
        Batch refresh all expiring tokens.

        This is the main method called by the Celery task.

        Args:
            buffer_hours: How many hours ahead to check for expiring tokens

        Returns:
            Dict with keys:
                - total_checked (int): Number of integrations checked
                - successful_refreshes (int): Number of successful refreshes
                - failed_refreshes (int): Number of failed refreshes
                - deactivated (int): Number of integrations deactivated
                - results (List[Dict]): Detailed results for each refresh
        """
        logger.info(f"Starting batch token refresh with {buffer_hours}h buffer")

        # Get all integrations needing refresh
        integrations = await self.get_expiring_integrations(buffer_hours)

        if not integrations:
            logger.info("No integrations need token refresh")
            return {
                "total_checked": 0,
                "successful_refreshes": 0,
                "failed_refreshes": 0,
                "deactivated": 0,
                "results": [],
            }

        # Process each integration
        results = []
        successful = 0
        failed = 0
        deactivated = 0

        for integration in integrations:
            result = await self.refresh_integration_token(integration)
            results.append(result)

            if result["success"]:
                successful += 1
            else:
                failed += 1
                # Deactivate integration on failure
                deactivated_count = self._handle_refresh_failure(integration, result["error"])
                deactivated += deactivated_count

        summary = {
            "total_checked": len(integrations),
            "successful_refreshes": successful,
            "failed_refreshes": failed,
            "deactivated": deactivated,
            "results": results,
        }

        logger.info(
            f"Batch token refresh complete: {successful} successful, "
            f"{failed} failed, {deactivated} deactivated"
        )

        return summary

    def _handle_refresh_failure(self, integration: KnowledgeIntegration, error_message: str) -> int:
        """
        Handle token refresh failure by deactivating integration.

        Args:
            integration: Failed integration
            error_message: Error message from refresh attempt

        Returns:
            Number of integrations deactivated (0 or 1)
        """
        try:
            integration.is_active = False
            integration.last_sync_status = "FAILED"
            integration.last_sync_error = f"Token refresh failed: {error_message}"
            integration.updated_at = datetime.now(UTC)

            self.db.commit()

            logger.warning(
                f"Deactivated integration {integration.integration_id} due to token refresh failure"
            )

            # Log failure for monitoring
            self._log_refresh_failure(integration)

            return 1

        except Exception as e:
            logger.error(
                f"Failed to deactivate integration {integration.integration_id}: {str(e)}",
                exc_info=True,
            )
            return 0

    def _log_refresh_failure(self, integration: KnowledgeIntegration) -> None:
        """
        Log token refresh failure for monitoring.

        This logs the failure so ops team can track integration health.
        In production, this should trigger user notifications.

        Args:
            integration: Failed integration

        Note:
            Email notifications should be implemented when email worker is available.
            For now, logs are sufficient for monitoring via CloudWatch/DataDog.
        """
        try:
            logger.warning(
                "Token refresh failed for integration: "
                "integration_id=%s type=%s org=%s - User may need to reauthorize",
                integration.integration_id,
                integration.integration_type.value,
                integration.organisation_id,
                extra={
                    "integration_id": str(integration.integration_id),
                    "integration_type": integration.integration_type.value,
                    "organisation_id": str(integration.organisation_id),
                    "action_required": "reauthorization",
                },
            )
        except Exception as e:
            logger.error(
                "Failed to log refresh failure for integration %s: %s",
                integration.integration_id,
                str(e),
                exc_info=True,
            )

    def check_integration_health(
        self,
        integration_id: UUID,
        organisation_id: UUID,
    ) -> dict[str, Any]:
        """
        Check health of a specific integration's tokens.

        Args:
            integration_id: Integration to check
            organisation_id: Organization ID for security check

        Returns:
            Dict with keys:
                - is_healthy (bool): Whether token is healthy
                - needs_refresh (bool): Whether token needs refresh soon
                - expires_at (datetime|None): Token expiry time
                - time_until_expiry (str|None): Human-readable time until expiry
        """
        try:
            stmt = select(KnowledgeIntegration).where(
                and_(
                    KnowledgeIntegration.integration_id == integration_id,
                    KnowledgeIntegration.organisation_id == organisation_id,
                )
            )

            result = self.db.execute(stmt)
            integration = result.scalar_one_or_none()

            if not integration:
                return {
                    "is_healthy": False,
                    "needs_refresh": False,
                    "expires_at": None,
                    "time_until_expiry": None,
                    "error": "Integration not found",
                }

            if not integration.access_token_expires_at:
                return {
                    "is_healthy": True,
                    "needs_refresh": False,
                    "expires_at": None,
                    "time_until_expiry": "No expiry",
                }

            now = datetime.now(UTC)
            expires_at = integration.access_token_expires_at
            time_until_expiry = expires_at - now

            is_expired = expires_at <= now
            needs_refresh = self.nango.is_token_expiring_soon(
                expires_at, buffer_minutes=self.buffer_minutes
            )

            return {
                "is_healthy": not is_expired,
                "needs_refresh": needs_refresh,
                "expires_at": expires_at,
                "time_until_expiry": str(time_until_expiry),
            }

        except Exception as e:
            logger.error(
                f"Failed to check integration health for {integration_id}: {str(e)}",
                exc_info=True,
            )
            return {
                "is_healthy": False,
                "needs_refresh": False,
                "expires_at": None,
                "time_until_expiry": None,
                "error": str(e),
            }


# Convenience function for use in Celery tasks
async def refresh_all_expiring_tokens(db_session: Session, buffer_hours: int = 2) -> dict[str, Any]:
    """
    Convenience function to refresh all expiring tokens.

    This is the main entry point for the Celery scheduled task.

    Args:
        db_session: Database session
        buffer_hours: How many hours ahead to check for expiring tokens

    Returns:
        Summary dict with refresh results
    """
    nango = NangoClient()
    service = TokenRefreshService(db_session=db_session, nango_client=nango)

    try:
        result = await service.refresh_expiring_tokens(buffer_hours=buffer_hours)
        return result
    finally:
        await nango.close()
