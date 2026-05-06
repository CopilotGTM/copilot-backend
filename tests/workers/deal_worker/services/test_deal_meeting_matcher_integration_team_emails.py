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

    session = _FakeSession([["calendar@google.com"]])
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
