"""CRM mutation-event recording for internal deal writes."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealMutationEvent
from src.service.crm.deals.contracts import DealWriteContext, DealWriteServiceError


def changed_field(old: Any, new: Any) -> dict[str, Any] | None:
    """Return an old/new payload only when a field actually changed."""
    if old == new:
        return None
    return {"old": old, "new": new}


class DealMutationEventService:
    """Writes immutable CRM mutation events for committed internal deal changes."""

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    async def record_event(
        self,
        *,
        deal: CRMDeal,
        event_type: str,
        operation: str,
        changed_fields: dict[str, dict[str, Any]],
        semantic_event_types: list[str],
        context: DealWriteContext,
        summary: str | None,
    ) -> CRMDealMutationEvent | None:
        """Persist one mutation event for one top-level deal write."""
        if not changed_fields:
            return None

        payload_hash = self._payload_hash(
            deal=deal,
            event_type=event_type,
            changed_fields=changed_fields,
            semantic_event_types=semantic_event_types,
        )

        if context.idempotency_key:
            existing = await self._get_existing_idempotency_event(context.idempotency_key)
            if existing:
                if existing.payload_hash == payload_hash:
                    return existing
                raise DealWriteServiceError(
                    "Idempotency key was already used for a different deal mutation",
                    status_code=409,
                )

        event = CRMDealMutationEvent(
            event_id=uuid4(),
            organisation_id=self.organisation_id,
            event_type=event_type,
            semantic_event_types=semantic_event_types,
            mutation_group_id=uuid4(),
            entity_type="deal",
            entity_id=deal.deal_id,
            deal_id=deal.deal_id,
            operation=operation,
            occurred_at=datetime.now(UTC),
            changed_fields=changed_fields,
            summary=summary,
            integration_id=getattr(deal, "integration_id", None),
            provider=None,
            source_object_mapping_id=None,
            actor_user_id=context.actor_user_id,
            source=context.source,
            source_label=context.source_label,
            approval_state_id=context.approval_state_id,
            correlation_id=context.correlation_id,
            idempotency_key=context.idempotency_key,
            payload_hash=payload_hash,
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def _get_existing_idempotency_event(
        self, idempotency_key: str
    ) -> CRMDealMutationEvent | None:
        # The uniqueness contract is tenant-scoped, so the org filter is the trust boundary.
        stmt = select(CRMDealMutationEvent).where(
            col(CRMDealMutationEvent.organisation_id) == self.organisation_id,
            col(CRMDealMutationEvent.idempotency_key) == idempotency_key,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    def _payload_hash(
        *,
        deal: CRMDeal,
        event_type: str,
        changed_fields: dict[str, dict[str, Any]],
        semantic_event_types: list[str],
    ) -> str:
        payload = {
            "event_type": event_type,
            "deal_id": str(deal.deal_id),
            "changed_fields": changed_fields,
            "semantic_event_types": semantic_event_types,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
