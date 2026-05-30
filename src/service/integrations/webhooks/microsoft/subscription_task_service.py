"""Worker-facing orchestration for Microsoft Graph webhook subscriptions.

Celery task modules should stay thin. This service owns the subscription
workflow that needs database sessions, Microsoft Graph calls, and metadata
persistence while preserving the fresh-session pattern.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.service.integrations.webhooks.microsoft.metadata import MicrosoftSubscriptionKind
from src.service.integrations.webhooks.microsoft.subscription_service import (
    MicrosoftGraphSubscriptionService,
    persist_subscription_metadata,
)

MICROSOFT_WEBHOOK_INTEGRATION_TYPES = (
    IntegrationType.OUTLOOK_CALENDAR,
    IntegrationType.OUTLOOK_EMAIL,
)


def kind_for_integration_type(
    integration_type: IntegrationType,
) -> MicrosoftSubscriptionKind | None:
    """Map Outlook integration type to the corresponding webhook kind."""
    if integration_type == IntegrationType.OUTLOOK_CALENDAR:
        return MicrosoftSubscriptionKind.CALENDAR
    if integration_type == IntegrationType.OUTLOOK_EMAIL:
        return MicrosoftSubscriptionKind.EMAIL
    return None


class MicrosoftGraphSubscriptionTaskService:
    """Coordinate Graph subscription ensure and renewal workflows for workers."""

    def __init__(
        self,
        subscription_service: MicrosoftGraphSubscriptionService | None = None,
    ) -> None:
        self._subscription_service = subscription_service or MicrosoftGraphSubscriptionService()

    async def ensure_subscription(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, Any]:
        """Create or renew subscription metadata for one connected Outlook integration."""
        integration_snapshot = await self._load_integration_snapshot(
            organisation_id=organisation_id,
            integration_id=integration_id,
        )
        if integration_snapshot is None:
            return {
                "status": "skipped",
                "reason": "integration_not_found",
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
            }

        kind = self._kind_for_integration(integration_snapshot)
        if kind is None:
            return {
                "status": "skipped",
                "reason": "unsupported_integration_type",
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
            }

        metadata = await self._subscription_service.ensure_subscription_metadata(
            organisation_id=organisation_id,
            integration=integration_snapshot,
            kind=kind,
        )
        # Re-open the org session after the Graph call. The integration may have
        # been disconnected while Graph was responding, so persistence re-checks.
        persisted = await self._persist_metadata(
            organisation_id=organisation_id,
            integration_id=integration_id,
            metadata=metadata,
        )
        if not persisted:
            return {
                "status": "skipped",
                "reason": "integration_not_found_at_persist",
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
            }

        return {
            "status": "success",
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
            "kind": kind.value,
        }

    async def dispatch_all_subscription_ensures(
        self,
        *,
        dispatch_subscription_ensure: Callable[[UUID, UUID], None],
    ) -> dict[str, Any]:
        """Dispatch one integration-scoped ensure task for every eligible Outlook integration."""
        organisation_ids: list[UUID] = []
        async for session in acquire_master_async_session():
            # Renewal scans only active approved orgs; trial/deleted tenants do
            # not need background subscription fanout.
            statement = select(col(Organisation.organisation_id)).where(
                col(Organisation.is_active).is_(True),
                col(Organisation.status) == OrganisationStatus.APPROVED,
            )
            result = await session.execute(statement)
            organisation_ids = list(result.scalars().all())

        integrations_dispatched = 0
        for organisation_id in organisation_ids:
            integration_ids = await self._load_org_integration_ids(organisation_id)
            for integration_id in integration_ids:
                dispatch_subscription_ensure(organisation_id, integration_id)
                integrations_dispatched += 1

        return {
            "status": "success",
            "orgs_checked": len(organisation_ids),
            "integrations_dispatched": integrations_dispatched,
            "completed_at": datetime.now(UTC).isoformat(),
        }

    async def _load_integration_snapshot(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> KnowledgeIntegration | None:
        """Load one eligible integration and detach a snapshot from the DB session."""
        async for session in acquire_org_async_session(organisation_id):
            # The org filter is the tenant boundary; integration_id is only a
            # route/task parameter and is not trusted by itself.
            statement = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.organisation_id) == organisation_id,
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.integration_type).in_(MICROSOFT_WEBHOOK_INTEGRATION_TYPES),
                col(KnowledgeIntegration.is_active).is_(True),
                col(KnowledgeIntegration.connection_id).isnot(None),
            )
            result = await session.execute(statement)
            integration = result.scalar_one_or_none()
            if integration is None:
                return None
            return self._snapshot_integration(integration)
        return None

    async def _load_org_integration_ids(
        self,
        organisation_id: UUID,
    ) -> list[UUID]:
        """Load eligible integration ids for renewal fan-out."""
        async for session in acquire_org_async_session(organisation_id):
            # All-org renewal fans out by integration, not by org task, so each
            # subscription can fail/retry independently.
            statement = select(col(KnowledgeIntegration.integration_id)).where(
                col(KnowledgeIntegration.organisation_id) == organisation_id,
                col(KnowledgeIntegration.integration_type).in_(MICROSOFT_WEBHOOK_INTEGRATION_TYPES),
                col(KnowledgeIntegration.is_active).is_(True),
                col(KnowledgeIntegration.connection_id).isnot(None),
            )
            result = await session.execute(statement)
            return list(result.scalars().all())
        return []

    async def _persist_metadata(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        metadata: dict[str, Any],
    ) -> bool:
        """Persist subscription metadata only if the integration is still connected."""
        async for session in acquire_org_async_session(organisation_id):
            # Persist only if the same org-scoped integration is still active and
            # connected; this prevents stale renewal work from reviving metadata.
            statement = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.organisation_id) == organisation_id,
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.integration_type).in_(MICROSOFT_WEBHOOK_INTEGRATION_TYPES),
                col(KnowledgeIntegration.is_active).is_(True),
                col(KnowledgeIntegration.connection_id).isnot(None),
            )
            result = await session.execute(statement)
            integration = result.scalar_one_or_none()
            if integration is None:
                return False

            persist_subscription_metadata(integration, metadata)
            session.add(integration)
            await session.commit()
            return True
        return False

    def _kind_for_integration(
        self,
        integration: KnowledgeIntegration,
    ) -> MicrosoftSubscriptionKind | None:
        integration_type = (
            IntegrationType(integration.integration_type)
            if isinstance(integration.integration_type, str)
            else integration.integration_type
        )
        return kind_for_integration_type(integration_type)

    def _snapshot_integration(self, integration: KnowledgeIntegration) -> KnowledgeIntegration:
        """Copy the integration row for provider calls outside the DB session."""
        return KnowledgeIntegration.model_validate(
            {
                "integration_id": integration.integration_id,
                "organisation_id": integration.organisation_id,
                "created_by_user_id": integration.created_by_user_id,
                "integration_type": integration.integration_type,
                "name": integration.name,
                "credentials": dict(integration.credentials or {}),
                "connection_id": integration.connection_id,
                "access_token_expires_at": integration.access_token_expires_at,
                "scopes": list(integration.scopes or []),
                "is_active": integration.is_active,
                "connection_metadata": dict(integration.connection_metadata or {}),
            }
        )


__all__ = [
    "MICROSOFT_WEBHOOK_INTEGRATION_TYPES",
    "MicrosoftGraphSubscriptionTaskService",
    "kind_for_integration_type",
]
