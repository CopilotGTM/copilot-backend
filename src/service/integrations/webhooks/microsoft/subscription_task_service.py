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
from sqlalchemy.orm import attributes
from sqlmodel import col
from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.domain.models.organisation_tables.sync import (
    IntegrationSyncState,
    IntegrationType,
    KnowledgeIntegration,
)
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
MICROSOFT_WEBHOOK_PARENT_TYPES = (IntegrationType.MICROSOFT,)


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
        requested_kind: MicrosoftSubscriptionKind | None = None,
        lifecycle_event: str | None = None,
    ) -> dict[str, Any]:
        """Create or renew subscription metadata for one connected Outlook integration."""
        targets = await self._load_subscription_targets(
            organisation_id=organisation_id,
            integration_id=integration_id,
        )
        if not targets:
            return {
                "status": "skipped",
                "reason": "integration_not_found",
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
            }

        completed_kinds: list[str] = []
        for integration_snapshot, kind, sync_state_id in targets:
            if requested_kind is not None and kind != requested_kind:
                continue
            if lifecycle_event:
                metadata = await self._subscription_service.recover_subscription_metadata(
                    organisation_id=organisation_id,
                    integration=integration_snapshot,
                    kind=kind,
                    lifecycle_event=lifecycle_event,
                )
            else:
                metadata = await self._subscription_service.ensure_subscription_metadata(
                    organisation_id=organisation_id,
                    integration=integration_snapshot,
                    kind=kind,
                )
            persisted = await self._persist_metadata(
                organisation_id=organisation_id,
                integration_id=integration_id,
                metadata=metadata,
                sync_state_id=sync_state_id,
            )
            if persisted:
                completed_kinds.append(kind.value)

        response = {
            "status": "success",
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
            "kinds": completed_kinds,
        }
        if len(completed_kinds) == 1:
            response["kind"] = completed_kinds[0]
        return response

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

    async def _load_subscription_targets(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> list[tuple[KnowledgeIntegration, MicrosoftSubscriptionKind, UUID | None]]:
        """Load one eligible integration and detach a snapshot from the DB session."""
        async for session in acquire_org_async_session(organisation_id):
            # The org filter is the tenant boundary; integration_id is only a
            # route/task parameter and is not trusted by itself.
            statement = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.organisation_id) == organisation_id,
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.integration_type).in_(
                    MICROSOFT_WEBHOOK_INTEGRATION_TYPES + MICROSOFT_WEBHOOK_PARENT_TYPES
                ),
                col(KnowledgeIntegration.is_active).is_(True),
                col(KnowledgeIntegration.connection_id).isnot(None),
            )
            result = await session.execute(statement)
            integration = result.scalar_one_or_none()
            if integration is None:
                return []
            integration_type = (
                IntegrationType(integration.integration_type)
                if isinstance(integration.integration_type, str)
                else integration.integration_type
            )
            if integration_type in MICROSOFT_WEBHOOK_INTEGRATION_TYPES:
                kind = self._kind_for_integration(integration)
                return [(self._snapshot_integration(integration), kind, None)] if kind else []

            state_stmt = select(IntegrationSyncState).where(
                col(IntegrationSyncState.integration_id) == integration.integration_id,
                col(IntegrationSyncState.capability).in_(MICROSOFT_WEBHOOK_INTEGRATION_TYPES),
                col(IntegrationSyncState.enabled).is_(True),
            )
            state_result = await session.execute(state_stmt)
            targets: list[tuple[KnowledgeIntegration, MicrosoftSubscriptionKind, UUID | None]] = []
            for state in state_result.scalars().all():
                capability = (
                    IntegrationType(state.capability)
                    if isinstance(state.capability, str)
                    else state.capability
                )
                kind = kind_for_integration_type(capability)
                if kind is None:
                    continue
                targets.append(
                    (
                        self._snapshot_integration(
                            integration,
                            integration_type=capability,
                            connection_metadata=dict(state.webhook_metadata or {}),
                        ),
                        kind,
                        state.sync_state_id,
                    )
                )
            return targets
        return []

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
                col(KnowledgeIntegration.integration_type).in_(
                    MICROSOFT_WEBHOOK_INTEGRATION_TYPES + MICROSOFT_WEBHOOK_PARENT_TYPES
                ),
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
        sync_state_id: UUID | None = None,
    ) -> bool:
        """Persist subscription metadata only if the integration is still connected."""
        async for session in acquire_org_async_session(organisation_id):
            # Persist only if the same org-scoped integration is still active and
            # connected; this prevents stale renewal work from reviving metadata.
            statement = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.organisation_id) == organisation_id,
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.integration_type).in_(
                    MICROSOFT_WEBHOOK_INTEGRATION_TYPES + MICROSOFT_WEBHOOK_PARENT_TYPES
                ),
                col(KnowledgeIntegration.is_active).is_(True),
                col(KnowledgeIntegration.connection_id).isnot(None),
            )
            result = await session.execute(statement)
            integration = result.scalar_one_or_none()
            if integration is None:
                return False

            if sync_state_id:
                state_stmt = select(IntegrationSyncState).where(
                    col(IntegrationSyncState.sync_state_id) == sync_state_id,
                    col(IntegrationSyncState.enabled).is_(True),
                )
                state_result = await session.execute(state_stmt)
                state = state_result.scalar_one_or_none()
                if state is None:
                    return False
                state.webhook_metadata = dict(metadata or {})
                attributes.flag_modified(state, "webhook_metadata")
                session.add(state)
                await session.commit()
                return True

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

    def _snapshot_integration(
        self,
        integration: KnowledgeIntegration,
        *,
        integration_type: IntegrationType | None = None,
        connection_metadata: dict[str, Any] | None = None,
    ) -> KnowledgeIntegration:
        """Copy the integration row for provider calls outside the DB session."""
        return KnowledgeIntegration.model_validate(
            {
                "integration_id": integration.integration_id,
                "organisation_id": integration.organisation_id,
                "created_by_user_id": integration.created_by_user_id,
                "integration_type": integration_type or integration.integration_type,
                "name": integration.name,
                "credentials": dict(integration.credentials or {}),
                "connection_id": integration.connection_id,
                "access_token_expires_at": integration.access_token_expires_at,
                "scopes": list(integration.scopes or []),
                "is_active": integration.is_active,
                "connection_metadata": (
                    dict(connection_metadata)
                    if connection_metadata is not None
                    else dict(integration.connection_metadata or {})
                ),
            }
        )


__all__ = [
    "MICROSOFT_WEBHOOK_INTEGRATION_TYPES",
    "MicrosoftGraphSubscriptionTaskService",
    "kind_for_integration_type",
]
