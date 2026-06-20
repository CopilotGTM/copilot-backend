from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.service.crm.deals.contracts import DealWriteContext
from src.service.crm.deals.write_service import DealWriteService


@pytest.mark.asyncio
async def test_record_mutation_event_uses_supplied_context_source() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    deal = SimpleNamespace(deal_id=uuid4(), integration_id=uuid4())
    service._mutation_events.record_event = AsyncMock()
    context = DealWriteContext(source="agent", actor_user_id=uuid4(), source_label="Chat agent")

    await service._record_mutation_event(
        deal=deal,
        event_type="crm.deal.updated",
        operation="update_deal",
        changed_fields={"amount": {"old": 100, "new": 200}},
        summary="Updated deal",
        context=context,
    )

    kwargs = service._mutation_events.record_event.await_args.kwargs
    assert kwargs["context"] is context


@pytest.mark.asyncio
async def test_record_mutation_event_defaults_to_api_context_for_legacy_callers() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    deal = SimpleNamespace(deal_id=uuid4(), integration_id=uuid4())
    service._mutation_events.record_event = AsyncMock()

    await service._record_mutation_event(
        deal=deal,
        event_type="crm.deal.updated",
        operation="update_deal",
        changed_fields={"amount": {"old": 100, "new": 200}},
        summary="Updated deal",
    )

    kwargs = service._mutation_events.record_event.await_args.kwargs
    assert kwargs["context"].source == "api"
    assert kwargs["context"].actor_user_id == service.actor_user_id
