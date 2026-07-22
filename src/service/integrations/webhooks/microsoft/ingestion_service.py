"""Microsoft Graph webhook ingestion service.

Called by the public webhook routes after request validation. This service
enforces tenant-scoped integration lookup and durably hands each accepted
callback to the integrations worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import (
    IntegrationSyncState,
    IntegrationType,
    KnowledgeIntegration,
    SyncStatus,
)
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.webhooks.microsoft.metadata import (
    MicrosoftSubscriptionKind,
    get_subscription_metadata,
)
from src.service.integrations.webhooks.microsoft.types import MicrosoftGraphWebhookNotification
from src.service.integrations.webhooks.sync_receipt import (
    create_webhook_sync_receipt,
    update_webhook_sync_receipt,
)
from src.workers.integrations_worker.client import send_integrations_task_async

logger = get_logger(__name__)

MICROSOFT_WEBHOOK_SYNC_COUNTDOWN_SECONDS = 15

_TASK_NAMES = {
    MicrosoftSubscriptionKind.CALENDAR: "integrations.sync_calendar_integration",
    MicrosoftSubscriptionKind.EMAIL: "integrations.sync_email_integration",
}

_INTEGRATION_TYPES = {
    MicrosoftSubscriptionKind.CALENDAR: IntegrationType.OUTLOOK_CALENDAR,
    MicrosoftSubscriptionKind.EMAIL: IntegrationType.OUTLOOK_EMAIL,
}


@dataclass(slots=True, frozen=True)
class MicrosoftWebhookIngestionResult:
    """Counters describing one webhook ingestion attempt."""

    parsed_count: int
    valid_count: int
    queued_count: int
    duplicate_count: int
    pending_count: int
    invalid_count: int


class MicrosoftWebhookIngestionService:
    """Validate webhook payloads and enqueue downstream integration sync work."""

    async def ingest(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        kind: MicrosoftSubscriptionKind,
        notification_payloads: list[dict[str, Any]],
    ) -> MicrosoftWebhookIngestionResult:
        """Process data-change notifications and schedule one sync per callback.

        Multiple notifications in a single callback can describe the same
        underlying change, so one durable cursor-based sync covers that callback.
        Separate deliveries are intentionally retained for recovery safety.
        """
        integration = await self._load_integration(
            organisation_id=organisation_id,
            integration_id=integration_id,
            kind=kind,
        )
        subscription_metadata = get_subscription_metadata(
            getattr(integration, "connection_metadata", None) if integration else None,
            kind,
        )
        if subscription_metadata is None:
            return MicrosoftWebhookIngestionResult(
                parsed_count=len(notification_payloads),
                valid_count=0,
                queued_count=0,
                duplicate_count=0,
                pending_count=0,
                invalid_count=len(notification_payloads),
            )

        valid_count = 0
        queued_count = 0
        duplicate_count = 0
        pending_count = 0
        invalid_count = 0
        sync_queued_in_call = False

        for payload in notification_payloads:
            notification = MicrosoftGraphWebhookNotification.from_payload(payload)
            if (
                notification is None
                or notification.subscription_id != subscription_metadata.subscription_id
                or notification.client_state != subscription_metadata.client_state
            ):
                # Subscription ID and client_state together bind notifications to
                # the subscription we created for this exact org integration.
                invalid_count += 1
                continue

            valid_count += 1
            if sync_queued_in_call:
                # One callback can contain several notifications; once this
                # request queued a sync, the rest can be counted as pending.
                pending_count += 1
                continue

            await self._enqueue_sync(
                organisation_id=organisation_id,
                integration_id=integration_id,
                kind=kind,
                countdown=MICROSOFT_WEBHOOK_SYNC_COUNTDOWN_SECONDS,
                metadata={
                    "provider": "microsoft",
                    "capability": kind.value,
                    "provider_notification_id": notification.dedupe_token(),
                    "provider_change_type": notification.change_type,
                    "provider_subscription_id": notification.subscription_id,
                },
            )
            queued_count += 1
            sync_queued_in_call = True

        return MicrosoftWebhookIngestionResult(
            parsed_count=len(notification_payloads),
            valid_count=valid_count,
            queued_count=queued_count,
            duplicate_count=duplicate_count,
            pending_count=pending_count,
            invalid_count=invalid_count,
        )

    async def ingest_lifecycle(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        kind: MicrosoftSubscriptionKind,
        notification_payloads: list[dict[str, Any]],
    ) -> MicrosoftWebhookIngestionResult:
        """Process lifecycle notifications and enqueue immediate subscription sync."""
        integration = await self._load_integration(
            organisation_id=organisation_id,
            integration_id=integration_id,
            kind=kind,
        )
        subscription_metadata = get_subscription_metadata(
            getattr(integration, "connection_metadata", None) if integration else None,
            kind,
        )
        if subscription_metadata is None:
            return MicrosoftWebhookIngestionResult(
                parsed_count=len(notification_payloads),
                valid_count=0,
                queued_count=0,
                duplicate_count=0,
                pending_count=0,
                invalid_count=len(notification_payloads),
            )

        valid_count = 0
        invalid_count = 0
        lifecycle_events: set[str] = set()

        for payload in notification_payloads:
            notification = MicrosoftGraphWebhookNotification.from_payload(payload)
            if (
                notification is None
                or notification.subscription_id != subscription_metadata.subscription_id
                or notification.client_state != subscription_metadata.client_state
            ):
                invalid_count += 1
                continue

            valid_count += 1
            lifecycle_events.add(notification.change_type)

        queued_count = 0
        if valid_count > 0:
            metadata = {
                "provider": "microsoft",
                "capability": kind.value,
                "provider_notification_id": (
                    f"{subscription_metadata.subscription_id}:"
                    f"{','.join(sorted(lifecycle_events))}"
                ),
                "provider_lifecycle_events": sorted(lifecycle_events),
            }
            recovery_event = next(
                (
                    event
                    for event in ("subscriptionRemoved", "reauthorizationRequired")
                    if event in lifecycle_events
                ),
                None,
            )
            if recovery_event:
                # Removed and reauthorization callbacks require subscription
                # repair before the delta sync that closes any delivery gap.
                await self._enqueue_subscription_recovery(
                    organisation_id=organisation_id,
                    integration_id=integration_id,
                    kind=kind,
                    lifecycle_event=recovery_event,
                    metadata=metadata,
                )
            else:
                # `missed` means Graph dropped notifications but the subscription
                # remains usable; an immediate delta sync is sufficient recovery.
                await self._enqueue_sync(
                    organisation_id=organisation_id,
                    integration_id=integration_id,
                    kind=kind,
                    countdown=None,
                    metadata=metadata,
                )
            queued_count = 1

        return MicrosoftWebhookIngestionResult(
            parsed_count=len(notification_payloads),
            valid_count=valid_count,
            queued_count=queued_count,
            duplicate_count=0,
            pending_count=0,
            invalid_count=invalid_count,
        )

    async def _load_integration(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        kind: MicrosoftSubscriptionKind,
    ) -> KnowledgeIntegration | None:
        """Load the active integration owned by this org and webhook kind."""
        expected_integration_type = _INTEGRATION_TYPES[kind]
        async for session in acquire_org_async_session(organisation_id):
            # Org filter is the immutable trust boundary; integration_id alone is
            # external routing input and is never trusted without org scoping.
            parent_stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.organisation_id) == organisation_id,
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.integration_type) == IntegrationType.MICROSOFT.value,
                col(KnowledgeIntegration.is_active).is_(True),
            )
            parent_result = await session.execute(parent_stmt)
            parent = parent_result.scalars().first()
            if parent is not None:
                state_stmt = select(IntegrationSyncState).where(
                    col(IntegrationSyncState.organisation_id) == organisation_id,
                    col(IntegrationSyncState.integration_id) == integration_id,
                    col(IntegrationSyncState.capability) == expected_integration_type,
                    col(IntegrationSyncState.enabled).is_(True),
                )
                state_result = await session.execute(state_stmt)
                state = state_result.scalar_one_or_none()
                if state is None:
                    return None
                return self._snapshot_integration(
                    parent,
                    connection_metadata=dict(state.webhook_metadata or {}),
                )

            legacy_stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.organisation_id) == organisation_id,
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.integration_type) == expected_integration_type.value,
                col(KnowledgeIntegration.is_active).is_(True),
            )
            legacy_result = await session.execute(legacy_stmt)
            return legacy_result.scalars().first()
        return None

    def _snapshot_integration(
        self,
        integration: KnowledgeIntegration,
        *,
        connection_metadata: dict[str, Any],
    ) -> KnowledgeIntegration:
        return KnowledgeIntegration.model_validate(
            {
                "integration_id": integration.integration_id,
                "organisation_id": integration.organisation_id,
                "created_by_user_id": integration.created_by_user_id,
                "created_by_admin": integration.created_by_admin,
                "integration_email": integration.integration_email,
                "integration_scope": integration.integration_scope,
                "integration_type": integration.integration_type,
                "name": integration.name,
                "credentials": dict(integration.credentials or {}),
                "connection_id": integration.connection_id,
                "access_token_expires_at": integration.access_token_expires_at,
                "refresh_token_expires_at": integration.refresh_token_expires_at,
                "scopes": list(integration.scopes or []),
                "is_active": integration.is_active,
                "connection_metadata": connection_metadata,
            }
        )

    async def _enqueue_sync(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        kind: MicrosoftSubscriptionKind,
        countdown: int | None,
        metadata: dict[str, Any],
    ) -> str:
        """Enqueue the org/integration sync task for the webhook subscription kind."""
        # Graph stops retrying after our 2xx response. Commit the receipt first
        # and let any broker error escape so the HTTP route returns non-2xx.
        sync_id = await create_webhook_sync_receipt(
            organisation_id=organisation_id,
            integration_id=integration_id,
            metadata=metadata,
        )
        try:
            task_id = await send_integrations_task_async(
                task_name=_TASK_NAMES[kind],
                kwargs={
                    "organisation_id": str(organisation_id),
                    "integration_id": str(integration_id),
                    "trigger_source": "webhook",
                    "webhook_sync_id": str(sync_id),
                },
                queue="integrations_priority",
                routing_key="integrations_priority",
                countdown=countdown,
            )
        except Exception as exc:
            await update_webhook_sync_receipt(
                organisation_id=organisation_id,
                sync_id=sync_id,
                status=SyncStatus.FAILED,
                metadata={"failure_stage": "queue_publish"},
                error=str(exc),
                completed=False,
            )
            raise
        await update_webhook_sync_receipt(
            organisation_id=organisation_id,
            sync_id=sync_id,
            metadata={"task_id": task_id},
        )
        return task_id

    async def _enqueue_subscription_recovery(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        kind: MicrosoftSubscriptionKind,
        lifecycle_event: str,
        metadata: dict[str, Any],
    ) -> str:
        """Durably queue subscription repair followed by an immediate delta sync."""
        sync_id = await create_webhook_sync_receipt(
            organisation_id=organisation_id,
            integration_id=integration_id,
            metadata={**metadata, "recovery_stage": "subscription"},
        )
        try:
            task_id = await send_integrations_task_async(
                task_name="integrations.ensure_microsoft_graph_subscription",
                kwargs={
                    "organisation_id": str(organisation_id),
                    "integration_id": str(integration_id),
                    "kind": kind.value,
                    "lifecycle_event": lifecycle_event,
                    "sync_after": True,
                    "webhook_sync_id": str(sync_id),
                },
                queue="integrations_priority",
                routing_key="integrations_priority",
            )
        except Exception as exc:
            await update_webhook_sync_receipt(
                organisation_id=organisation_id,
                sync_id=sync_id,
                status=SyncStatus.FAILED,
                metadata={"failure_stage": "queue_publish"},
                error=str(exc),
                completed=False,
            )
            raise
        await update_webhook_sync_receipt(
            organisation_id=organisation_id,
            sync_id=sync_id,
            metadata={"task_id": task_id},
        )
        return task_id


__all__ = [
    "MICROSOFT_WEBHOOK_SYNC_COUNTDOWN_SECONDS",
    "MicrosoftWebhookIngestionResult",
    "MicrosoftWebhookIngestionService",
]
