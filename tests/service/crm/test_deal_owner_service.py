"""Regression tests for deal owner snapshot and participant projection boundaries."""

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
from src.service.crm.deals.owner_service import DealOwnerService
from src.service.crm.deals.write_service import DealWriteService
from src.service.crm.participants.contracts import ParticipantWriteResult


class _FakeScalarResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalarResult:
        """Return self to mimic SQLAlchemy's scalar result chain."""
        return self

    def all(self) -> list[object]:
        """Return the fake rows supplied by each test."""
        return self._rows


class _FakeOwnerSession:
    def __init__(self, rows: list[object] | None = None) -> None:
        self.rows = rows or []
        self.added: list[object] = []
        self.statements: list[object] = []
        self.flush_count = 0

    async def execute(self, statement):
        """Capture statements while returning the configured fake rows."""
        self.statements.append(statement)
        return _FakeScalarResult(self.rows)

    def add(self, obj: object) -> None:
        """Capture mutated participants for assertions without a real session."""
        self.added.append(obj)

    async def flush(self) -> None:
        """Count flush attempts for no-op assignment assertions."""
        self.flush_count += 1


def _owner_participant(
    *,
    deal_id,
    integration_id,
    related_local_id,
    role: str = "owner",
    is_deleted: bool = False,
) -> CRMDealParticipant:
    return CRMDealParticipant(
        participant_id=uuid4(),
        organisation_id=uuid4(),
        integration_id=integration_id,
        deal_id=deal_id,
        participant_type=CRMParticipantType.USER,
        relationship_role=role,
        related_local_id=related_local_id,
        related_name="Owner",
        related_email="owner@example.com",
        is_deleted=is_deleted,
    )


@pytest.mark.asyncio
async def test_sync_owner_participant_creates_projection_and_clears_old_owner_rows() -> None:
    """Owner assignment owns the deal snapshot and leaves one active owner projection."""
    organisation_id = uuid4()
    actor_user_id = uuid4()
    deal_id = uuid4()
    integration_id = uuid4()
    old_owner_id = uuid4()
    new_owner_id = uuid4()
    old_owner = _owner_participant(
        deal_id=deal_id,
        integration_id=integration_id,
        related_local_id=old_owner_id,
        role=" Owner ",
    )
    collaborator = _owner_participant(
        deal_id=deal_id,
        integration_id=integration_id,
        related_local_id=uuid4(),
        role="collaborator",
    )
    session = _FakeOwnerSession([old_owner, collaborator])
    service = DealOwnerService(
        session=session,  # type: ignore[arg-type]
        organisation_id=organisation_id,
        actor_user_id=actor_user_id,
        master_session=SimpleNamespace(),  # type: ignore[arg-type]
    )
    service._participant_writer.add_participants = AsyncMock(  # type: ignore[attr-defined]
        return_value=ParticipantWriteResult(
            created=[],
            skipped=[],
            contact_participant_changed=False,
            integration_id=integration_id,
        )
    )
    deal = SimpleNamespace(deal_id=deal_id, integration_id=integration_id)
    crm_user = SimpleNamespace(
        user_id=new_owner_id,
        full_name="New Owner",
        email="new.owner@example.com",
    )

    await service.sync_owner_participant(deal=deal, crm_user=crm_user)  # type: ignore[arg-type]

    service._participant_writer.add_participants.assert_awaited_once_with(  # type: ignore[attr-defined]
        deal_id,
        [
            {
                "participant_type": CRMParticipantType.USER.value,
                "related_id": new_owner_id,
                "relationship_role": "owner",
            }
        ],
    )
    assert old_owner.is_deleted is True
    assert old_owner.source_modified_at is not None
    assert collaborator.is_deleted is False


@pytest.mark.asyncio
async def test_clear_owner_participants_soft_deletes_only_owner_role_rows() -> None:
    """Clearing a deal owner must not remove normal user collaborators."""
    deal_id = uuid4()
    integration_id = uuid4()
    owner = _owner_participant(
        deal_id=deal_id,
        integration_id=integration_id,
        related_local_id=uuid4(),
        role="OWNER",
    )
    collaborator = _owner_participant(
        deal_id=deal_id,
        integration_id=integration_id,
        related_local_id=uuid4(),
        role="split_owner",
    )
    session = _FakeOwnerSession([owner, collaborator])
    service = DealOwnerService(
        session=session,  # type: ignore[arg-type]
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
        master_session=SimpleNamespace(),  # type: ignore[arg-type]
    )

    await service.clear_owner_participants(
        deal=SimpleNamespace(deal_id=deal_id, integration_id=integration_id)
    )  # type: ignore[arg-type]

    assert owner.is_deleted is True
    assert owner.source_modified_at is not None
    assert collaborator.is_deleted is False


def test_apply_owner_same_owner_reports_no_change() -> None:
    """Same-owner assignment is a true no-op for caller-side audit decisions."""
    owner_id = uuid4()
    deal = SimpleNamespace(
        owner_id=owner_id,
        owner_name="Ada Owner",
        owner_email="ada@example.com",
    )
    crm_user = SimpleNamespace(user_id=owner_id, full_name="Ada Owner", email="ada@example.com")

    result = DealOwnerService().apply_owner(deal=deal, crm_user=crm_user)  # type: ignore[arg-type]

    assert result.changed is False
    assert result.changed_fields == {}


