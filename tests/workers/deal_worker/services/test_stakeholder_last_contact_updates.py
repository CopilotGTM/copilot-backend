"""Tests for stakeholder last_contact_at updates in matcher services."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarEvent,
)
from src.domain.models.organisation_tables.sync.email_models import (
    DealEmailDirection,
    Email,
    EmailDirection,
)
from src.workers.deal_worker.services.deal_mapper_services.deal_email_matcher import (
    DealEmailMatcher,
    EmailMatchResult,
)
from src.workers.deal_worker.services.deal_mapper_services.deal_meeting_matcher import (
    DealMeetingMatcher,
)


class _FakeScalarResult:
    def __init__(self, rows: list[SimpleNamespace]):
        self._rows = rows

    def all(self) -> list[SimpleNamespace]:
        """Return scripted stakeholder rows for matcher update scans."""
        return self._rows


class _FakeExecuteResult:
    def __init__(self, rows: list[SimpleNamespace]):
        self._rows = rows

    def scalars(self) -> _FakeScalarResult:
        """Expose fake scalar rows through the SQLAlchemy result shape."""
        return _FakeScalarResult(self._rows)


class _FakeSession:
    def __init__(self, rows: list[SimpleNamespace]):
        self._rows = rows

    async def execute(self, _stmt):  # noqa: ANN001 - SQLAlchemy statement type is not relevant here
        """Return scripted rows for read-only matcher queries."""
        return _FakeExecuteResult(self._rows)


class _InsertConflictSession:
    def __init__(self, rows: list[SimpleNamespace]):
        self._rows = rows
        self.added: list[object] = []

    async def execute(self, stmt):  # noqa: ANN001 - SQLAlchemy statement type is not relevant here
        """Simulate a duplicate insert while preserving later stakeholder lookups."""
        if "INSERT INTO calendar_event_deal_associations" in str(stmt):
            return SimpleNamespace(rowcount=0)
        return _FakeExecuteResult(self._rows)

    def add(self, obj: object) -> None:
        """Capture ORM side effects that would normally be added to the session."""
        self.added.append(obj)


class _InsertCreatedSession:
    def __init__(self):
        self.added: list[object] = []

    async def execute(self, stmt):  # noqa: ANN001 - SQLAlchemy statement type is not relevant here
        """Simulate a successful association insert for projection-call tests."""
        statement_text = str(stmt)
        if "INSERT INTO" in statement_text:
            return SimpleNamespace(rowcount=1)
        return _FakeExecuteResult([])

    def add(self, obj: object) -> None:
        """Capture ORM side effects so tests can inspect transaction writes."""
        self.added.append(obj)


@pytest.mark.asyncio
async def test_email_matcher_updates_last_contact_only_when_newer() -> None:
    """Email contact time only advances stakeholders with missing or older timestamps."""
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
    """Upcoming meetings must not update stakeholder recency before they happen."""
    matcher = DealMeetingMatcher(uuid4())
    now = datetime.now(UTC)

    past_event = SimpleNamespace(start_at=now - timedelta(hours=2), end_at=now - timedelta(hours=1))
    upcoming_event = SimpleNamespace(
        start_at=now + timedelta(hours=1), end_at=now + timedelta(hours=2)
    )

    assert matcher._resolve_completed_contact_time(past_event) == past_event.end_at  # noqa: SLF001
    assert matcher._resolve_completed_contact_time(upcoming_event) is None  # noqa: SLF001


def test_email_matcher_collects_normalized_account_domain() -> None:
    """Email matching accepts account website values as normalized customer domains."""
    matcher = DealEmailMatcher(uuid4())
    deal = SimpleNamespace(
        primary_contact_email=None,
        participants=[],
        company_domain="https://www.Acme.com/products?ref=crm",
    )

    assert matcher._collect_company_domains(deal) == {"acme.com"}  # noqa: SLF001


def test_meeting_matcher_collects_normalized_account_domain() -> None:
    """Meeting matching accepts account website values as normalized customer domains."""
    matcher = DealMeetingMatcher(uuid4())
    deal = SimpleNamespace(
        primary_contact_email=None,
        participants=[],
        company_domain="https://www.Acme.com/products?ref=crm",
    )

    assert matcher._collect_customer_domains(deal) == {"acme.com"}  # noqa: SLF001


@pytest.mark.asyncio
async def test_meeting_matcher_updates_last_contact_only_for_completed_events() -> None:
    """Meeting recency updates use completed events and ignore future meetings."""
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
    """Duplicate meeting links still refresh stakeholder recency for completed meetings."""
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


@pytest.mark.asyncio
async def test_email_matcher_projects_activity_after_association_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newly inserted email association must refresh the activity-feed projection."""
    organisation_id = uuid4()
    matcher = DealEmailMatcher(organisation_id)
    session = _InsertCreatedSession()
    email = Email(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        folder_id=uuid4(),
        external_message_id="provider-message-1",
        internet_message_id="<message-1@example.com>",
        from_email="buyer@example.com",
        to_emails=["seller@ourco.com"],
        subject="Pricing follow-up",
        received_at=datetime(2026, 6, 22, 14, 15, tzinfo=UTC),
        direction=EmailDirection.INBOUND,
    )
    match = EmailMatchResult(
        deal_id=uuid4(),
        score=0.9,
        direction=DealEmailDirection.INBOUND,
        matched_emails=["buyer@example.com"],
    )
    projected: list[tuple[object, object]] = []

    class _ProjectionService:
        def __init__(self, projected_session, projected_org_id):  # noqa: ANN001
            assert projected_session is session
            assert projected_org_id == organisation_id

        async def upsert_email_association(self, association, email_record):  # noqa: ANN001
            """Record the feed projection call made after the email insert."""
            projected.append((association, email_record))

    monkeypatch.setattr(
        "src.workers.deal_worker.services.deal_mapper_services.deal_email_matcher."
        "ActivityFeedProjectionService",
        _ProjectionService,
    )

    association = await matcher._create_association(  # noqa: SLF001
        session,  # type: ignore[arg-type]
        email,
        match,
    )

    assert association is not None
    assert projected == [(association, email)]


