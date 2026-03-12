"""
Custom Token Refresh Service for Integration Management.

Handles automatic token refresh for OAuth integrations using our first-party
provider implementations instead of Nango.

Uses the "fresh session pattern" - manages its own database sessions
internally to avoid holding connections open during long-running operations.
"""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlmodel import col

from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.providers.provider_factory import ProviderFactory

logger = get_logger(__name__)

# Maximum consecutive refresh failures before deactivating an integration
TOKEN_REFRESH_MAX_FAILURES = 3


class CustomTokenRefreshService:
    """
    Service for managing OAuth token refresh operations with custom providers.

    Uses the "fresh session pattern":
    - For org-level operations: fetches IDs first, then processes each item with a fresh session
    - This prevents holding database connections open during long-running provider API calls
    """

    def __init__(self, organisation_id: UUID):
        self.organisation_id = organisation_id

    async def refresh_org_tokens_async(self, buffer_hours: int = 2) -> dict[str, Any]:
        """
        Refresh tokens that need refreshing for the organization.

        Refreshes tokens that:
        1. Have no expiry set (NULL)
        2. Are expiring within buffer_hours
        """
        expiry_threshold = datetime.utcnow() + timedelta(hours=buffer_hours)

        logger.info(
            "Checking custom-provider tokens for org: %s (expiry_threshold=%s)",
            self.organisation_id,
            expiry_threshold.isoformat(),
        )

        integration_ids: list[UUID] = []
        async for session in acquire_org_async_session(self.organisation_id):
            stmt = select(col(KnowledgeIntegration.integration_id)).where(
                and_(
                    col(KnowledgeIntegration.is_active) == True,  # noqa: E712
                    col(KnowledgeIntegration.connection_id).isnot(None),
                    col(KnowledgeIntegration.integration_type)
                    != IntegrationType.INTERNAL_CRM.value,
                    or_(
                        col(KnowledgeIntegration.access_token_expires_at).is_(None),
                        col(KnowledgeIntegration.access_token_expires_at) <= expiry_threshold,
                    ),
                )
            )
            result = await session.execute(stmt)
            integration_ids = [row[0] for row in result.all()]

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
        """Refresh token for a single integration (for manual/on-demand use)."""
        return await self._refresh_single_integration(integration_id)

    async def _refresh_single_integration(self, integration_id: UUID) -> dict[str, Any]:
        """
        Refresh token for a single integration with fresh session.

        Opens a new session, fetches integration, calls the custom provider,
        updates DB, and closes the session.
        """
        async for session in acquire_org_async_session(self.organisation_id):
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

            integration_type = integration.integration_type
            if isinstance(integration_type, str):
                integration_type = IntegrationType(integration_type)

            if integration_type == IntegrationType.INTERNAL_CRM:
                return {
                    "success": True,
                    "integration_id": str(integration_id),
                    "skipped": True,
                    "reason": "internal_crm_no_refresh",
                    "deactivated": False,
                }

            credentials = dict(integration.credentials or {})
            refresh_token = credentials.get("refresh_token")
            if not isinstance(refresh_token, str) or not refresh_token:
                return {
                    "success": False,
                    "integration_id": str(integration_id),
                    "error": "Integration has no refresh_token",
                    "deactivated": False,
                }

            try:
                provider = ProviderFactory.get_provider(integration_type)

                logger.info(
                    "Refreshing token via custom provider: org=%s integration=%s type=%s provider=%s",
                    self.organisation_id,
                    integration_id,
                    integration_type.value,
                    provider.provider_key,
                )

                token_payload = await provider.refresh_token(refresh_token)
                access_token = token_payload.get("access_token")
                if not isinstance(access_token, str) or not access_token:
                    raise ValueError("Provider refresh response missing access_token")

                self._apply_refreshed_token_payload(
                    integration=integration,
                    existing_credentials=credentials,
                    token_payload=token_payload,
                )

                self._reset_failure_count(integration)
                integration.updated_at = datetime.utcnow()
                session.add(integration)
                await session.commit()

                logger.info(
                    "Token refreshed: integration=%s new_expiry=%s",
                    integration_id,
                    integration.access_token_expires_at,
                )

                return {
                    "success": True,
                    "integration_id": str(integration_id),
                    "new_expiry": (
                        integration.access_token_expires_at.isoformat()
                        if integration.access_token_expires_at
                        else None
                    ),
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

                failure_count = self._increment_failure_count(integration)
                integration.last_sync_status = "FAILED"
                integration.last_sync_error = f"Token refresh failed: {str(e)[:200]}"
                integration.updated_at = datetime.utcnow()

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

        return {
            "success": False,
            "integration_id": str(integration_id),
            "error": "Failed to acquire database session",
            "deactivated": False,
        }

    @staticmethod
    def _parse_token_expiry(expires_value: int | str | None) -> datetime | None:
        if expires_value is None:
            return None
        if isinstance(expires_value, str):
            if "T" in expires_value:
                return datetime.fromisoformat(expires_value.replace("Z", "+00:00")).replace(
                    tzinfo=None
                )
            return datetime.utcnow() + timedelta(seconds=int(expires_value))
        return datetime.utcnow() + timedelta(seconds=expires_value)

    @staticmethod
    def _parse_scopes(
        token_payload: dict[str, Any],
        fallback: list[str] | None = None,
    ) -> list[str] | None:
        scopes = token_payload.get("scope")
        if isinstance(scopes, str):
            parsed = [scope for scope in scopes.split(" ") if scope]
            return parsed or fallback
        if isinstance(scopes, list):
            parsed = [scope for scope in scopes if isinstance(scope, str) and scope]
            return parsed or fallback
        return fallback

    def _apply_refreshed_token_payload(
        self,
        *,
        integration: KnowledgeIntegration,
        existing_credentials: dict[str, Any],
        token_payload: dict[str, Any],
    ) -> None:
        merged_credentials = dict(existing_credentials)
        merged_credentials.update(token_payload)

        if "refresh_token" not in token_payload and existing_credentials.get("refresh_token"):
            merged_credentials["refresh_token"] = existing_credentials["refresh_token"]

        integration.credentials = merged_credentials
        integration.access_token_expires_at = self._parse_token_expiry(
            merged_credentials.get("expires_at") or merged_credentials.get("expires_in")
        )
        integration.refresh_token_expires_at = self._parse_token_expiry(
            merged_credentials.get("refresh_token_expires_at")
            or merged_credentials.get("refresh_token_expires_in")
        )
        integration.scopes = self._parse_scopes(merged_credentials, integration.scopes)

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
