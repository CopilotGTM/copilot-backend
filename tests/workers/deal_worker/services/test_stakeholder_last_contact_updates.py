"""Tests for stakeholder last_contact_at updates in matcher services."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.workers.deal_worker.services.deal_mapper_services.deal_email_matcher import (
    DealEmailMatcher,
)
from src.workers.deal_worker.services.deal_mapper_services.deal_meeting_matcher import (
    DealMeetingMatcher,
)


class _FakeScalarResult:
    def __init__(self, rows: list[SimpleNamespace]):
        self._rows = rows

    def all(self) -> list[SimpleNamespace]:
        return self._rows


class _FakeExecuteResult:
    def __init__(self, rows: list[SimpleNamespace]):
        self._rows = rows

    def scalars(self) -> _FakeScalarResult:
        return _FakeScalarResult(self._rows)


class _FakeSession:
    def __init__(self, rows: list[SimpleNamespace]):
        self._rows = rows

    async def execute(self, _stmt):  # noqa: ANN001 - SQLAlchemy statement type is not relevant here
        return _FakeExecuteResult(self._rows)


class _InsertConflictSession:
    def __init__(self, rows: list[SimpleNamespace]):
        self._rows = rows
        self.added: list[object] = []

    async def execute(self, stmt):  # noqa: ANN001 - SQLAlchemy statement type is not relevant here
        if "INSERT INTO calendar_event_deal_associations" in str(stmt):
            return SimpleNamespace(rowcount=0)
        return _FakeExecuteResult(self._rows)

    def add(self, obj: object) -> None:
        self.added.append(obj)


@pytest.mark.asyncio
async def test_email_matcher_updates_last_contact_only_when_newer() -> None:
    matcher = DealEmailMatcher(uuid4())

    now = datetime.now(UTC)
    contact_time = now - timedelta(days=2)
    newer_contact = now - timedelta(days=1)

    s1 = SimpleNamespace(last_contact_at=None, updated_at=None, email="a@acme.com")
    s2 = SimpleNamespace(
        last_contact_at=now - timedelta(days=10), updated_at=None, email="b@acme.com"
    )
    s3 = SimpleNamespace(last_contact_at=newer_contact, updated_at=None, email="c@acme.com")
    session = _FakeSession([s1, s2, s3])

    updated = await matcher._update_stakeholder_last_contact_from_email(  # noqa: SLF001
        session=session,
        deal_id=uuid4(),
        matched_emails=["a@acme.com", "b@acme.com", "c@acme.com"],
        contact_time=contact_time,
    )

    assert updated == 2
    assert s1.last_contact_at == contact_time
    assert s2.last_contact_at == contact_time
    assert s3.last_contact_at == newer_contact


def test_meeting_matcher_resolve_completed_contact_time_skips_upcoming() -> None:
    matcher = DealMeetingMatcher(uuid4())
    now = datetime.now(UTC)

    past_event = SimpleNamespace(start_at=now - timedelta(hours=2), end_at=now - timedelta(hours=1))
    upcoming_event = SimpleNamespace(
        start_at=now + timedelta(hours=1), end_at=now + timedelta(hours=2)
    )

    assert matcher._resolve_completed_contact_time(past_event) == past_event.end_at  # noqa: SLF001
    assert matcher._resolve_completed_contact_time(upcoming_event) is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_meeting_matcher_updates_last_contact_only_for_completed_events() -> None:
    matcher = DealMeetingMatcher(uuid4())
    now = datetime.now(UTC)

    stakeholder = SimpleNamespace(
        email="buyer@acme.com",
        last_contact_at=None,
        updated_at=None,
    )
    session = _FakeSession([stakeholder])

    completed_event = SimpleNamespace(
        start_at=now - timedelta(hours=2),
        end_at=now - timedelta(hours=1),
        organiser_email="owner@ourco.com",
        attendees=[SimpleNamespace(email="buyer@acme.com")],
    )
    upcoming_event = SimpleNamespace(
        start_at=now + timedelta(hours=1),
        end_at=now + timedelta(hours=2),
        organiser_email="owner@ourco.com",
        attendees=[SimpleNamespace(email="buyer@acme.com")],
    )

    updated_completed = await matcher._update_stakeholder_last_contact_from_event(  # noqa: SLF001
        session=session,
        deal_id=uuid4(),
        event=completed_event,
    )
    assert updated_completed == 1
    assert stakeholder.last_contact_at == completed_event.end_at

    previous_contact = stakeholder.last_contact_at
    updated_upcoming = await matcher._update_stakeholder_last_contact_from_event(  # noqa: SLF001
        session=session,
        deal_id=uuid4(),
        event=upcoming_event,
    )
    assert updated_upcoming == 0
    assert stakeholder.last_contact_at == previous_contact


@pytest.mark.asyncio
async def test_meeting_matcher_updates_last_contact_even_when_association_exists() -> None:
    matcher = DealMeetingMatcher(uuid4())
    now = datetime.now(UTC)
    completed_end = now - timedelta(minutes=10)

    stakeholder = SimpleNamespace(
        email="buyer@acme.com",
        last_contact_at=None,
        updated_at=None,
    )
    session = _InsertConflictSession([stakeholder])

    deal = SimpleNamespace(deal_id=uuid4())
    event = SimpleNamespace(
        event_id=uuid4(),
        integration_id=None,
        user_states=[],
        start_at=now - timedelta(hours=1),
        end_at=completed_end,
        organiser_email="owner@ourco.com",
        attendees=[SimpleNamespace(email="buyer@acme.com")],
    )

    linked_count = await matcher._create_associations(  # noqa: SLF001
        session=session,  # type: ignore[arg-type]
        deal=deal,  # type: ignore[arg-type]
        events=[event],  # type: ignore[arg-type]
        match_score=1.0,
        linked_event_ids=set(),
        customer_domains={"acme.com"},
        team_emails={"owner@ourco.com"},
    )

    assert linked_count == 0
    assert stakeholder.last_contact_at == completed_end
