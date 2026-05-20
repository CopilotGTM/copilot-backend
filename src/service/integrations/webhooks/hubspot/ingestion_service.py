"""HubSpot webhook ingestion service.

The public FastAPI route owns HTTP concerns: request size, signature
verification, and JSON parsing. This service owns webhook semantics: supported
subscription filtering, property allow-lists, per-request coalescing,
idempotency, debounce, and Celery fanout.

The service does not call HubSpot or write tenant data directly. It only turns a
verified provider delivery into integrations-worker tasks, where token lookup,
provider reads, and org-scoped persistence happen.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from src.infra.messaging.redis_client import get_redis_client
from src.infra.utils.logger import get_logger
from src.service.integrations.webhooks.hubspot.companies.subscriptions import (
    COMPANY_CREATION,
    COMPANY_DELETION,
    COMPANY_PROPERTY_CHANGE,
    COMPANY_PROPERTY_CHANGE_PROPERTY_SET,
)
from src.service.integrations.webhooks.hubspot.constants import HUBSPOT_WEBHOOK_EVENT_TASK
from src.service.integrations.webhooks.hubspot.contacts.subscriptions import (
    CONTACT_CREATION,
    CONTACT_DELETION,
    CONTACT_PROPERTY_CHANGE,
    CONTACT_PROPERTY_CHANGE_PROPERTY_SET,
)
from src.service.integrations.webhooks.hubspot.deals.subscriptions import (
    DEAL_CREATION,
    DEAL_DELETION,
    DEAL_PROPERTY_CHANGE,
    DEAL_PROPERTY_CHANGE_PROPERTY_SET,
)
from src.service.integrations.webhooks.hubspot.dispatcher import HubSpotWebhookDispatcher
from src.service.integrations.webhooks.hubspot.types import HubSpotWebhookEvent
from src.workers.integrations_worker.client import send_integrations_task_async

logger = get_logger(__name__)

# HubSpot retries failed webhook deliveries for a limited period. Keep the
# idempotency claim long enough to absorb retries without permanently hiding a
# future legitimate event with the same object id.
HUBSPOT_WEBHOOK_DEDUPE_TTL_SECONDS = 24 * 60 * 60

# Property-change bursts often arrive as several provider events for the same
# object. A short pending window collapses those bursts into one full-object
# refresh while still letting follow-up changes sync quickly.
HUBSPOT_WEBHOOK_REFRESH_DEBOUNCE_TTL_SECONDS = 45
HUBSPOT_WEBHOOK_REFRESH_COUNTDOWN_SECONDS = 30

_CREATION_SUBSCRIPTION_TYPES = frozenset({COMPANY_CREATION, CONTACT_CREATION, DEAL_CREATION})
_DELETION_SUBSCRIPTION_TYPES = frozenset({COMPANY_DELETION, CONTACT_DELETION, DEAL_DELETION})
_PROPERTY_CHANGE_SUBSCRIPTION_TYPES = frozenset(
    {COMPANY_PROPERTY_CHANGE, CONTACT_PROPERTY_CHANGE, DEAL_PROPERTY_CHANGE}
)
_COALESCED_SUBSCRIPTION_TYPES = (
    _CREATION_SUBSCRIPTION_TYPES
    | _DELETION_SUBSCRIPTION_TYPES
    | _PROPERTY_CHANGE_SUBSCRIPTION_TYPES
)
_DELAYED_REFRESH_SUBSCRIPTION_TYPES = frozenset(
    {
        COMPANY_CREATION,
        COMPANY_PROPERTY_CHANGE,
        CONTACT_CREATION,
        CONTACT_PROPERTY_CHANGE,
        DEAL_CREATION,
        DEAL_PROPERTY_CHANGE,
    }
)


@dataclass(slots=True, frozen=True)
class HubSpotWebhookIngestionResult:
    """Summary of one webhook HTTP payload ingestion pass.

    The public route does not expose this to HubSpot. It exists so tests and
    future internal diagnostics can inspect ingestion behavior without parsing
    log lines.
    """

    parsed_count: int
    supported_count: int
    queued_count: int
    duplicate_count: int
    refresh_pending_count: int


class HubSpotWebhookIngestionService:
    """Filter, coalesce, dedupe, debounce, and enqueue HubSpot webhook events.

    Called by the public HubSpot webhook route after signature verification.
    It is intentionally stateless; Redis holds short-lived idempotency and
    refresh-pending claims, and Celery owns durable worker execution.
    """

    async def ingest(
        self,
        *,
        raw_body: bytes,
        event_payloads: list[dict],
    ) -> HubSpotWebhookIngestionResult:
        """Process one verified HubSpot HTTP payload into worker tasks.

        Malformed or unsupported events are acknowledged by omission rather
        than raising, because HubSpot retries the whole delivery on non-2xx
        responses. Broker failures still bubble up so HubSpot can retry.
        """
        events: list[HubSpotWebhookEvent] = []

        # --- Step 1: validate shape and subscription scope ---
        # Unsupported provider subscriptions are acknowledged but not queued.
        # This lets the HubSpot app contain future subscriptions without making
        # older deployed workers process event types they do not understand.
        for event_payload in event_payloads:
            event = HubSpotWebhookEvent.from_payload(event_payload)
            if event is None:
                logger.warning(
                    "Ignoring malformed HubSpot webhook event",
                    extra={"event": "hubspot_webhook_event_malformed"},
                )
                continue

            if not _is_supported_event(event):
                continue

            events.append(event)

        queued_count = 0
        duplicate_count = 0
        refresh_pending_count = 0

        # --- Step 2: collapse noisy same-object events in this HTTP delivery ---
        # HubSpot can send create plus several propertyChange events together.
        # Coalescing before Redis dedupe means one delivery schedules at most one
        # refresh per object family and object id.
        for event in _coalesce_batch_events(events):
            if _is_duplicate_event(event, raw_body):
                duplicate_count += 1
                logger.info(
                    "Duplicate HubSpot webhook event ignored",
                    extra={
                        "event": "hubspot_webhook_event_duplicate",
                        "subscription_type": event.subscription_type,
                        "portal_id": event.portal_id,
                        "object_id": event.object_id,
                        "event_id": event.event_id,
                        "property_name": _event_property_name(event),
                    },
                )
                continue

            enqueue_countdown: int | None = None
            refresh_claimed = False
            if _is_delayed_refresh_event(event):
                # Delay create/update refreshes briefly so HubSpot's object API
                # has the final object snapshot and sibling property-change
                # events can collapse into the same pending refresh.
                refresh_claimed = _claim_refresh_pending(event)
                if not refresh_claimed:
                    refresh_pending_count += 1
                    logger.info(
                        "HubSpot webhook refresh already pending",
                        extra={
                            "event": "hubspot_webhook_refresh_pending",
                            "subscription_type": event.subscription_type,
                            "portal_id": event.portal_id,
                            "object_id": event.object_id,
                            "event_id": event.event_id,
                            "property_name": _event_property_name(event),
                        },
                    )
                    continue
                enqueue_countdown = HUBSPOT_WEBHOOK_REFRESH_COUNTDOWN_SECONDS

            if event.subscription_type in {COMPANY_DELETION, CONTACT_DELETION, DEAL_DELETION}:
                # Deletions should not wait behind an earlier create/update
                # debounce window. Releasing the pending key lets the deletion
                # task queue immediately and soft-delete the local row.
                _release_refresh_pending(event)

            try:
                task_id = await _enqueue_event(event, enqueue_countdown=enqueue_countdown)
            except Exception:
                # The dedupe claim is only valid after the worker task is safely
                # queued. If broker enqueue fails, release claims so HubSpot's
                # retry can schedule the event again.
                if refresh_claimed:
                    _release_refresh_pending(event)
                _release_dedupe_claim(event, raw_body)
                raise

            queued_count += 1
            logger.info(
                "Queued HubSpot webhook event",
                extra={
                    "event": "hubspot_webhook_event_queued",
                    "subscription_type": event.subscription_type,
                    "portal_id": event.portal_id,
                    "object_id": event.object_id,
                    "event_id": event.event_id,
                    "property_name": _event_property_name(event),
                    "task_id": task_id,
                },
            )

        return HubSpotWebhookIngestionResult(
            parsed_count=len(event_payloads),
            supported_count=len(events),
            queued_count=queued_count,
            duplicate_count=duplicate_count,
            refresh_pending_count=refresh_pending_count,
        )


async def _enqueue_event(event: HubSpotWebhookEvent, *, enqueue_countdown: int | None) -> str:
    """Send one HubSpot event to the integrations priority queue.

    The worker performs all provider reads and database writes. This API process
    only dispatches the event so the public webhook request stays fast and has a
    small failure surface.
    """
    kwargs: dict[str, Any] = {
        "task_name": HUBSPOT_WEBHOOK_EVENT_TASK,
        "kwargs": event.to_task_kwargs(),
        "queue": "integrations_priority",
        "routing_key": "integrations_priority",
    }
    if enqueue_countdown is not None:
        kwargs["countdown"] = enqueue_countdown
    return await send_integrations_task_async(**kwargs)


def _dedupe_key(event: HubSpotWebhookEvent, raw_body: bytes) -> str:
    """Build the idempotency key for a webhook event.

    HubSpot normally sends `eventId`, which is the safest retry identifier. The
    raw-body hash fallback keeps older or incomplete payloads idempotent without
    collapsing different events for the same object.
    """
    if event.event_id:
        return f"webhook:hubspot:{event.portal_id}:{event.event_id}"

    body_hash = hashlib.sha256(raw_body).hexdigest()
    return (
        f"webhook:hubspot:{event.portal_id}:{event.subscription_type}:{event.object_id}:{body_hash}"
    )


def _refresh_pending_key(event: HubSpotWebhookEvent) -> str:
    """Build the short-lived same-object refresh key.

    The object family is part of the key so `deal:123` and `company:123` never
    suppress each other even if HubSpot ids happen to match.
    """
    object_family = event.subscription_type.split(".", 1)[0]
    return f"webhook:hubspot:refresh:{object_family}:{event.portal_id}:{event.object_id}"


def _event_property_name(event: HubSpotWebhookEvent) -> str | None:
    """Return the property name from a HubSpot propertyChange event, if present."""
    property_name = event.raw.get("propertyName")
    return str(property_name) if property_name is not None else None


def _is_supported_event(event: HubSpotWebhookEvent) -> bool:
    """Return whether this event is supported at both type and property level.

    The dispatcher registry answers "do we have a handler for this subscription
    type?" Property-change events then get a narrower allow-list so unrelated
    HubSpot custom fields do not trigger full-object refreshes.
    """
    if not HubSpotWebhookDispatcher.is_supported(event.subscription_type):
        return False

    if event.subscription_type == DEAL_PROPERTY_CHANGE:
        return _is_supported_deal_property_change(event)

    if event.subscription_type == CONTACT_PROPERTY_CHANGE:
        return _is_supported_contact_property_change(event)

    if event.subscription_type == COMPANY_PROPERTY_CHANGE:
        return _is_supported_company_property_change(event)

    return True


def _is_supported_deal_property_change(event: HubSpotWebhookEvent) -> bool:
    """Allow deal property changes only for fields our sync pipeline consumes."""
    property_name = _event_property_name(event)
    if property_name in DEAL_PROPERTY_CHANGE_PROPERTY_SET:
        return True

    logger.info(
        "Ignoring unsupported HubSpot deal propertyChange event",
        extra={
            "event": "hubspot_deal_property_change_unsupported",
            "subscription_type": event.subscription_type,
            "portal_id": event.portal_id,
            "object_id": event.object_id,
            "event_id": event.event_id,
            "property_name": property_name,
        },
    )
    return False


def _is_supported_contact_property_change(event: HubSpotWebhookEvent) -> bool:
    """Allow contact property changes only for fields our contact writer maps."""
    property_name = _event_property_name(event)
    if property_name in CONTACT_PROPERTY_CHANGE_PROPERTY_SET:
        return True

    logger.info(
        "Ignoring unsupported HubSpot contact propertyChange event",
        extra={
            "event": "hubspot_contact_property_change_unsupported",
            "subscription_type": event.subscription_type,
            "portal_id": event.portal_id,
            "object_id": event.object_id,
            "event_id": event.event_id,
            "property_name": property_name,
        },
    )
    return False


def _is_supported_company_property_change(event: HubSpotWebhookEvent) -> bool:
    """Allow company property changes only for fields our account table stores."""
    property_name = _event_property_name(event)
    if property_name in COMPANY_PROPERTY_CHANGE_PROPERTY_SET:
        return True

    logger.info(
        "Ignoring unsupported HubSpot company propertyChange event",
        extra={
            "event": "hubspot_company_property_change_unsupported",
            "subscription_type": event.subscription_type,
            "portal_id": event.portal_id,
            "object_id": event.object_id,
            "event_id": event.event_id,
            "property_name": property_name,
        },
    )
    return False


def _claim_refresh_pending(event: HubSpotWebhookEvent) -> bool:
    """Claim a short object-level refresh window for noisy webhook bursts.

    Redis is an optimization, not the source of truth. If Redis is unavailable,
    return True so webhooks still sync correctly, accepting extra duplicate
    refreshes until the cache recovers.
    """
    try:
        redis = get_redis_client()
        was_set = redis.set(
            _refresh_pending_key(event),
            "1",
            nx=True,
            ex=HUBSPOT_WEBHOOK_REFRESH_DEBOUNCE_TTL_SECONDS,
        )
        return bool(was_set)
    except Exception:
        logger.debug("HubSpot webhook refresh debounce claim failed", exc_info=True)
        return True


def _release_refresh_pending(event: HubSpotWebhookEvent) -> None:
    """Release a refresh claim after enqueue failure or immediate deletion.

    Best-effort release is enough because the key has a short TTL. A failed
    delete may cause one extra debounce skip, not permanent data loss.
    """
    try:
        redis = get_redis_client()
        redis.delete(_refresh_pending_key(event))
    except Exception:
        logger.debug("HubSpot webhook refresh debounce release failed", exc_info=True)


def _coalesce_batch_events(events: list[HubSpotWebhookEvent]) -> list[HubSpotWebhookEvent]:
    """Coalesce same-object events within one HubSpot HTTP payload.

    This does not dedupe across separate requests; Redis handles that later.
    The narrower scope keeps deal and company events independent even when they
    share a portal id and object id.
    """
    coalesced: list[HubSpotWebhookEvent] = []
    coalescing_indexes: dict[tuple[str, str, str], int] = {}

    for event in events:
        if event.subscription_type not in _COALESCED_SUBSCRIPTION_TYPES:
            coalesced.append(event)
            continue

        object_family = event.subscription_type.split(".", 1)[0]
        coalescing_key = (object_family, event.portal_id, event.object_id)
        existing_index = coalescing_indexes.get(coalescing_key)
        if existing_index is None:
            coalescing_indexes[coalescing_key] = len(coalesced)
            coalesced.append(event)
            continue

        existing_event = coalesced[existing_index]
        selected_event, ignored_event = _select_same_object_batch_events(
            existing_event,
            event,
        )
        coalesced[existing_index] = selected_event

        logger.info(
            "Coalesced HubSpot webhook event",
            extra={
                "event": "hubspot_webhook_event_coalesced",
                "object_family": object_family,
                "subscription_type": ignored_event.subscription_type,
                "selected_subscription_type": selected_event.subscription_type,
                "portal_id": ignored_event.portal_id,
                "object_id": ignored_event.object_id,
                "event_id": ignored_event.event_id,
                "selected_event_id": selected_event.event_id,
                "property_name": _event_property_name(ignored_event),
            },
        )

    return coalesced


def _select_same_object_batch_events(
    existing_event: HubSpotWebhookEvent,
    incoming_event: HubSpotWebhookEvent,
) -> tuple[HubSpotWebhookEvent, HubSpotWebhookEvent]:
    """Return the selected and ignored events for one same-object batch pair.

    The rule is intentionally direct: deletion wins, then creation wins, then
    one propertyChange is enough. The worker fetches the full latest object for
    create/update events, so keeping multiple property changes adds work but no
    additional state.
    """
    if incoming_event.subscription_type in _DELETION_SUBSCRIPTION_TYPES:
        return incoming_event, existing_event
    if existing_event.subscription_type in _DELETION_SUBSCRIPTION_TYPES:
        return existing_event, incoming_event

    if incoming_event.subscription_type in _CREATION_SUBSCRIPTION_TYPES:
        return incoming_event, existing_event
    if existing_event.subscription_type in _CREATION_SUBSCRIPTION_TYPES:
        return existing_event, incoming_event

    return existing_event, incoming_event


def _is_delayed_refresh_event(event: HubSpotWebhookEvent) -> bool:
    """Return whether the event should wait for the object snapshot to settle."""
    return event.subscription_type in _DELAYED_REFRESH_SUBSCRIPTION_TYPES


def _is_duplicate_event(event: HubSpotWebhookEvent, raw_body: bytes) -> bool:
    """Claim the webhook event in Redis and report whether it was already seen.

    Cache failure favors correctness over efficiency: if Redis cannot answer,
    enqueue the event and let the worker write the same final provider snapshot
    again rather than risk dropping a real change.
    """
    try:
        redis = get_redis_client()
        was_set = redis.set(
            _dedupe_key(event, raw_body),
            "1",
            nx=True,
            ex=HUBSPOT_WEBHOOK_DEDUPE_TTL_SECONDS,
        )
        return not bool(was_set)
    except Exception:
        logger.debug("HubSpot webhook dedupe check failed", exc_info=True)
        return False


def _release_dedupe_claim(event: HubSpotWebhookEvent, raw_body: bytes) -> None:
    """Release a dedupe claim when enqueueing fails so HubSpot can retry.

    The claim protects against provider retries only after queueing succeeds.
    Without this release, a transient broker failure could make the retry look
    like a duplicate for the full dedupe TTL.
    """
    try:
        redis = get_redis_client()
        redis.delete(_dedupe_key(event, raw_body))
    except Exception:
        logger.debug("HubSpot webhook dedupe release failed", exc_info=True)


__all__ = [
    "HUBSPOT_WEBHOOK_DEDUPE_TTL_SECONDS",
    "HUBSPOT_WEBHOOK_REFRESH_COUNTDOWN_SECONDS",
    "HUBSPOT_WEBHOOK_REFRESH_DEBOUNCE_TTL_SECONDS",
    "HubSpotWebhookIngestionResult",
    "HubSpotWebhookIngestionService",
]
