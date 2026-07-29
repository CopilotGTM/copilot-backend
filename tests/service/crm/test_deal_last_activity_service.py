"""Regression coverage for the cross-provider deal last-activity policy."""

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
        """Mirror the SQLAlchemy result method used by the service."""
        return self._value


class _FakeSession:
    def __init__(self, values: list[object]):
        self.values = values
        self.statements: list[object] = []
        self.added: list[object] = []

    async def execute(self, statement):
        """Return queued scalars while retaining SQL for policy assertions."""
        self.statements.append(statement)
        return _FakeResult(self.values.pop(0))

    def add(self, obj):
        """Record the deal scheduled for persistence by the refresh path."""
        self.added.append(obj)


@pytest.mark.asyncio
async def test_refresh_deal_uses_latest_allowed_activity_or_mutation() -> None:
    """A newer CopilotGTM mutation wins and records explainable provenance."""
    organisation_id = uuid4()
    deal_id = uuid4()
    deal = SimpleNamespace(properties={"is_internal_crm": True}, last_activity_at=None)
    activity_at = datetime(2026, 7, 1, 10, tzinfo=UTC)
    mutation_at = datetime(2026, 7, 1, 12, tzinfo=UTC)
    session = _FakeSession([deal, activity_at, mutation_at, None])

    result = await DealLastActivityService(session, organisation_id).refresh_deal(deal_id)

    assert result == mutation_at
    assert deal.last_activity_at == mutation_at
    assert deal.properties["last_activity_source"] == "copilot"
    assert session.added == [deal]
    activity_sql = str(session.statements[1])
    assert "crm_activity_links.occurred_at <= now()" in activity_sql


@pytest.mark.asyncio
async def test_refresh_deal_accepts_external_deal_and_prefers_newer_email() -> None:
    """A Salesforce deal uses its newer linked email instead of provider fallback."""
    organisation_id = uuid4()
    deal = SimpleNamespace(properties={}, last_activity_at=None)
    email_at = datetime(2026, 7, 24, 10, 5, tzinfo=UTC)
    mapping = SimpleNamespace(
        provider="salesforce",
        data_payload={"LastActivityDate": "2026-07-03"},
    )
    session = _FakeSession([deal, email_at, None, mapping])

    result = await DealLastActivityService(session, organisation_id).refresh_deal(uuid4())

    assert result == email_at
    assert deal.properties["last_activity_source"] == "copilot"
    lookup_sql = str(session.statements[0])
    assert "knowledge_integrations" not in lookup_sql
    assert "crm_deals.is_deleted IS false" in lookup_sql


@pytest.mark.asyncio
async def test_refresh_deal_uses_provider_when_copilot_activity_is_older() -> None:
    """Provider-only activity prevents a stale or empty CopilotGTM timestamp."""
    organisation_id = uuid4()
    deal = SimpleNamespace(properties={}, last_activity_at=None)
    copilot_at = datetime(2026, 7, 2, tzinfo=UTC)
    provider_at = datetime(2026, 7, 3, tzinfo=UTC)
    mapping = SimpleNamespace(
        provider="salesforce",
        data_payload={"LastActivityDate": "2026-07-03"},
    )
    session = _FakeSession([deal, copilot_at, None, mapping])

    result = await DealLastActivityService(session, organisation_id).refresh_deal(uuid4())

    assert result == provider_at
    assert deal.properties["last_activity_source"] == "salesforce"


@pytest.mark.asyncio
async def test_refresh_deal_prefers_copilot_on_equal_timestamp() -> None:
    """Equal dates use CopilotGTM provenance because the timeline explains it."""
    organisation_id = uuid4()
    deal = SimpleNamespace(properties={}, last_activity_at=None)
    occurred_at = datetime(2026, 7, 3, tzinfo=UTC)
    mapping = SimpleNamespace(
        provider="hubspot",
        data_payload={"properties": {"lastactivitydate": "2026-07-03T00:00:00Z"}},
    )
    session = _FakeSession([deal, occurred_at, None, mapping])

    await DealLastActivityService(session, organisation_id).refresh_deal(uuid4())

    assert deal.last_activity_at == occurred_at
    assert deal.properties["last_activity_source"] == "copilot"


@pytest.mark.asyncio
async def test_refresh_deal_ignores_malformed_provider_timestamp() -> None:
    """A bad provider field cannot break refresh or preserve stale provenance."""
    organisation_id = uuid4()
    deal = SimpleNamespace(
        properties={"last_activity_source": "salesforce"},
        last_activity_at=datetime(2026, 7, 3, tzinfo=UTC),
    )
    mapping = SimpleNamespace(
        provider="salesforce",
        data_payload={"LastActivityDate": "not-a-date"},
    )
    session = _FakeSession([deal, None, None, mapping])

    result = await DealLastActivityService(session, organisation_id).refresh_deal(uuid4())

    assert result is None
    assert "last_activity_source" not in deal.properties


@pytest.mark.asyncio
async def test_refresh_deal_skips_missing_or_deleted_deal() -> None:
    """Unknown rows stop before provider/activity queries or writes."""
    organisation_id = uuid4()
    session = _FakeSession([None])

    result = await DealLastActivityService(session, organisation_id).refresh_deal(uuid4())

    assert result is None
    assert len(session.statements) == 1
    assert session.added == []


def test_last_activity_policy_is_allowlisted() -> None:
    """Only explicit communication and mutation semantics affect freshness."""
    assert "TASK" not in ALLOWED_ACTIVITY_SOURCE_TYPES
    assert "RISK_OPENED" not in ALLOWED_ACTIVITY_SOURCE_TYPES
    assert is_last_activity_mutation(["crm.deal.amount_changed"])
    assert is_last_activity_mutation(["crm.deal.expected_close_date_changed"])
    assert not is_last_activity_mutation(["crm.deal.account_changed"])
