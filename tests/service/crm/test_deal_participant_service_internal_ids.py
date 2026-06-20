from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.service.crm.participants.contracts import ParticipantWriteResult
from src.service.crm.participants.participant_write_service import ParticipantWriteService


class _FakeResult:
    def __init__(self, rows: list[object]):
        self._rows = rows

    def scalars(self) -> _FakeResult:
        return self

    def first(self) -> object | None:
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, rows: list[object] | None = None):
        self._rows = rows or []
        self._added: list[object] = []
        self._objects: dict[object, object] = {}

    async def execute(self, _stmt):
        return _FakeResult(self._rows)

    async def get(self, _model, obj_id):
        return self._objects.get(obj_id)

    def add(self, obj: object) -> None:
        self._added.append(obj)


@pytest.mark.asyncio
async def test_maybe_apply_company_to_deal_sets_internal_company_id() -> None:
    session = _FakeSession()
    organisation_id = uuid4()
    company_id = uuid4()
    integration_id = uuid4()
    company = SimpleNamespace(
        company_id=company_id,
        organisation_id=organisation_id,
        integration_id=integration_id,
        name="Acme",
        domain="acme.com",
        is_deleted=False,
    )
    session._objects[company_id] = company

    deal = SimpleNamespace(
        company_id=None,
        company_name=None,
        company_domain=None,
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )

    service = ParticipantWriteService(
        session=session,
        organisation_id=organisation_id,
        actor_user_id=uuid4(),
    )

    now = datetime.now(UTC)
    await service._maybe_apply_company_to_deal(
        deal=deal,
        integration_id=integration_id,
        related_local_id=company_id,
        updated_at=now,
    )

    assert deal.company_id == company_id
    assert deal.company_name == "Acme"
    assert deal.company_domain == "acme.com"


@pytest.mark.asyncio
async def test_add_participants_reuses_contact_when_same_contact_has_multiple_roles() -> None:
    session = _FakeSession()
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()

    service = ParticipantWriteService(
        session=session,
        organisation_id=organisation_id,
        actor_user_id=uuid4(),
    )
    service._get_internal_deal = AsyncMock(  # type: ignore[method-assign]
        return_value=(SimpleNamespace(deal_id=deal_id), integration_id)
    )
    service._resolve_participant_entity = AsyncMock(  # type: ignore[method-assign]
        return_value=(contact_id, "Ada Lovelace", "ada@example.com")
    )
    service._find_existing_participant = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await service.add_participants(
        deal_id,
        [
            {
                "participant_type": CRMParticipantType.CONTACT.value,
                "related_id": contact_id,
                "relationship_role": "decision_maker",
            },
            {
                "participant_type": CRMParticipantType.CONTACT.value,
                "related_id": contact_id,
                "relationship_role": "primary_contact",
            },
        ],
    )

    created = result.created
    skipped = result.skipped
    changed = result.contact_participant_changed
    assert len(created) == 1
    assert len(skipped) == 1
    assert skipped[0].reason == "Duplicate participant in request"
    assert changed is True
    assert isinstance(created[0], CRMDealParticipant)
    assert created[0].is_primary_contact is True
    assert getattr(created[0], "properties", None) is None


@pytest.mark.asyncio
async def test_participant_write_service_returns_typed_result() -> None:
    session = _FakeSession()
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()

    service = ParticipantWriteService(
        session=session,
        organisation_id=organisation_id,
        actor_user_id=uuid4(),
    )
    service._get_internal_deal = AsyncMock(  # type: ignore[method-assign]
        return_value=(SimpleNamespace(deal_id=deal_id), integration_id)
    )
    service._resolve_participant_entity = AsyncMock(  # type: ignore[method-assign]
        return_value=(contact_id, "Ada Lovelace", "ada@example.com")
    )
    service._find_existing_participant = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await service.add_participants(
        deal_id,
        [
            {
                "participant_type": CRMParticipantType.CONTACT.value,
                "related_id": contact_id,
                "relationship_role": "decision_maker",
            },
            {
                "participant_type": CRMParticipantType.CONTACT.value,
                "related_id": contact_id,
                "relationship_role": "primary_contact",
            },
        ],
    )

    assert isinstance(result, ParticipantWriteResult)
    assert len(result.created) == 1
    assert len(result.skipped) == 1
    assert result.skipped[0].reason == "Duplicate participant in request"
    assert result.contact_participant_changed is True
    assert result.integration_id == integration_id
    assert result.created[0].is_primary_contact is True
