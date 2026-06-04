"""Microsoft Graph webhook ingestion service.

Called by the public webhook routes after request validation. This service
enforces tenant-scoped integration lookup, deduplicates provider retries, and
debounces bursty notifications into a single sync task.
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
)
from src.infra.database.sessions import acquire_org_async_session
from src.infra.messaging.redis_client import get_redis_client
from src.infra.utils.logger import get_logger
from src.service.integrations.webhooks.microsoft.metadata import (
    MicrosoftSubscriptionKind,
    get_subscription_metadata,
)
from src.service.integrations.webhooks.microsoft.types import MicrosoftGraphWebhookNotification
from src.workers.integrations_worker.client import send_integrations_task_async

logger = get_logger(__name__)

# Graph retries can arrive long after the first callback; keep dedupe tokens for
# a day so replayed notifications do not create repeated sync fanout.
MICROSOFT_WEBHOOK_DEDUPE_TTL_SECONDS = 24 * 60 * 60

# A short pending marker turns webhook bursts into one integration sync. The
# sync itself reads Graph deltas, so coalescing notifications does not lose data.
MICROSOFT_WEBHOOK_SYNC_DEBOUNCE_TTL_SECONDS = 45
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
        """Process data-change notifications and schedule one debounced sync.

        Multiple notifications in a single callback can describe the same
        underlying change; we dedupe and coalesce them to avoid redundant sync
        fanout while still acknowledging all valid notifications.
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
            if not self._claim_dedupe(subscription_metadata.subscription_id, notification):
                duplicate_count += 1
                continue

            if sync_queued_in_call:
                # One callback can contain several notifications; once this
                # request queued a sync, the rest can be counted as pending.
                pending_count += 1
                continue

            if not self._claim_sync_pending(organisation_id, integration_id, kind):
                pending_count += 1
                continue

            await self._enqueue_sync(
                organisation_id=organisation_id,
                integration_id=integration_id,
                kind=kind,
                countdown=MICROSOFT_WEBHOOK_SYNC_COUNTDOWN_SECONDS,
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

        queued_count = 0
        if valid_count > 0:
            # Lifecycle notifications mean the subscription needs attention, so
            # bypass the debounce delay and let the existing sync path refresh.
            await self._enqueue_sync(
                organisation_id=organisation_id,
                integration_id=integration_id,
                kind=kind,
                countdown=None,
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

    def _dedupe_key(
        self,
        subscription_id: str,
        notification: MicrosoftGraphWebhookNotification,
    ) -> str:
        """Build Redis key used to dedupe replayed webhook notifications."""
        return f"webhook:microsoft:dedupe:{subscription_id}:{notification.dedupe_token()}"

    def _sync_pending_key(
        self,
        organisation_id: UUID,
        integration_id: UUID,
        kind: MicrosoftSubscriptionKind,
    ) -> str:
        """Build Redis key that coalesces burst notifications into one sync."""
        return f"webhook:microsoft:sync:{organisation_id}:{integration_id}:{kind.value}"

    def _claim_dedupe(
        self,
        subscription_id: str,
        notification: MicrosoftGraphWebhookNotification,
    ) -> bool:
        """Claim notification dedupe token; true means this is first-seen."""
        try:
            redis = get_redis_client()
            was_set = redis.set(
                self._dedupe_key(subscription_id, notification),
                "1",
                nx=True,
                ex=MICROSOFT_WEBHOOK_DEDUPE_TTL_SECONDS,
            )
            return bool(was_set)
        except Exception:
            # Redis outages must not block webhook processing; accepting a small
            # amount of duplicate fanout is safer than dropping provider changes.
            logger.debug("Microsoft webhook dedupe claim failed", exc_info=True)
            return True

    def _claim_sync_pending(
        self,
        organisation_id: UUID,
        integration_id: UUID,
        kind: MicrosoftSubscriptionKind,
    ) -> bool:
        """Claim short-lived pending marker to debounce repeated sync requests."""
        try:
            redis = get_redis_client()
            was_set = redis.set(
                self._sync_pending_key(organisation_id, integration_id, kind),
                "1",
                nx=True,
                ex=MICROSOFT_WEBHOOK_SYNC_DEBOUNCE_TTL_SECONDS,
            )
            return bool(was_set)
        except Exception:
            # Debounce is an optimization, not correctness. If Redis is down we
            # still enqueue so Graph deltas remain the source of truth.
            logger.debug("Microsoft webhook sync pending claim failed", exc_info=True)
            return True

    async def _enqueue_sync(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        kind: MicrosoftSubscriptionKind,
        countdown: int | None,
    ) -> str:
        """Enqueue the org/integration sync task for the webhook subscription kind."""
        return await send_integrations_task_async(
            task_name=_TASK_NAMES[kind],
            kwargs={
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
            },
            queue="integrations_priority",
            routing_key="integrations_priority",
            countdown=countdown,
        )


__all__ = [
    "MICROSOFT_WEBHOOK_DEDUPE_TTL_SECONDS",
    "MICROSOFT_WEBHOOK_SYNC_COUNTDOWN_SECONDS",
    "MICROSOFT_WEBHOOK_SYNC_DEBOUNCE_TTL_SECONDS",
    "MicrosoftWebhookIngestionResult",
    "MicrosoftWebhookIngestionService",
]
