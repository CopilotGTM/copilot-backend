"""Mutation-event tests for one-event-per-deal-write semantics."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealMutationEvent
from src.service.crm.deals.contracts import DealWriteContext, DealWriteServiceError
from src.service.crm.deals.mutation_event_service import DealMutationEventService


class _ScalarResult:
    def __init__(self, row: object | None) -> None:
        self._row = row

    def scalar_one_or_none(self) -> object | None:
        """Return the configured row like SQLAlchemy scalar results do."""
        return self._row


class _FakeSession:
    def __init__(self, existing: object | None = None) -> None:
        self.existing = existing
        self.added: list[object] = []

    async def execute(self, _statement: object) -> _ScalarResult:
        """Return a scalar result with the configured idempotency row."""
        return _ScalarResult(self.existing)

    def add(self, obj: object) -> None:
        """Capture rows the service would add to the active transaction."""
        self.added.append(obj)

    async def flush(self) -> None:
        """Match the async session flush contract without touching a database."""
        return None


class _FakeActivationService:
    def __init__(self) -> None:
        self.event_ids: list[object] = []

    async def activate_crm_event(self, event_id: object) -> None:
        self.event_ids.append(event_id)


def _deal() -> SimpleNamespace:
    return SimpleNamespace(deal_id=uuid4(), integration_id=uuid4())


def _context(idempotency_key: str | None = None) -> DealWriteContext:
    return DealWriteContext(
        source="api",
        actor_user_id=uuid4(),
        idempotency_key=idempotency_key,
    )


def test_deal_event_service_uses_scoped_idempotency_key() -> None:
    source = Path("src/service/crm/deals/mutation_event_service.py").read_text()

    assert "CRMMutationEventService" in source
    assert "CRMMutationRecordCommand" in source
    assert 'idempotency_scope=f"deal:' in source


def test_deal_event_service_does_not_hash_payload_locally() -> None:
    source = Path("src/service/crm/deals/mutation_event_service.py").read_text()

    assert "hashlib" not in source
    assert "json.dumps" not in source


@pytest.mark.asyncio
async def test_record_event_skips_empty_changed_fields() -> None:
    """No-op writes must not produce audit rows or workflow trigger input."""
    service = DealMutationEventService(_FakeSession(), uuid4())

    result = await service.record_event(
        deal=_deal(),
        event_type="crm.deal.updated",
        operation="update_deal",
        changed_fields={},
        semantic_event_types=[],
        context=_context(),
        summary=None,
    )

    assert result.changed is False
    assert result.event_id is None
    assert service.session.added == []


@pytest.mark.asyncio
async def test_record_event_persists_one_event_for_multi_field_update() -> None:
    """A multi-field deal update must be stored as one mutation event row."""
    activation = _FakeActivationService()
    service = DealMutationEventService(_FakeSession(), uuid4(), activation_service=activation)

    result = await service.record_event(
        deal=_deal(),
        event_type="crm.deal.updated",
        operation="update_deal",
        changed_fields={
            "amount": {"old": 100, "new": 200},
            "stage_id": {"old": "discovery", "new": "proposal"},
        },
        semantic_event_types=["crm.deal.amount_changed", "crm.deal.stage_changed"],
        context=_context(),
        summary="Updated deal",
    )

    assert result.changed is True
    event = service.session.added[0]
    assert isinstance(event, CRMDealMutationEvent)
    assert event.event_type == "crm.deal.updated"
    assert set(event.changed_fields) == {"amount", "stage_id"}
    assert event.semantic_event_types == ["crm.deal.amount_changed", "crm.deal.stage_changed"]
    assert service.session.added == [event]
    assert activation.event_ids == [event.event_id]


@pytest.mark.asyncio
async def test_record_event_does_not_activate_unrelated_semantic_event() -> None:
    """Only the supported stage-change semantic event enters trigger activation."""
    activation = _FakeActivationService()
    service = DealMutationEventService(_FakeSession(), uuid4(), activation_service=activation)

    result = await service.record_event(
        deal=_deal(),
        event_type="crm.deal.updated",
        operation="update_deal",
        changed_fields={"amount": {"old": 100, "new": 200}},
        semantic_event_types=["crm.deal.amount_changed"],
        context=_context(),
        summary="Updated deal",
    )

    assert result.changed is True
    assert activation.event_ids == []


def test_record_event_does_not_store_workflow_runtime_state() -> None:
    """The mutation ledger must not become a workflow dispatch runtime table."""
    event = CRMDealMutationEvent(
        event_id=uuid4(),
        organisation_id=uuid4(),
        event_type="crm.deal.updated",
        semantic_event_types=[],
        mutation_group_id=uuid4(),
        entity_type="deal",
        entity_id=uuid4(),
        operation="update_deal",
        changed_fields={"amount": {"old": 100, "new": 200}},
        source="api",
    )

    assert not hasattr(event, "workflow_run_id")
    assert not hasattr(event, "dispatch_status")
    assert not hasattr(event, "attempt_count")


@pytest.mark.asyncio
async def test_record_event_returns_existing_event_for_same_idempotency_payload() -> None:
    """Safe retries with the same payload should return the existing event."""
    deal = _deal()
    changed_fields = {"amount": {"old": 100, "new": 200}}
    service = DealMutationEventService(_FakeSession(), uuid4())
    await service.record_event(
        deal=deal,
        event_type="crm.deal.updated",
        operation="update_deal",
        changed_fields=changed_fields,
        semantic_event_types=["crm.deal.amount_changed"],
        context=_context("retry-key"),
        summary="Updated deal",
    )
    first = service.session.added[0]

    retry_session = _FakeSession(existing=first)
    retry_service = DealMutationEventService(retry_session, service.organisation_id)
    second = await retry_service.record_event(
        deal=deal,
        event_type="crm.deal.updated",
        operation="update_deal",
        changed_fields=changed_fields,
        semantic_event_types=["crm.deal.amount_changed"],
        context=_context("retry-key"),
        summary="Updated deal",
    )

    assert second.event_id == first.event_id
    assert retry_session.added == []


@pytest.mark.asyncio
async def test_record_event_rejects_same_idempotency_key_with_different_payload() -> None:
    """Reusing an idempotency key for a different mutation is a conflict."""
    existing = CRMDealMutationEvent(
        event_id=uuid4(),
        organisation_id=uuid4(),
        event_type="crm.deal.updated",
        semantic_event_types=["crm.deal.amount_changed"],
        mutation_group_id=uuid4(),
        entity_type="deal",
        entity_id=uuid4(),
        operation="update_deal",
        changed_fields={"amount": {"old": 100, "new": 200}},
        source="api",
        idempotency_key="retry-key",
        payload_hash="different",
    )
    service = DealMutationEventService(_FakeSession(existing=existing), existing.organisation_id)

    with pytest.raises(DealWriteServiceError) as exc:
        await service.record_event(
            deal=_deal(),
            event_type="crm.deal.updated",
            operation="update_deal",
            changed_fields={"amount": {"old": 100, "new": 300}},
            semantic_event_types=["crm.deal.amount_changed"],
            context=_context("retry-key"),
            summary="Updated deal",
        )

    assert exc.value.status_code == 409
