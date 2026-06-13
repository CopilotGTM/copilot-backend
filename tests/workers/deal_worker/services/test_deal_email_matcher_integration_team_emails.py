"""Tests for deal email matching with user-owned integration email expansion."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import CRMParticipantType
from src.domain.models.organisation_tables.sync.crm.stakeholders import ApprovalStatus
from src.domain.models.organisation_tables.sync.email_models import DealEmailDirection
from src.workers.deal_worker.services.deal_mapper_services.deal_email_matcher import (
    DealEmailMatcher,
    DealStakeholderInfo,
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


@pytest.mark.asyncio
async def test_email_matcher_owner_id_collects_integration_email_for_matching() -> None:
    matcher = DealEmailMatcher(uuid4())
    owner_id = uuid4()
    deal_id = uuid4()

    deal = SimpleNamespace(
        deal_id=deal_id,
        owner_id=owner_id,
        owner_email="crm-owner@acme.com",
        participants=[
            SimpleNamespace(
                participant_type=CRMParticipantType.CONTACT,
                related_email="customer@acme.com",
                is_deleted=False,
                approval_status=ApprovalStatus.APPROVED.value,
            )
        ],
    )

    info = DealStakeholderInfo(deal_id=deal_id)
    session = _FakeSession(
        [
            [(owner_id, "owner@Gmail.com"), (owner_id, " "), (owner_id, "null")],
        ]
    )

    await matcher._collect_team_emails(session, deal, info)  # noqa: SLF001
    await matcher._collect_stakeholder_emails(session, deal, info)  # noqa: SLF001

    assert info.team_emails == {"crm-owner@acme.com", "owner@gmail.com"}
    assert info.contact_emails == {"customer@acme.com"}

    matcher._team_email_to_deals["owner@gmail.com"] = {deal_id}
    matcher._contact_email_to_deals["customer@acme.com"] = {deal_id}
    matcher._deal_info[deal_id] = info

    email = SimpleNamespace(
        from_email="owner@gmail.com",
        to_emails=["customer@acme.com"],
        cc_emails=[],
        internet_message_id="123",
    )
    matches = matcher._match_email_to_deals(email)  # noqa: SLF001

    assert len(matches) == 1
    assert matches[0].deal_id == deal_id
    assert matches[0].direction == DealEmailDirection.OUTBOUND
    assert set(matches[0].matched_emails) == {"owner@gmail.com", "customer@acme.com"}
    assert matches[0].score == 1.0


@pytest.mark.asyncio
async def test_email_matcher_user_participant_local_id_collects_integration_email_for_inbound_match() -> (
    None
):
    matcher = DealEmailMatcher(uuid4())
    participant_id = uuid4()
    deal_id = uuid4()

    participant = SimpleNamespace(
        participant_type=CRMParticipantType.USER,
        related_local_id=participant_id,
        related_email="participant@acme.com",
    )
    contact = SimpleNamespace(
        participant_type=CRMParticipantType.CONTACT,
        related_email="customer@acme.com",
        is_deleted=False,
        approval_status=ApprovalStatus.APPROVED.value,
    )
    deal = SimpleNamespace(
        deal_id=deal_id,
        owner_id=None,
        owner_email=None,
        participants=[participant, contact],
    )

    info = DealStakeholderInfo(deal_id=deal_id)
    session = _FakeSession(
        [
            [(participant_id, "calendar@gmail.com")],
        ]
    )

    await matcher._collect_team_emails(session, deal, info)  # noqa: SLF001
    await matcher._collect_stakeholder_emails(session, deal, info)  # noqa: SLF001

    assert info.team_emails == {"calendar@gmail.com", "participant@acme.com"}

    matcher._team_email_to_deals["calendar@gmail.com"] = {deal_id}
    matcher._contact_email_to_deals["customer@acme.com"] = {deal_id}
    matcher._deal_info[deal_id] = info

    email = SimpleNamespace(
        from_email="customer@acme.com",
        to_emails=["calendar@gmail.com"],
        cc_emails=[],
        internet_message_id="456",
    )
    matches = matcher._match_email_to_deals(email)  # noqa: SLF001

    assert len(matches) == 1
    assert matches[0].deal_id == deal_id
    assert matches[0].direction == DealEmailDirection.INBOUND
    assert matches[0].score == 1.0


@pytest.mark.asyncio
async def test_email_matcher_resolves_integrations_for_all_internal_participant_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matcher = DealEmailMatcher(uuid4())
    owner_id = uuid4()
    participant_id = uuid4()
    collaborator_id = uuid4()
    captured_user_ids: set[object] = set()

    async def fake_get_active_integration_emails_for_users(
        session,
        *,
        organisation_id,
        created_by_user_ids,
        integration_types,
    ):
        captured_user_ids.update(created_by_user_ids)
        return {"connected@google.com"}

    monkeypatch.setattr(
        "src.workers.deal_worker.services.deal_mapper_services.deal_email_matcher."
        "get_active_integration_emails_for_users",
        fake_get_active_integration_emails_for_users,
    )

    deal = SimpleNamespace(
        deal_id=uuid4(),
        owner_id=owner_id,
        owner_email="owner@acme.com",
        participants=[
            SimpleNamespace(
                participant_type=CRMParticipantType.USER,
                related_local_id=participant_id,
                related_email="participant@acme.com",
            ),
            SimpleNamespace(
                participant_type=CRMParticipantType.USER,
                related_local_id=collaborator_id,
                related_email="collaborator@acme.com",
            ),
        ],
    )
    info = DealStakeholderInfo(deal_id=deal.deal_id)

    await matcher._collect_team_emails(_FakeSession([]), deal, info)  # noqa: SLF001

    assert captured_user_ids == {owner_id, participant_id, collaborator_id}
    assert info.team_emails == {
        "owner@acme.com",
        "participant@acme.com",
        "collaborator@acme.com",
        "connected@google.com",
    }


@pytest.mark.asyncio
async def test_email_matcher_build_maps_batches_integration_email_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    matcher = DealEmailMatcher(org_id)
    owner_one_id = uuid4()
    participant_one_id = uuid4()
    owner_two_id = uuid4()
    participant_two_id = uuid4()
    deal_one_id = uuid4()
    deal_two_id = uuid4()
    grouped_calls: list[set[object]] = []
    legacy_calls: list[set[object]] = []

    async def fake_get_active_integration_emails_grouped_by_user(
        session,
        *,
        organisation_id,
        created_by_user_ids,
        integration_types,
    ):
        grouped_calls.append(set(created_by_user_ids))
        return {
            owner_one_id: {"owner-one@gmail.com"},
            participant_one_id: {"participant-one@gmail.com"},
            owner_two_id: {"owner-two@outlook.com"},
            participant_two_id: {"participant-two@outlook.com"},
        }

    async def fake_get_active_integration_emails_for_users(
        session,
        *,
        organisation_id,
        created_by_user_ids,
        integration_types,
    ):
        legacy_calls.append(set(created_by_user_ids))
        emails_by_user = {
            owner_one_id: {"owner-one@gmail.com"},
            participant_one_id: {"participant-one@gmail.com"},
            owner_two_id: {"owner-two@outlook.com"},
            participant_two_id: {"participant-two@outlook.com"},
        }
        return {
            email for user_id in created_by_user_ids for email in emails_by_user.get(user_id, set())
        }

    monkeypatch.setattr(
        "src.workers.deal_worker.services.deal_mapper_services.deal_email_matcher."
        "get_active_integration_emails_grouped_by_user",
        fake_get_active_integration_emails_grouped_by_user,
        raising=False,
    )
    monkeypatch.setattr(
        "src.workers.deal_worker.services.deal_mapper_services.deal_email_matcher."
        "get_active_integration_emails_for_users",
        fake_get_active_integration_emails_for_users,
    )

    deal_one = SimpleNamespace(
        deal_id=deal_one_id,
        integration_id=None,
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
        integration_id=None,
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
    session = _FakeSession([[deal_one, deal_two], [], []])

    await matcher._build_stakeholder_maps(session)  # noqa: SLF001

    assert grouped_calls == [{owner_one_id, participant_one_id, owner_two_id, participant_two_id}]
    assert legacy_calls == []
    assert matcher._deal_info[deal_one_id].team_emails == {  # noqa: SLF001
        "owner-one@crm.com",
        "participant-one@crm.com",
        "owner-one@gmail.com",
        "participant-one@gmail.com",
    }
    assert matcher._deal_info[deal_two_id].team_emails == {  # noqa: SLF001
        "owner-two@crm.com",
        "participant-two@crm.com",
        "owner-two@outlook.com",
        "participant-two@outlook.com",
    }
