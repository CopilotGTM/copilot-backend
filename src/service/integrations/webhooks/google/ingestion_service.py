"""Google webhook business validation and sync fanout.

Called after the route/request layer has parsed a valid provider notification.
Webhook callbacks are triggers only: after tenant and subscription validation,
this service schedules the existing integration-level sync tasks that own
provider data writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlmodel import col
from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.domain.models.organisation_tables.sync import (
    IntegrationSyncState,
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.messaging.redis_client import get_redis_client
from src.infra.utils.logger import get_logger
from src.service.integrations.webhooks.google.metadata import (
    GoogleSubscriptionKind,
    get_subscription_metadata,
)
from src.service.integrations.webhooks.google.types import (
    GoogleCalendarChannelNotification,
    GoogleGmailPubSubNotification,
)
from src.workers.integrations_worker.client import send_integrations_task_async

logger = get_logger(__name__)

# Provider retries can replay the same notification hours later. Keep the
# dedupe token for a day so replayed Pub/Sub or Calendar deliveries do not fan
# out redundant sync work.
GOOGLE_WEBHOOK_DEDUPE_TTL_SECONDS = 24 * 60 * 60

# Coalesce bursts from one provider account into one near-term sync. The sync
# task still owns cursor correctness; this only reduces queue pressure.
GOOGLE_WEBHOOK_SYNC_DEBOUNCE_TTL_SECONDS = 45
GOOGLE_WEBHOOK_SYNC_COUNTDOWN_SECONDS = 15


@dataclass(slots=True, frozen=True)
class GoogleWebhookIngestionResult:
    """Counters returned to route tests and worker diagnostics."""

    valid_count: int
    queued_count: int
    duplicate_count: int
    pending_count: int
    invalid_count: int


@dataclass(slots=True, frozen=True)
class GoogleWebhookIntegrationTarget:
    """One active integration matched from provider-owned webhook routing data.

    Gmail Pub/Sub does not carry our org id, so ingestion first resolves a list
    of candidate org/integration pairs and then enqueues per target.
    """

    organisation_id: UUID
    integration_id: UUID
    integration: KnowledgeIntegration


class GoogleWebhookIngestionService:
    """Validate Google notifications and enqueue downstream sync work."""

    async def ingest_gmail(
        self,
        *,
        notification: GoogleGmailPubSubNotification,
    ) -> GoogleWebhookIngestionResult:
        """Process a decoded Gmail Pub/Sub notification.

        Called by the public Google route for `kind=gmail`. A shared Pub/Sub
        push subscription cannot include per-user query params, so the decoded
        Gmail `emailAddress` is the routing signal and every resulting sync is
        still scoped to a concrete org/integration pair.
        """
        # Resolve targets before touching Redis. Unknown Gmail accounts should
        # be cheap no-ops, and claiming dedupe first would hide future valid
        # retries if an integration is created immediately after the callback.
        targets = await self._load_gmail_targets_by_email(notification.email_address)
        if not targets:
            return GoogleWebhookIngestionResult(0, 0, 0, 0, 1)

        # Require stored watch metadata before scheduling sync. This prevents a
        # random Pub/Sub message with the same email from waking an integration
        # that never successfully created a Gmail watch.
        valid_targets: list[GoogleWebhookIntegrationTarget] = []
        invalid_count = 0
        for target in targets:
            metadata = get_subscription_metadata(
                getattr(target.integration, "connection_metadata", None),
                GoogleSubscriptionKind.GMAIL,
            )
            if metadata is None or not self._matches_gmail_email(
                target.integration,
                notification.email_address,
            ):
                invalid_count += 1
                continue
            valid_targets.append(target)

        if not valid_targets:
            return GoogleWebhookIngestionResult(0, 0, 0, 0, invalid_count)

        # Deduping is global by Pub/Sub message id because Pub/Sub retry
        # semantics replay the same envelope, not one envelope per org.
        if not self._claim_dedupe(f"gmail:{notification.message_id}"):
            return GoogleWebhookIngestionResult(1, 0, 1, 0, 0)

        queued_count = 0
        pending_count = 0
        for target in valid_targets:
            if not self._claim_sync_pending(
                target.organisation_id,
                target.integration_id,
                GoogleSubscriptionKind.GMAIL,
            ):
                pending_count += 1
                continue

            await self._enqueue_sync(
                task_name="integrations.sync_email_integration",
                organisation_id=target.organisation_id,
                integration_id=target.integration_id,
            )
            queued_count += 1

        return GoogleWebhookIngestionResult(
            len(valid_targets),
            queued_count,
            0,
            pending_count,
            invalid_count,
        )

    async def ingest_calendar(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        notification: GoogleCalendarChannelNotification,
    ) -> GoogleWebhookIngestionResult:
        """Process a Google Calendar channel notification.

        Calendar watch callbacks include our org and integration ids in the
        generated callback URL, but those ids are not trusted until the org
        query below confirms an active matching integration.
        """
        integration = await self._load_integration(
            organisation_id=organisation_id,
            integration_id=integration_id,
            integration_type=IntegrationType.GOOGLE_CALENDAR,
        )
        metadata = get_subscription_metadata(
            getattr(integration, "connection_metadata", None) if integration else None,
            GoogleSubscriptionKind.CALENDAR,
        )
        if integration is None or metadata is None:
            return GoogleWebhookIngestionResult(0, 0, 0, 0, 1)
        if (
            notification.channel_id != metadata.channel_id
            or notification.resource_id != metadata.resource_id
            or notification.channel_token != metadata.channel_token
        ):
            return GoogleWebhookIngestionResult(0, 0, 0, 0, 1)
        if notification.resource_state == "sync":
            # Calendar sends an initial "sync" message when a channel starts.
            # It only confirms delivery is active; syncing here would duplicate
            # the post-connect sync that establishes the real cursor.
            return GoogleWebhookIngestionResult(1, 0, 0, 0, 0)

        if not self._claim_dedupe(
            f"calendar:{notification.channel_id}:{notification.message_number}"
        ):
            return GoogleWebhookIngestionResult(1, 0, 1, 0, 0)
        if not self._claim_sync_pending(
            organisation_id,
            integration_id,
            GoogleSubscriptionKind.CALENDAR,
        ):
            return GoogleWebhookIngestionResult(1, 0, 0, 1, 0)

        await self._enqueue_sync(
            task_name="integrations.sync_calendar_integration",
            organisation_id=organisation_id,
            integration_id=integration_id,
        )
        return GoogleWebhookIngestionResult(1, 1, 0, 0, 0)

    async def _load_integration(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        integration_type: IntegrationType,
    ) -> KnowledgeIntegration | None:
        """Load the active org-owned integration for this callback."""
        async for session in acquire_org_async_session(organisation_id):
            # The org filter is the trust boundary. Provider callback ids are
            # externally supplied and cannot be used without tenant scoping.
            provider = (
                IntegrationType.GOOGLE
                if integration_type == IntegrationType.GOOGLE_CALENDAR
                else integration_type
            )
            parent_stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.organisation_id) == organisation_id,
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.integration_type) == provider.value,
                col(KnowledgeIntegration.is_active).is_(True),
            )
            parent_result = await session.execute(parent_stmt)
            parent = parent_result.scalars().first()
            if parent is not None and provider == IntegrationType.GOOGLE:
                state_stmt = select(IntegrationSyncState).where(
                    col(IntegrationSyncState.organisation_id) == organisation_id,
                    col(IntegrationSyncState.integration_id) == integration_id,
                    col(IntegrationSyncState.capability) == integration_type,
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
            if parent is not None:
                return parent

            legacy_stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.organisation_id) == organisation_id,
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.integration_type) == integration_type.value,
                col(KnowledgeIntegration.is_active).is_(True),
            )
            legacy_result = await session.execute(legacy_stmt)
            return legacy_result.scalars().first()
        return None

    async def _load_gmail_targets_by_email(
        self,
        email_address: str,
    ) -> list[GoogleWebhookIntegrationTarget]:
        """Find active Gmail integrations for Pub/Sub notifications by account email."""
        normalized_email = email_address.strip().lower()
        if not normalized_email:
            return []

        # Gmail Pub/Sub does not carry our tenant id. Scan approved orgs first,
        # then query each tenant schema so routing still respects isolation.
        organisation_ids: list[UUID] = []
        async for session in acquire_master_async_session():
            org_stmt = select(col(Organisation.organisation_id)).where(
                col(Organisation.is_active).is_(True),
                col(Organisation.status) == OrganisationStatus.APPROVED,
            )
            org_result = await session.execute(org_stmt)
            organisation_ids = list(org_result.scalars().all())

        targets: list[GoogleWebhookIntegrationTarget] = []
        for organisation_id in organisation_ids:
            async for session in acquire_org_async_session(organisation_id):
                # Match on integration_email only. Connection metadata can
                # contain provider profile fields, but the persisted integration
                # owner email is the stable routing key from OAuth connect.
                integration_stmt = select(KnowledgeIntegration).where(
                    col(KnowledgeIntegration.organisation_id) == organisation_id,
                    col(KnowledgeIntegration.integration_type) == IntegrationType.GOOGLE.value,
                    col(KnowledgeIntegration.is_active).is_(True),
                    col(KnowledgeIntegration.connection_id).isnot(None),
                    func.lower(col(KnowledgeIntegration.integration_email)) == normalized_email,
                )
                integration_result = await session.execute(integration_stmt)
                for integration in integration_result.scalars().all():
                    state_stmt = select(IntegrationSyncState).where(
                        col(IntegrationSyncState.organisation_id) == organisation_id,
                        col(IntegrationSyncState.integration_id) == integration.integration_id,
                        col(IntegrationSyncState.capability) == IntegrationType.GMAIL,
                        col(IntegrationSyncState.enabled).is_(True),
                    )
                    state_result = await session.execute(state_stmt)
                    state = state_result.scalar_one_or_none()
                    if state is None:
                        continue
                    targets.append(
                        GoogleWebhookIntegrationTarget(
                            organisation_id=organisation_id,
                            integration_id=integration.integration_id,
                            integration=self._snapshot_integration(
                                integration,
                                connection_metadata=dict(state.webhook_metadata or {}),
                            ),
                        )
                    )
        return targets

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

    def _matches_gmail_email(
        self,
        integration: KnowledgeIntegration,
        email_address: str,
    ) -> bool:
        """Validate Gmail's decoded account email against stored owner metadata."""
        metadata = getattr(integration, "connection_metadata", None) or {}
        candidates = (
            getattr(integration, "integration_email", None),
            metadata.get("email") if isinstance(metadata, dict) else None,
            metadata.get("emailAddress") if isinstance(metadata, dict) else None,
            metadata.get("google_email") if isinstance(metadata, dict) else None,
        )
        known_emails = {
            value.lower() for value in candidates if isinstance(value, str) and value.strip()
        }
        return not known_emails or email_address.lower() in known_emails

    def _claim_dedupe(self, token: str) -> bool:
        """Claim a notification token; true means this callback is first-seen."""
        try:
            was_set = get_redis_client().set(
                f"webhook:google:dedupe:{token}",
                "1",
                nx=True,
                ex=GOOGLE_WEBHOOK_DEDUPE_TTL_SECONDS,
            )
            return bool(was_set)
        except Exception:
            # Redis is an optimization, not the correctness layer. Fail open so
            # provider notifications still schedule sync; the sync task remains
            # idempotent through provider cursors and local upserts.
            logger.debug("Google webhook dedupe claim failed", exc_info=True)
            return True

    def _claim_sync_pending(
        self,
        organisation_id: UUID,
        integration_id: UUID,
        kind: GoogleSubscriptionKind,
    ) -> bool:
        """Claim a short-lived marker to coalesce bursts into one sync."""
        try:
            was_set = get_redis_client().set(
                f"webhook:google:sync:{organisation_id}:{integration_id}:{kind.value}",
                "1",
                nx=True,
                ex=GOOGLE_WEBHOOK_SYNC_DEBOUNCE_TTL_SECONDS,
            )
            return bool(was_set)
        except Exception:
            # A Redis outage should increase queue volume, not drop provider
            # notifications. The downstream sync task is safe to run repeatedly.
            logger.debug("Google webhook sync pending claim failed", exc_info=True)
            return True

    async def _enqueue_sync(
        self,
        *,
        task_name: str,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> str:
        """Enqueue the existing org/integration sync task.

        Webhooks never write emails or events directly; provider cursors and
        upsert idempotency live in the existing sync implementations.
        """
        return await send_integrations_task_async(
            task_name=task_name,
            kwargs={
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
                "trigger_source": "webhook",
            },
            queue="integrations_priority",
            routing_key="integrations_priority",
            countdown=GOOGLE_WEBHOOK_SYNC_COUNTDOWN_SECONDS,
        )


__all__ = [
    "GOOGLE_WEBHOOK_DEDUPE_TTL_SECONDS",
    "GOOGLE_WEBHOOK_SYNC_COUNTDOWN_SECONDS",
    "GOOGLE_WEBHOOK_SYNC_DEBOUNCE_TTL_SECONDS",
    "GoogleWebhookIntegrationTarget",
    "GoogleWebhookIngestionResult",
    "GoogleWebhookIngestionService",
]
