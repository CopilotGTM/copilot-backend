"""Shared CRM mutation-event recorder.

Called by CRM write services after they mutate the entity row and compute the
field-level diff. This module owns the append-only audit insert, idempotent retry
behavior, tenant-scoped lookup, and durable dispatch of supported agent events.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealMutationEvent
from src.service.automation.errors import AutomationServiceError
from src.service.automation.events.service import AutomationEventDispatchService
from src.service.automation.triggers.crm_events import CRM_AGENT_EVENT_SCHEMAS
from src.service.crm.mutations.contracts import (
    CRMMutationEventServiceError,
    CRMMutationRecordCommand,
    CRMMutationRecordResult,
)
from src.service.crm.mutations.json_safe import normalize_changed_fields


class AutomationEventEnqueuer(Protocol):
    """Transaction-local outbox registration for supported source events."""

    async def enqueue(self, *, source_domain: str, source_event_id: str, event_type: str) -> object:
        """Append one source occurrence without committing or publishing."""

        ...


class CRMMutationEventService:
    """Writes immutable CRM mutation events for contact, account, and deal writes.

    The service is constructed with the caller's org session, so it participates
    in the same transaction as the entity write. A rollback removes both the CRM
    row change and its audit event, keeping the ledger consistent.
    """

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        *,
        event_enqueuer: AutomationEventEnqueuer | None = None,
    ):
        self.session = session
        self.organisation_id = organisation_id
        self._automation_events = event_enqueuer or AutomationEventDispatchService(
            session, organisation_id
        )

    async def record_event(self, command: CRMMutationRecordCommand) -> CRMMutationRecordResult:
        """Persist one mutation event when the command contains real field changes.

        Callers pass all changed fields for one logical write in a single
        command. That keeps multi-field updates as one audit row and gives future
        workflow routing one coherent mutation envelope.
        """
        if not command.changed_fields:
            return CRMMutationRecordResult(event_id=None, changed=False)

        # Normalize before hashing so semantically identical retries compare on
        # the exact JSON-safe payload that will be stored in the audit table.
        changed_fields = normalize_changed_fields(command.changed_fields)
        scoped_idempotency_key = self._scoped_idempotency_key(command)
        payload_hash = self._payload_hash(
            command=command,
            changed_fields=changed_fields,
            scoped_idempotency_key=scoped_idempotency_key,
        )

        if scoped_idempotency_key:
            # Idempotency is scoped per tenant and logical entity operation.
            # Reusing a request key for a different mutation should fail loudly.
            existing = await self._get_existing_idempotency_event(scoped_idempotency_key)
            if existing:
                if existing.payload_hash == payload_hash:
                    result = CRMMutationRecordResult(
                        event_id=existing.event_id,
                        changed=True,
                        changed_fields=existing.changed_fields,
                        semantic_event_types=existing.semantic_event_types,
                    )
                    # Replay outbox registration too. Its source identity is unique, so this
                    # repairs a missing dispatch without duplicating worker activation.
                    await self._enqueue_supported_events(result)
                    return result
                raise CRMMutationEventServiceError(
                    "Idempotency key was already used for a different CRM mutation",
                    status_code=409,
                )

        # A missing group id means this write is a standalone user action. When
        # a facade performs multiple entity writes, it can pass one group id so
        # audit consumers can reconstruct the original action.
        event = CRMDealMutationEvent(
            event_id=uuid4(),
            organisation_id=self.organisation_id,
            event_type=command.event_type,
            semantic_event_types=command.semantic_event_types,
            mutation_group_id=command.context.mutation_group_id or uuid4(),
            entity_type=command.entity_type,
            entity_id=command.entity_id,
            deal_id=command.deal_id,
            operation=command.operation,
            occurred_at=datetime.now(UTC),
            changed_fields=changed_fields,
            summary=command.summary,
            integration_id=command.integration_id,
            provider=command.provider,
            source_object_mapping_id=command.source_object_mapping_id,
            actor_user_id=command.context.actor_user_id,
            source=command.context.source,
            source_label=command.context.source_label,
            approval_state_id=command.context.approval_state_id,
            correlation_id=command.context.correlation_id,
            idempotency_key=scoped_idempotency_key,
            payload_hash=payload_hash,
        )
        self.session.add(event)
        await self.session.flush()
        result = CRMMutationRecordResult(
            event_id=event.event_id,
            changed=True,
            changed_fields=changed_fields,
            semantic_event_types=event.semantic_event_types,
        )
        await self._enqueue_supported_events(result)
        return result

    async def _enqueue_supported_events(self, result: CRMMutationRecordResult) -> None:
        """Append supported semantics before the source transaction can commit."""

        if result.event_id is None:
            return
        try:
            for event_type in result.semantic_event_types:
                if event_type in CRM_AGENT_EVENT_SCHEMAS:
                    # Mutation and dispatch share this session. A rollback removes both,
                    # so recovery can never activate an event whose CRM write failed.
                    await self._automation_events.enqueue(
                        source_domain="crm",
                        source_event_id=str(result.event_id),
                        event_type=event_type,
                    )
        except AutomationServiceError as exc:
            raise CRMMutationEventServiceError(
                "Failed to enqueue CRM agent automation", status_code=500
            ) from exc

    async def _get_existing_idempotency_event(
        self, scoped_idempotency_key: str
    ) -> CRMDealMutationEvent | None:
        # The unique index is tenant-scoped; the org filter is the data-isolation
        # boundary and prevents a caller-supplied key from matching another org.
        stmt = select(CRMDealMutationEvent).where(
            col(CRMDealMutationEvent.organisation_id) == self.organisation_id,
            col(CRMDealMutationEvent.idempotency_key) == scoped_idempotency_key,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def latest_events_by_entity(
        self, entity_type: str, entity_ids: list[UUID]
    ) -> dict[UUID, CRMDealMutationEvent]:
        """Return the newest mutation event for each requested entity id."""
        if not entity_ids:
            return {}
        stmt = (
            select(CRMDealMutationEvent)
            .where(
                col(CRMDealMutationEvent.organisation_id) == self.organisation_id,
                col(CRMDealMutationEvent.entity_type) == entity_type,
                col(CRMDealMutationEvent.entity_id).in_(entity_ids),
            )
            .order_by(col(CRMDealMutationEvent.occurred_at).desc())
        )
        result = await self.session.execute(stmt)
        events: dict[UUID, CRMDealMutationEvent] = {}
        for event in result.scalars().all():
            events.setdefault(event.entity_id, event)
        return events

    @staticmethod
    def _payload_hash(
        *,
        command: CRMMutationRecordCommand,
        changed_fields: dict[str, dict[str, object]],
        scoped_idempotency_key: str | None,
    ) -> str:
        """Hash the retry-relevant mutation envelope stored in the audit row.

        Non-routing metadata such as summary text is intentionally excluded so a
        harmless wording change does not turn an otherwise identical retry into
        an idempotency conflict.
        """
        payload = {
            "entity_type": command.entity_type,
            "entity_id": str(command.entity_id),
            "event_type": command.event_type,
            "operation": command.operation,
            "changed_fields": changed_fields,
            "semantic_event_types": command.semantic_event_types,
            "source": command.context.source,
            "idempotency_key": scoped_idempotency_key,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    @staticmethod
    def _scoped_idempotency_key(command: CRMMutationRecordCommand) -> str | None:
        """Derive the storage key used for safe CRM write retries.

        The caller's request key is never stored raw. Hashing the request key
        with the operation scope keeps the value short, avoids leaking API input,
        and lets one request produce multiple entity events without collision.
        """
        request_key = command.context.idempotency_key
        if not request_key:
            return None

        # Default scope protects the common one-entity case; facades can provide
        # a narrower scope when one API request writes several related entities.
        scope = command.idempotency_scope or (
            f"{command.entity_type}:{command.operation}:{command.entity_id}"
        )
        digest = hashlib.sha256(f"{request_key}:{scope}".encode()).hexdigest()
        return f"crm-mut:{digest}"