@pytest.mark.asyncio
async def test_assign_owner_updates_snapshot_syncs_projection_and_records_one_event() -> None:
    """Public owner assignment orchestrates snapshot, projection, and one mutation event."""
    session = _FakeOwnerSession()
    service = DealWriteService(
        org_session=session,  # type: ignore[arg-type]
        master_session=SimpleNamespace(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    owner_user_id = uuid4()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        integration_id=uuid4(),
        owner_id=None,
        owner_name=None,
        owner_email=None,
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    crm_user = SimpleNamespace(
        user_id=owner_user_id,
        full_name="Owner User",
        email="owner@example.com",
    )
    service._load_internal_deal = AsyncMock(return_value=deal)  # type: ignore[method-assign]
    service._owners.sync_owner_participant = AsyncMock()  # type: ignore[method-assign]
    service._record_mutation_event = AsyncMock()  # type: ignore[method-assign]

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "src.service.crm.deals.write_service.InternalCRMUserService.ensure_crm_user",
            AsyncMock(return_value=crm_user),
        )
        result = await service.assign_owner(deal.deal_id, owner_user_id)

    assert result is deal
    assert deal.owner_id == owner_user_id
    assert deal.owner_name == "Owner User"
    assert deal.owner_email == "owner@example.com"
    service._owners.sync_owner_participant.assert_awaited_once_with(  # type: ignore[attr-defined]
        deal=deal,
        crm_user=crm_user,
    )
    service._record_mutation_event.assert_awaited_once()  # type: ignore[attr-defined]
    event_kwargs = service._record_mutation_event.await_args.kwargs  # type: ignore[attr-defined]
    assert event_kwargs["event_type"] == "crm.deal.owner_changed"


@pytest.mark.asyncio
async def test_assign_owner_same_owner_is_noop_without_timestamp_or_event() -> None:
    """No-op owner assignment returns before timestamps, participant writes, or audit rows."""
    unchanged_at = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
    owner_user_id = uuid4()
    session = _FakeOwnerSession()
    service = DealWriteService(
        org_session=session,  # type: ignore[arg-type]
        master_session=SimpleNamespace(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    deal = SimpleNamespace(
        deal_id=uuid4(),
        integration_id=uuid4(),
        owner_id=owner_user_id,
        owner_name="Owner User",
        owner_email="owner@example.com",
        source_modified_at=unchanged_at,
        synced_at=unchanged_at,
        last_seen_at=unchanged_at,
    )
    crm_user = SimpleNamespace(
        user_id=owner_user_id,
        full_name="Owner User",
        email="owner@example.com",
    )
    service._load_internal_deal = AsyncMock(return_value=deal)  # type: ignore[method-assign]
    service._owners.sync_owner_participant = AsyncMock()  # type: ignore[method-assign]
    service._owners.clear_owner_participants = AsyncMock()  # type: ignore[method-assign]
    service._record_mutation_event = AsyncMock()  # type: ignore[method-assign]

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "src.service.crm.deals.write_service.InternalCRMUserService.ensure_crm_user",
            AsyncMock(return_value=crm_user),
        )
        result = await service.assign_owner(deal.deal_id, owner_user_id)

    assert result is deal
    assert deal.source_modified_at == unchanged_at
    assert deal.synced_at == unchanged_at
    assert deal.last_seen_at == unchanged_at
    assert session.flush_count == 0
    service._owners.sync_owner_participant.assert_not_awaited()  # type: ignore[attr-defined]
    service._owners.clear_owner_participants.assert_not_awaited()  # type: ignore[attr-defined]
    service._record_mutation_event.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_assign_owner_none_clears_snapshot_and_owner_projection() -> None:
    """Clearing owner uses the owner service cleanup path instead of raw participant writes."""
    owner_user_id = uuid4()
    session = _FakeOwnerSession()
    service = DealWriteService(
        org_session=session,  # type: ignore[arg-type]
        master_session=SimpleNamespace(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    deal = SimpleNamespace(
        deal_id=uuid4(),
        integration_id=uuid4(),
        owner_id=owner_user_id,
        owner_name="Owner User",
        owner_email="owner@example.com",
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    service._load_internal_deal = AsyncMock(return_value=deal)  # type: ignore[method-assign]
    service._owners.clear_owner_participants = AsyncMock()  # type: ignore[method-assign]
    service._record_mutation_event = AsyncMock()  # type: ignore[method-assign]

    result = await service.assign_owner(deal.deal_id, None)

    assert result is deal
    assert deal.owner_id is None
    assert deal.owner_name is None
    assert deal.owner_email is None
    service._owners.clear_owner_participants.assert_awaited_once_with(  # type: ignore[attr-defined]
        deal=deal
    )
    service._record_mutation_event.assert_awaited_once()  # type: ignore[attr-defined]
