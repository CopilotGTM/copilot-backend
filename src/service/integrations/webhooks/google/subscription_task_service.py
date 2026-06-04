"""Worker-facing orchestration for Google webhook subscriptions.

Called by Celery task adapters for post-connect subscription creation and
scheduled renewal. This layer owns DB loading/persistence so the subscription
service can perform Google API calls without holding tenant sessions.
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
from src.infra.config.google_integration_scopes import GOOGLE_SCOPE_GMAIL_READONLY
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.service.integrations.scope_validation import is_required_scope_satisfied
from src.service.integrations.webhooks.google.metadata import (
    GOOGLE_WEBHOOK_SUBSCRIPTIONS_KEY,
    GoogleSubscriptionKind,
)
from src.service.integrations.webhooks.google.subscription_service import (
    GoogleSubscriptionService,
    persist_subscription_metadata,
)

GOOGLE_WEBHOOK_INTEGRATION_TYPES = (
    IntegrationType.GMAIL,
    IntegrationType.GOOGLE_CALENDAR,
)
GOOGLE_WEBHOOK_PARENT_TYPES = (IntegrationType.GOOGLE,)


def kind_for_integration_type(integration_type: IntegrationType) -> GoogleSubscriptionKind | None:
    """Map Google integration type to the corresponding webhook kind."""
    if integration_type == IntegrationType.GMAIL:
        return GoogleSubscriptionKind.GMAIL
    if integration_type == IntegrationType.GOOGLE_CALENDAR:
        return GoogleSubscriptionKind.CALENDAR
    return None


class GoogleSubscriptionTaskService:
    """Coordinate Google subscription ensure and renewal workflows for workers."""

    def __init__(self, subscription_service: GoogleSubscriptionService | None = None) -> None:
        self._subscription_service = subscription_service or GoogleSubscriptionService()

    async def ensure_subscription(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, Any]:
        """Create or refresh subscription metadata for one connected Google integration.

        The task loads a detached snapshot first, calls Google outside the DB
        session, then reopens the tenant session to persist metadata. This
        ordering prevents slow provider calls from pinning pooled connections.
        """
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
        skipped_for_missing_scope = False
        for integration_snapshot, kind, sync_state_id in targets:
            if not self._can_ensure_subscription(integration_snapshot, kind):
                skipped_for_missing_scope = True
                continue
            metadata = await self._subscription_service.ensure_subscription_metadata(
                organisation_id=organisation_id,
                integration=integration_snapshot,
                kind=kind,
            )
            persisted = await self._persist_metadata(
                organisation_id=organisation_id,
                integration_id=integration_id,
                metadata=metadata,
                kind=kind,
                sync_state_id=sync_state_id,
            )
            if persisted:
                completed_kinds.append(kind.value)

        if not completed_kinds:
            reason = (
                "gmail_read_scope_missing"
                if skipped_for_missing_scope
                else "integration_not_found_at_persist"
            )
            return {
                "status": "skipped",
                "reason": reason,
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
            }

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
        """Dispatch one integration-scoped ensure task for eligible Google integrations.

        Scheduled renewal fans out lightweight ensure tasks instead of renewing
        inline, keeping the coordinator short-lived and retry boundaries per
        integration.
        """
        organisation_ids: list[UUID] = []
        async for session in acquire_master_async_session():
            # Only approved active orgs receive renewal work; this mirrors other
            # all-org integration fanout and avoids touching suspended tenants.
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
    ) -> list[tuple[KnowledgeIntegration, GoogleSubscriptionKind, UUID | None]]:
        """Load one eligible integration and detach a snapshot from the DB session."""
        async for session in acquire_org_async_session(organisation_id):
            # The organisation filter is the tenant boundary; `integration_id`
            # alone can be externally supplied by an enqueue or repair call.
            statement = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.organisation_id) == organisation_id,
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.integration_type).in_(
                    GOOGLE_WEBHOOK_INTEGRATION_TYPES + GOOGLE_WEBHOOK_PARENT_TYPES
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
            if integration_type in GOOGLE_WEBHOOK_INTEGRATION_TYPES:
                kind = self._kind_for_integration(integration)
                return [(self._snapshot_integration(integration), kind, None)] if kind else []

            state_stmt = select(IntegrationSyncState).where(
                col(IntegrationSyncState.integration_id) == integration.integration_id,
                col(IntegrationSyncState.capability).in_(GOOGLE_WEBHOOK_INTEGRATION_TYPES),
                col(IntegrationSyncState.enabled).is_(True),
            )
            state_result = await session.execute(state_stmt)
            targets: list[tuple[KnowledgeIntegration, GoogleSubscriptionKind, UUID | None]] = []
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

    async def _load_org_integration_ids(self, organisation_id: UUID) -> list[UUID]:
        """Load eligible Google integration ids for renewal fanout."""
        async for session in acquire_org_async_session(organisation_id):
            # Renewal only targets active connected integrations. Disconnected
            # rows keep their metadata for audit but should not renew watches.
            statement = select(col(KnowledgeIntegration.integration_id)).where(
                col(KnowledgeIntegration.organisation_id) == organisation_id,
                col(KnowledgeIntegration.integration_type).in_(
                    GOOGLE_WEBHOOK_INTEGRATION_TYPES + GOOGLE_WEBHOOK_PARENT_TYPES
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
        kind: GoogleSubscriptionKind,
        sync_state_id: UUID | None = None,
    ) -> bool:
        """Persist subscription metadata only if the integration is still connected."""
        async for session in acquire_org_async_session(organisation_id):
            # Recheck connection state after the provider call. A user may
            # disconnect while Google is responding, and stale metadata must not
            # resurrect a disabled webhook.
            statement = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.organisation_id) == organisation_id,
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.integration_type).in_(
                    GOOGLE_WEBHOOK_INTEGRATION_TYPES + GOOGLE_WEBHOOK_PARENT_TYPES
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
                state.webhook_metadata = self._merge_subscription_metadata(
                    current_metadata=state.webhook_metadata,
                    provider_metadata=metadata,
                    kind=kind,
                )
                attributes.flag_modified(state, "webhook_metadata")
                session.add(state)
                await session.commit()
                return True

            merged_metadata = self._merge_subscription_metadata(
                current_metadata=integration.connection_metadata,
                provider_metadata=metadata,
                kind=kind,
            )
            persist_subscription_metadata(integration, merged_metadata)
            session.add(integration)
            await session.commit()
            return True
        return False

    def _merge_subscription_metadata(
        self,
        *,
        current_metadata: dict[str, Any] | None,
        provider_metadata: dict[str, Any],
        kind: GoogleSubscriptionKind,
    ) -> dict[str, Any]:
        """Merge only the refreshed Google subscription entry into the live row."""
        updated = dict(current_metadata or {})
        current_subscriptions = updated.get(GOOGLE_WEBHOOK_SUBSCRIPTIONS_KEY)
        subscriptions = (
            dict(current_subscriptions) if isinstance(current_subscriptions, dict) else {}
        )
        provider_subscriptions = provider_metadata.get(GOOGLE_WEBHOOK_SUBSCRIPTIONS_KEY)
        if isinstance(provider_subscriptions, dict):
            provider_entry = provider_subscriptions.get(kind.value)
            if isinstance(provider_entry, dict):
                subscriptions[kind.value] = dict(provider_entry)
                updated[GOOGLE_WEBHOOK_SUBSCRIPTIONS_KEY] = subscriptions
        return updated

    def _kind_for_integration(
        self, integration: KnowledgeIntegration
    ) -> GoogleSubscriptionKind | None:
        integration_type = (
            IntegrationType(integration.integration_type)
            if isinstance(integration.integration_type, str)
            else integration.integration_type
        )
        return kind_for_integration_type(integration_type)

    def _can_ensure_subscription(
        self,
        integration: KnowledgeIntegration,
        kind: GoogleSubscriptionKind,
    ) -> bool:
        if kind != GoogleSubscriptionKind.GMAIL:
            return True
        return is_required_scope_satisfied(
            GOOGLE_SCOPE_GMAIL_READONLY,
            list(integration.scopes or []),
        )

    def _snapshot_integration(
        self,
        integration: KnowledgeIntegration,
        *,
        integration_type: IntegrationType | None = None,
        connection_metadata: dict[str, Any] | None = None,
    ) -> KnowledgeIntegration:
        """Copy the integration row for provider calls outside the DB session.

        SQLModel instances are session-bound; this plain validated copy carries
        only the fields needed for token resolution and metadata decisions.
        """
        return KnowledgeIntegration.model_validate(
            {
                "integration_id": integration.integration_id,
                "organisation_id": integration.organisation_id,
                "created_by_user_id": integration.created_by_user_id,
                "created_by_admin": integration.created_by_admin,
                "integration_email": integration.integration_email,
                "integration_scope": integration.integration_scope,
                "integration_type": integration_type or integration.integration_type,
                "name": integration.name,
                "credentials": dict(integration.credentials or {}),
                "connection_id": integration.connection_id,
                "access_token_expires_at": integration.access_token_expires_at,
                "refresh_token_expires_at": integration.refresh_token_expires_at,
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
    "GOOGLE_WEBHOOK_INTEGRATION_TYPES",
    "GoogleSubscriptionTaskService",
    "kind_for_integration_type",
]
