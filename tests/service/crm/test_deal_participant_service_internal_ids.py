from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.service.crm.internal.deal_participant_service import DealParticipantService


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

    service = DealParticipantService(
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
