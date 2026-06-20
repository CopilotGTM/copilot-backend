"""Shared CRM mutation-event foundation tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealMutationEvent
from src.service.crm.mutations.contracts import (
    CRMMutationContext,
    CRMMutationEventServiceError,
    CRMMutationRecordCommand,
)
from src.service.crm.mutations.event_service import CRMMutationEventService
from src.service.crm.mutations.json_safe import normalize_changed_fields


class _SampleStage(str, Enum):
    PROPOSAL = "proposal"


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
        """Match AsyncSession.flush without touching a database."""
        return None


def _command(
    *,
    entity_id=None,
    changed_fields: dict | None = None,
    idempotency_key: str | None = None,
    idempotency_scope: str | None = None,
    mutation_group_id=None,
) -> CRMMutationRecordCommand:
    entity_id = entity_id or uuid4()
    return CRMMutationRecordCommand(
        entity_type="contact",
        entity_id=entity_id,
        event_type="crm.contact.updated",
        operation="update_contact",
        changed_fields=(
            changed_fields
            if changed_fields is not None
            else {"email": {"old": None, "new": "buyer@example.com"}}
        ),
        semantic_event_types=["crm.contact.email_added"],
        context=CRMMutationContext(
            source="api",
            actor_user_id=uuid4(),
            idempotency_key=idempotency_key,
            mutation_group_id=mutation_group_id,
        ),
        summary="Updated contact",
        idempotency_scope=idempotency_scope,
    )


def test_json_safe_values_are_normalized() -> None:
    """Mutation payloads must be stored as JSON-safe audit values."""
    value_id = uuid4()
    normalized = normalize_changed_fields(
        {
            "id": {"old": None, "new": value_id},
            "closed_at": {"old": None, "new": datetime(2026, 6, 20, 9, 30, tzinfo=UTC)},
            "renewal_date": {"old": None, "new": date(2026, 7, 1)},
            "amount": {"old": Decimal("123.45"), "new": Decimal("200.00")},
            "stage": {"old": None, "new": _SampleStage.PROPOSAL},
            "tags": {"old": [], "new": [value_id, Decimal("1.50")]},
            "meta": {"old": {}, "new": {"owner": value_id}},
            "cleared": {"old": "value", "new": None},
        }
    )

    assert normalized["id"]["new"] == str(value_id)
    assert normalized["closed_at"]["new"] == "2026-06-20T09:30:00+00:00"
    assert normalized["renewal_date"]["new"] == "2026-07-01"
    assert normalized["amount"] == {"old": "123.45", "new": "200.00"}
    assert normalized["stage"]["new"] == "proposal"
    assert normalized["tags"]["new"] == [str(value_id), "1.50"]
    assert normalized["meta"]["new"] == {"owner": str(value_id)}
    assert normalized["cleared"] == {"old": "value", "new": None}


def test_json_safe_rejects_sensitive_fields() -> None:
    """Audit payloads must not persist credentials or bearer material."""
    with pytest.raises(CRMMutationEventServiceError) as exc:
        normalize_changed_fields({"api_token": {"old": None, "new": "secret"}})

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_record_event_skips_empty_changed_fields() -> None:
    """No-op writes should not produce audit rows or future workflow input."""
    service = CRMMutationEventService(_FakeSession(), uuid4())

    result = await service.record_event(_command(changed_fields={}))

    assert result.event_id is None
    assert result.changed is False
    assert service.session.added == []


@pytest.mark.asyncio
async def test_record_event_persists_generic_crm_event() -> None:
    """The shared service should hide the current deal-named ORM class."""
    organisation_id = uuid4()
    service = CRMMutationEventService(_FakeSession(), organisation_id)

    result = await service.record_event(_command(idempotency_key="req-123"))

    assert result.event_id is not None
    assert result.changed is True
    event = service.session.added[0]
    assert isinstance(event, CRMDealMutationEvent)
    assert event.organisation_id == organisation_id
    assert event.entity_type == "contact"
    assert event.event_type == "crm.contact.updated"
    assert event.idempotency_key != "req-123"
    assert len(event.idempotency_key) <= 128
    assert event.payload_hash


@pytest.mark.asyncio
async def test_record_event_returns_existing_event_for_same_scoped_idempotency_payload() -> None:
    """Safe retries with the same scoped payload should return the original event id."""
    session = _FakeSession()
    service = CRMMutationEventService(session, uuid4())
    command = _command(idempotency_key="req-123", idempotency_scope="contact:update:1")
    first = await service.record_event(command)

    retry_session = _FakeSession(existing=session.added[0])
    retry_service = CRMMutationEventService(retry_session, service.organisation_id)
    second = await retry_service.record_event(command)

    assert second.event_id == first.event_id
    assert retry_session.added == []


@pytest.mark.asyncio
async def test_record_event_rejects_same_scoped_idempotency_key_with_different_payload() -> None:
    """Reusing a scoped idempotency key for a different mutation is a conflict."""
    session = _FakeSession()
    service = CRMMutationEventService(session, uuid4())
    command = _command(idempotency_key="req-123", idempotency_scope="contact:update:1")
    await service.record_event(command)

    retry_service = CRMMutationEventService(
        _FakeSession(existing=session.added[0]), service.organisation_id
    )
    with pytest.raises(CRMMutationEventServiceError) as exc:
        await retry_service.record_event(
            _command(
                entity_id=command.entity_id,
                changed_fields={"email": {"old": None, "new": "new@example.com"}},
                idempotency_key="req-123",
                idempotency_scope="contact:update:1",
            )
        )

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_same_request_idempotency_key_allows_different_event_scopes() -> None:
    """One API action can create more than one entity event in the same transaction."""
    session = _FakeSession()
    service = CRMMutationEventService(session, uuid4())

    first = await service.record_event(
        _command(idempotency_key="req-123", idempotency_scope="account:create:1")
    )
    second = await service.record_event(
        _command(idempotency_key="req-123", idempotency_scope="contact:create:1")
    )

    first_event = session.added[0]
    second_event = session.added[1]
    assert first.event_id != second.event_id
    assert first_event.idempotency_key != second_event.idempotency_key


@pytest.mark.asyncio
async def test_record_event_preserves_caller_mutation_group_id() -> None:
    """Events from one user action need a shared group id for audit tracing."""
    mutation_group_id = uuid4()
    session = _FakeSession()
    service = CRMMutationEventService(session, uuid4())

    await service.record_event(
        _command(
            idempotency_key="req-123",
            idempotency_scope="account:create:1",
            mutation_group_id=mutation_group_id,
        )
    )
    await service.record_event(
        _command(
            idempotency_key="req-123",
            idempotency_scope="contact:create:1",
            mutation_group_id=mutation_group_id,
        )
    )

    assert session.added[0].mutation_group_id == mutation_group_id
    assert session.added[1].mutation_group_id == mutation_group_id
