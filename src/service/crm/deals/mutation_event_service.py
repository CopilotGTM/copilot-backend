"""CRM mutation-event recording for internal deal writes."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.service.crm.deals.contracts import DealWriteContext, DealWriteServiceError
from src.service.crm.mutations.contracts import (
    CRMMutationContext,
    CRMMutationEventServiceError,
    CRMMutationRecordCommand,
    CRMMutationRecordResult,
)
from src.service.crm.mutations.event_service import CRMMutationEventService
from src.service.crm.mutations.json_safe import changed_field

__all__ = ["DealMutationEventService", "changed_field"]


class DealMutationEventService:
    """Deal-specific facade over the shared CRM mutation event recorder."""

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self._events = CRMMutationEventService(session, organisation_id)

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
    ) -> CRMMutationRecordResult:
        command = CRMMutationRecordCommand(
            entity_type="deal",
            entity_id=deal.deal_id,
            deal_id=deal.deal_id,
            event_type=event_type,
            operation=operation,
            changed_fields=changed_fields,
            semantic_event_types=semantic_event_types,
            context=CRMMutationContext(
                source=context.source,
                actor_user_id=context.actor_user_id,
                source_label=context.source_label,
                approval_state_id=context.approval_state_id,
                correlation_id=context.correlation_id,
                idempotency_key=context.idempotency_key,
            ),
            summary=summary,
            integration_id=getattr(deal, "integration_id", None),
            idempotency_scope=f"deal:{operation}:{deal.deal_id}",
        )
        try:
            return await self._events.record_event(command)
        except CRMMutationEventServiceError as exc:
            raise DealWriteServiceError(exc.message, status_code=exc.status_code) from exc
