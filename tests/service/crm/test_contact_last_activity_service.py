"""Regression coverage for Contact communication and activity timestamps."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.service.crm.contacts.last_activity_service import (
    ACTIVITY_SOURCE_TYPES,
    CONTACTED_SOURCE_TYPES,
    ContactLastActivityService,
)


class _FakeResult:
    """Expose the small SQLAlchemy result surface used by the service."""

    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        """Return the configured scalar result."""
        return self._value

    def one(self):
        """Return the configured aggregate row."""
        return self._value


class _FakeSession:
    """Feed deterministic query results and capture the persisted Contact."""

    def __init__(self, values: list[object]):
        self.values = values
        self.statements: list[object] = []
        self.added: list[object] = []

    async def execute(self, statement):
        """Return the next result in the service query sequence."""
        self.statements.append(statement)
        return _FakeResult(self.values.pop(0))

    def add(self, obj):
        """Capture the Contact scheduled for persistence."""
        self.added.append(obj)


@pytest.mark.asyncio
async def test_note_advances_activity_without_advancing_contacted() -> None:
    """Notes are activity, while only direct communication marks a Contact contacted."""
    contact = SimpleNamespace(last_contacted_at=None, last_activity_at=None)
    email_at = datetime(2026, 7, 1, 10, tzinfo=UTC)
    note_at = datetime(2026, 7, 1, 12, tzinfo=UTC)
    mapping = SimpleNamespace(
        provider="hubspot",
        data_payload={
            "properties": {
                "notes_last_contacted": "2026-06-30T10:00:00Z",
                "notes_last_updated": "2026-06-30T12:00:00Z",
            }
        },
    )
    session = _FakeSession([contact, (email_at, note_at), mapping])

    result = await ContactLastActivityService(session, uuid4()).refresh_contact(uuid4())

    assert result is contact
    assert contact.last_contacted_at == email_at
    assert contact.last_activity_at == note_at
    assert session.added == [contact]


@pytest.mark.asyncio
async def test_newer_provider_activity_wins_over_copilot_timeline() -> None:
    """Salesforce activity remains useful when its latest event is not projected locally."""
    contact = SimpleNamespace(last_contacted_at=None, last_activity_at=None)
    email_at = datetime(2026, 7, 1, tzinfo=UTC)
    mapping = SimpleNamespace(
        provider="salesforce",
        data_payload={"LastActivityDate": "2026-07-03"},
    )
    session = _FakeSession([contact, (email_at, email_at), mapping])

    await ContactLastActivityService(session, uuid4()).refresh_contact(uuid4())

    assert contact.last_contacted_at == email_at
    assert contact.last_activity_at == datetime(2026, 7, 3, tzinfo=UTC)


@pytest.mark.asyncio
async def test_refresh_filters_future_deleted_and_unapproved_activity_types() -> None:
    """Only past, active, explicitly allowed timeline rows can change freshness."""
    contact = SimpleNamespace(last_contacted_at=None, last_activity_at=None)
    session = _FakeSession([contact, (None, None), None])

    await ContactLastActivityService(session, uuid4()).refresh_contact(uuid4())

    timeline_sql = str(session.statements[1])
    assert "crm_activity_links.occurred_at <= now()" in timeline_sql
    assert "crm_activities.is_deleted IS false" in timeline_sql
    assert "TASK" not in ACTIVITY_SOURCE_TYPES
    assert CONTACTED_SOURCE_TYPES < ACTIVITY_SOURCE_TYPES


@pytest.mark.asyncio
async def test_refresh_stops_for_missing_or_deleted_contact() -> None:
    """An unknown Contact cannot trigger cross-tenant timeline or provider lookups."""
    session = _FakeSession([None])

    result = await ContactLastActivityService(session, uuid4()).refresh_contact(uuid4())

    assert result is None
    assert len(session.statements) == 1
    assert session.added == []
