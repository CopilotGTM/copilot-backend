"""Tests for meeting matcher integration email expansion."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import CRMParticipantType
from src.workers.deal_worker.services.deal_mapper_services.deal_meeting_matcher import (
    DealMeetingMatcher,
)


class _FakeScalarResult:
    def __init__(self, rows: list[object]):
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows


class _FakeExecuteResult:
    def __init__(self, rows: list[object]):
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows

    def scalars(self) -> _FakeScalarResult:
        return _FakeScalarResult(self._rows)


class _FakeSession:
    def __init__(self, rows_by_execute: list[list[object]]):
        self._rows_by_execute = rows_by_execute
        self.statements: list[str] = []
        self.execute_calls = 0

    async def execute(self, stmt):  # noqa: ANN001
        self.statements.append(str(stmt))
        idx = min(self.execute_calls, len(self._rows_by_execute) - 1)
        self.execute_calls += 1
        if not self._rows_by_execute:
            return _FakeExecuteResult([])
        return _FakeExecuteResult(self._rows_by_execute[idx])

    async def commit(self) -> None:
        return None


@pytest.mark.asyncio
async def test_meeting_matcher_collects_user_local_id_integration_calendar_email() -> None:
    matcher = DealMeetingMatcher(uuid4())

    participant = SimpleNamespace(
        participant_type=CRMParticipantType.USER,
        related_local_id=uuid4(),
        related_email="seller@crm.com",
    )
    deal = SimpleNamespace(
        deal_id=uuid4(),
        owner_id=None,
        owner_email="owner@crm.com",
        participants=[participant],
    )

    session = _FakeSession([[(participant.related_local_id, "calendar@google.com")]])
    team_emails = await matcher._collect_team_emails(session, deal)  # noqa: SLF001

    assert team_emails == {"calendar@google.com", "owner@crm.com", "seller@crm.com"}


@pytest.mark.asyncio
async def test_meeting_matcher_resolves_integrations_for_all_internal_participant_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matcher = DealMeetingMatcher(uuid4())
    owner_id = uuid4()
    participant_id = uuid4()
    collaborator_id = uuid4()
    captured_user_ids: set[object] = set()

    async def fake_get_active_integration_emails_for_users(
        _session,
        *,
        organisation_id,
        created_by_user_ids,
        integration_types,
    ):
        captured_user_ids.update(created_by_user_ids)
        return {"connected@calendar.com"}

    monkeypatch.setattr(
        "src.workers.deal_worker.services.deal_mapper_services.deal_meeting_matcher."
        "get_active_integration_emails_for_users",
        fake_get_active_integration_emails_for_users,
    )

    deal = SimpleNamespace(
        deal_id=uuid4(),
        owner_id=owner_id,
        owner_email="owner@crm.com",
        participants=[
            SimpleNamespace(
                participant_type=CRMParticipantType.USER,
                related_local_id=participant_id,
                related_email="participant@crm.com",
            ),
            SimpleNamespace(
                participant_type=CRMParticipantType.USER,
                related_local_id=collaborator_id,
                related_email="collaborator@crm.com",
            ),
        ],
    )

    team_emails = await matcher._collect_team_emails(_FakeSession([]), deal)  # noqa: SLF001

    assert captured_user_ids == {owner_id, participant_id, collaborator_id}
    assert team_emails == {
        "owner@crm.com",
        "participant@crm.com",
        "collaborator@crm.com",
        "connected@calendar.com",
    }


@pytest.mark.asyncio
async def test_meeting_matcher_attendee_matching_uses_integration_team_email() -> None:
    matcher = DealMeetingMatcher(uuid4())
    deal = SimpleNamespace(deal_id=uuid4())

    integration_team_email = "calendar@google.com"
    matched_event = SimpleNamespace(event_id=uuid4())
    session = _FakeSession([[matched_event]])
    captured: dict[str, object] = {}

    async def fake_create_associations(
        _session,
        _deal,
        events,
        match_score,
        linked_event_ids,
        customer_domains,
        team_emails,
    ):
        captured["match_score"] = match_score
        captured["events"] = list(events)
        captured["team_emails"] = set(team_emails)
        captured["linked_event_ids"] = set(linked_event_ids)
        captured["customer_domains"] = set(customer_domains)
        return len(events)

    matcher._create_associations = fake_create_associations  # type: ignore[method-assign]

    linked = await matcher._match_events_as_attendee(
        session=session,
        deal=deal,
        team_email_list=[integration_team_email],
        contact_email_list=["buyer@acme.com"],
        linked_event_ids=set(),
        customer_domains={"acme.com"},
        team_emails={integration_team_email},
    )

    # Return value is based on fake create call using the matched events list.
    assert linked == 1
    assert captured["match_score"] == 0.8
    assert captured["events"] == [matched_event]
    assert captured["team_emails"] == {integration_team_email}
    assert captured["customer_domains"] == {"acme.com"}

    # Ensure matcher executed a query for event matching.
    assert any("calendar_events" in stmt for stmt in session.statements)


@pytest.mark.asyncio
async def test_meeting_matcher_process_organisation_batches_integration_email_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    matcher = DealMeetingMatcher(org_id)
    owner_one_id = uuid4()
    participant_one_id = uuid4()
    owner_two_id = uuid4()
    participant_two_id = uuid4()
    deal_one_id = uuid4()
    deal_two_id = uuid4()
    grouped_calls: list[set[object]] = []
    legacy_calls: list[set[object]] = []
    captured_team_emails: dict[object, set[str]] = {}

    async def fake_get_active_integration_emails_grouped_by_user(
        session,
        *,
        organisation_id,
        created_by_user_ids,
        integration_types,
    ):
        grouped_calls.append(set(created_by_user_ids))
        return {
            owner_one_id: {"owner-one@calendar.com"},
            participant_one_id: {"participant-one@calendar.com"},
            owner_two_id: {"owner-two@calendar.com"},
            participant_two_id: {"participant-two@calendar.com"},
        }

    async def fake_get_active_integration_emails_for_users(
        _session,
        *,
        organisation_id,
        created_by_user_ids,
        integration_types,
    ):
        legacy_calls.append(set(created_by_user_ids))
        emails_by_user = {
            owner_one_id: {"owner-one@calendar.com"},
            participant_one_id: {"participant-one@calendar.com"},
            owner_two_id: {"owner-two@calendar.com"},
            participant_two_id: {"participant-two@calendar.com"},
        }
        return {
            email for user_id in created_by_user_ids for email in emails_by_user.get(user_id, set())
        }

    monkeypatch.setattr(
        "src.workers.deal_worker.services.deal_mapper_services.deal_meeting_matcher."
        "get_active_integration_emails_grouped_by_user",
        fake_get_active_integration_emails_grouped_by_user,
        raising=False,
    )
    monkeypatch.setattr(
        "src.workers.deal_worker.services.deal_mapper_services.deal_meeting_matcher."
        "get_active_integration_emails_for_users",
        fake_get_active_integration_emails_for_users,
    )

    deal_one = SimpleNamespace(
        deal_id=deal_one_id,
        owner_id=owner_one_id,
        owner_email="owner-one@crm.com",
        primary_contact_email=None,
        company_domain=None,
        participants=[
            SimpleNamespace(
                participant_type=CRMParticipantType.USER,
                related_local_id=participant_one_id,
                related_email="participant-one@crm.com",
            )
        ],
    )
    deal_two = SimpleNamespace(
        deal_id=deal_two_id,
        owner_id=owner_two_id,
        owner_email="owner-two@crm.com",
        primary_contact_email=None,
        company_domain=None,
        participants=[
            SimpleNamespace(
                participant_type=CRMParticipantType.USER,
                related_local_id=participant_two_id,
                related_email="participant-two@crm.com",
            )
        ],
    )
    setup_session = _FakeSession([[deal_one, deal_two]])
    process_session = _FakeSession([])
    sessions = iter([setup_session, process_session])

    async def fake_acquire_org_async_session(organisation_id):
        yield next(sessions)

    async def fake_handle_cancelled_associations(_session):
        return 0

    async def fake_find_and_link_meetings(
        session,
        deal,
        customer_domains,
        team_emails,
    ):
        captured_team_emails[deal.deal_id] = set(team_emails)
        return 0

    async def fake_dispatch() -> None:
        return None

    monkeypatch.setattr(
        "src.workers.deal_worker.services.deal_mapper_services.deal_meeting_matcher."
        "acquire_org_async_session",
        fake_acquire_org_async_session,
    )
    matcher._handle_cancelled_associations = fake_handle_cancelled_associations  # type: ignore[method-assign]
    matcher._find_and_link_meetings = fake_find_and_link_meetings  # type: ignore[method-assign]
    matcher._dispatch_pending_recordings = fake_dispatch  # type: ignore[method-assign]
    matcher._dispatch_pending_research = fake_dispatch  # type: ignore[method-assign]

    stats = await matcher.process_organisation()

    assert stats == {"deals_processed": 2, "meetings_linked": 0, "cancelled": 0}
    assert grouped_calls == [{owner_one_id, participant_one_id, owner_two_id, participant_two_id}]
    assert legacy_calls == []
    assert captured_team_emails[deal_one_id] == {
        "owner-one@crm.com",
        "participant-one@crm.com",
        "owner-one@calendar.com",
        "participant-one@calendar.com",
    }
    assert captured_team_emails[deal_two_id] == {
        "owner-two@crm.com",
        "participant-two@crm.com",
        "owner-two@calendar.com",
        "participant-two@calendar.com",
    }