@pytest.mark.asyncio
async def test_meeting_matcher_projects_activity_after_association_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newly inserted meeting association must refresh the activity-feed projection."""
    organisation_id = uuid4()
    matcher = DealMeetingMatcher(organisation_id)
    session = _InsertCreatedSession()
    deal = SimpleNamespace(deal_id=uuid4())
    event = CalendarEvent(
        organisation_id=organisation_id,
        event_id=uuid4(),
        external_id="event-1",
        title="Renewal review",
        start_at=datetime(2026, 6, 22, 15, 0, tzinfo=UTC),
        end_at=datetime(2026, 6, 22, 15, 45, tzinfo=UTC),
    )
    event.attendees = []
    event.user_states = []
    projected: list[tuple[object, object]] = []

    class _ProjectionService:
        def __init__(self, projected_session, projected_org_id):  # noqa: ANN001
            assert projected_session is session
            assert projected_org_id == organisation_id

        async def upsert_meeting_association(self, association, event_record):  # noqa: ANN001
            """Record the feed projection call made after the meeting insert."""
            projected.append((association, event_record))

    monkeypatch.setattr(
        "src.workers.deal_worker.services.deal_mapper_services.deal_meeting_matcher."
        "ActivityFeedProjectionService",
        _ProjectionService,
    )
    monkeypatch.setattr(matcher, "_enqueue_meeting_recording", _async_false)
    monkeypatch.setattr(matcher, "_discover_stakeholders_from_event", _async_zero)
    monkeypatch.setattr(matcher, "_update_stakeholder_last_contact_from_event", _async_zero)

    linked_count = await matcher._create_associations(  # noqa: SLF001
        session=session,  # type: ignore[arg-type]
        deal=deal,  # type: ignore[arg-type]
        events=[event],
        match_score=1.0,
        linked_event_ids=set(),
        customer_domains=set(),
        team_emails=set(),
    )

    assert linked_count == 1
    assert len(projected) == 1
    association, projected_event = projected[0]
    assert association.organisation_id == organisation_id
    assert association.deal_id == deal.deal_id
    assert association.event_id == event.event_id
    assert association.status == AssociationStatus.ACTIVE
    assert projected_event is event


async def _async_false(*_args, **_kwargs):  # noqa: ANN002, ANN003
    return False


async def _async_zero(*_args, **_kwargs):  # noqa: ANN002, ANN003
    return 0
