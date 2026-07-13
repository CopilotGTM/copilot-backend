from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.service.crm.deals.last_activity_service import (
    ALLOWED_ACTIVITY_SOURCE_TYPES,
    DealLastActivityService,
    is_last_activity_mutation,
)


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    def __init__(self, values: list[object]):
        self.values = values
        self.statements: list[object] = []
        self.added: list[object] = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _FakeResult(self.values.pop(0))

    def add(self, obj):
        self.added.append(obj)


@pytest.mark.asyncio
async def test_refresh_deal_uses_latest_allowed_activity_or_mutation() -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    deal = SimpleNamespace(properties={"is_internal_crm": True}, last_activity_at=None)
    activity_at = datetime(2026, 7, 1, 10, tzinfo=UTC)
    mutation_at = datetime(2026, 7, 1, 12, tzinfo=UTC)
    session = _FakeSession([deal, activity_at, mutation_at])

    result = await DealLastActivityService(session, organisation_id).refresh_deal(deal_id)

    assert result == mutation_at
    assert deal.last_activity_at == mutation_at
    assert session.added == [deal]


@pytest.mark.asyncio
async def test_refresh_deal_skips_external_provider_deals() -> None:
    organisation_id = uuid4()
    session = _FakeSession([None])

    result = await DealLastActivityService(session, organisation_id).refresh_deal(uuid4())

    assert result is None
    assert len(session.statements) == 1


@pytest.mark.asyncio
async def test_internal_deal_lookup_accepts_internal_integration_marker() -> None:
    organisation_id = uuid4()
    deal = SimpleNamespace(properties={}, last_activity_at=None)
    session = _FakeSession([deal, None, None])

    await DealLastActivityService(session, organisation_id).refresh_deal(uuid4())

    lookup_sql = str(session.statements[0])
    assert "knowledge_integrations" in lookup_sql
    assert "integration_type" in lookup_sql


def test_last_activity_policy_is_allowlisted() -> None:
    assert "TASK" not in ALLOWED_ACTIVITY_SOURCE_TYPES
    assert "RISK_OPENED" not in ALLOWED_ACTIVITY_SOURCE_TYPES
    assert is_last_activity_mutation(["crm.deal.amount_changed"])
    assert is_last_activity_mutation(["crm.deal.expected_close_date_changed"])
    assert not is_last_activity_mutation(["crm.deal.account_changed"])
