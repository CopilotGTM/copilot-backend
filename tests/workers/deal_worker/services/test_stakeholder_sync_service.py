"""Tests for participant-backed stakeholder sync behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import src.workers.deal_worker.services.deal_mapper_services.stakeholder_sync_service as stakeholder_sync_module
from sqlalchemy.dialects import postgresql
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import ApprovalStatus
from src.workers.deal_worker.services.deal_mapper_services.stakeholder_sync_service import (
    OrgStakeholderSyncResult,
    StakeholderSyncResult,
    StakeholderSyncService,
)


@pytest.mark.asyncio
async def test_sync_deal_collapses_duplicate_contact_participants_by_normalized_email() -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    service = StakeholderSyncService(organisation_id)
    service._internal_domains = set()
    service._enrich_from_contact = AsyncMock(return_value=False)  # type: ignore[method-assign]

    first_participant = CRMDealParticipant(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        deal_id=deal_id,
        participant_type=CRMParticipantType.CONTACT.value,
        related_email="Info@Salesforce.com",
        related_name="Salesforce Info",
        approval_status=ApprovalStatus.APPROVED.value,
    )
    duplicate_participant = CRMDealParticipant(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        deal_id=deal_id,
        participant_type=CRMParticipantType.CONTACT,
        related_email=" info@salesforce.com ",
        related_name="Salesforce Billing",
        approval_status=ApprovalStatus.APPROVED.value,
    )
    deal = SimpleNamespace(
        deal_id=deal_id,
        integration_id=uuid4(),
        participants=[first_participant, duplicate_participant],
    )
    service._load_deal = AsyncMock(return_value=deal)  # type: ignore[method-assign]
    session = SimpleNamespace(add=MagicMock())

    result = await service._sync_deal_internal(session, deal_id)  # noqa: SLF001

    assert result.created == 0
    assert result.updated == 1
    assert result.skipped == 1
    assert first_participant.related_email == "info@salesforce.com"
    session.add.assert_called_once_with(first_participant)
    assert service._pending_research == []


@pytest.mark.asyncio
async def test_sync_deal_skips_research_when_linked_contact_is_researched() -> None:
    """Sync dispatch gating should use contact research state, not participant snapshots."""
    organisation_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()
    service = StakeholderSyncService(organisation_id)
    service._internal_domains = set()
    service._enrich_from_contact = AsyncMock(return_value=False)  # type: ignore[method-assign]
    participant = CRMDealParticipant(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        deal_id=deal_id,
        participant_type=CRMParticipantType.CONTACT.value,
        related_local_id=contact_id,
        related_email="buyer@example.com",
        approval_status=ApprovalStatus.APPROVED.value,
    )
    deal = SimpleNamespace(
        deal_id=deal_id,
        integration_id=uuid4(),
        participants=[participant],
    )
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=participant.integration_id,
        contact_id=contact_id,
        email="buyer@example.com",
        contact_research="Existing contact research",
    )
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = contact
    session = SimpleNamespace(add=MagicMock(), execute=AsyncMock(return_value=execute_result))
    service._load_deal = AsyncMock(return_value=deal)  # type: ignore[method-assign]

    result = await service._sync_deal_internal(session, deal_id)  # noqa: SLF001

    assert result.created == 0
    assert service._pending_research == []


@pytest.mark.asyncio
async def test_sync_deal_rejects_internal_domain_contact_participants() -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    service = StakeholderSyncService(organisation_id)
    service._internal_domains = {"copilotgtm.com"}
    service._enrich_from_contact = AsyncMock(return_value=False)  # type: ignore[method-assign]
    participant = CRMDealParticipant(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        deal_id=deal_id,
        participant_type=CRMParticipantType.CONTACT,
        related_email="seller@copilotgtm.com",
        related_name="Internal Seller",
        approval_status=ApprovalStatus.APPROVED.value,
    )
    deal = SimpleNamespace(deal_id=deal_id, integration_id=uuid4(), participants=[participant])
    service._load_deal = AsyncMock(return_value=deal)  # type: ignore[method-assign]
    session = SimpleNamespace(add=MagicMock())

    result = await service._sync_deal_internal(session, deal_id)  # noqa: SLF001

    assert result.rejected == 1
    assert result.created == 0
    assert participant.approval_status == ApprovalStatus.REJECTED.value
    session.add.assert_called_once_with(participant)
    assert service._pending_research == []


@pytest.mark.asyncio
async def test_enrich_from_contact_links_contact_without_profile_snapshot() -> None:
    """Stakeholder sync links the contact but leaves person profile on CRMContact."""
    organisation_id = uuid4()
    service = StakeholderSyncService(organisation_id)
    contact_id = uuid4()
    contact = SimpleNamespace(
        contact_id=contact_id,
        full_name="CRM Name",
        job_title="VP Sales",
        phone="+1 800-555-0123",
        mobile_phone=None,
        properties={"department": "Revenue", "external_links": [{"type": "linkedin"}]},
    )
    scalars_result = MagicMock()
    scalars_result.first.return_value = contact
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session = SimpleNamespace(execute=AsyncMock(return_value=execute_result))
    participant = CRMDealParticipant(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        deal_id=uuid4(),
        participant_type=CRMParticipantType.CONTACT,
        related_email="person@example.com",
        approval_status=ApprovalStatus.APPROVED.value,
    )

    changed = await service._enrich_from_contact(  # noqa: SLF001
        session=session,
        stakeholder=participant,
        email="person@example.com",
    )

    assert changed is True
    assert participant.related_local_id == contact_id
    assert participant.related_name is None
    assert not hasattr(participant, "job_title")
    assert not hasattr(participant, "phone_number")
    assert not hasattr(participant, "linkedin_url")
    assert not hasattr(participant, "department")
    assert not hasattr(participant, "external_links")


@pytest.mark.asyncio
async def test_load_deal_locks_deal_row_to_serialize_concurrent_syncs() -> None:
    service = StakeholderSyncService(uuid4())

    execute_result = MagicMock()
    execute_result.scalars.return_value.first.return_value = None
    session = SimpleNamespace(execute=AsyncMock(return_value=execute_result))

    await service._load_deal(session, uuid4())  # noqa: SLF001

    stmt = session.execute.await_args.args[0]
    compiled_query = str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "FOR UPDATE" in compiled_query


class _FakeScalarResult:
    def __init__(self, rows: list[CRMDealParticipant]):
        self._rows = rows

    def all(self) -> list[CRMDealParticipant]:
        return self._rows


class _FakeExecuteResult:
    def __init__(self, rows: list[CRMDealParticipant]):
        self._rows = rows

    def scalars(self) -> _FakeScalarResult:
        return _FakeScalarResult(self._rows)


class _FakeSession:
    def __init__(self, rows: list[CRMDealParticipant]):
        self._rows = rows
        self.add = MagicMock()

    async def execute(self, _stmt):  # noqa: ANN001
        return _FakeExecuteResult(self._rows)


class TestRejectInternalStakeholders:
    @pytest.mark.asyncio
    async def test_rejects_internal_emails(self) -> None:
        organisation_id = uuid4()
        deal_id = uuid4()
        service = StakeholderSyncService(organisation_id)
        service._internal_domains = {"copilotgtm.com"}
        internal = CRMDealParticipant(
            organisation_id=organisation_id,
            integration_id=uuid4(),
            deal_id=deal_id,
            participant_type=CRMParticipantType.CONTACT,
            related_email="saksham@copilotgtm.com",
            approval_status=ApprovalStatus.APPROVED.value,
        )
        external = CRMDealParticipant(
            organisation_id=organisation_id,
            integration_id=uuid4(),
            deal_id=deal_id,
            participant_type=CRMParticipantType.CONTACT,
            related_email="buyer@acme.com",
            approval_status=ApprovalStatus.APPROVED.value,
        )
        session = _FakeSession([internal, external])

        count = await service._reject_internal_stakeholders(session, deal_id)  # noqa: SLF001

        assert count == 1
        assert internal.approval_status == ApprovalStatus.REJECTED.value
        assert external.approval_status == ApprovalStatus.APPROVED.value
        session.add.assert_called_once_with(internal)

    @pytest.mark.asyncio
    async def test_no_internal_domains_returns_zero(self) -> None:
        service = StakeholderSyncService(uuid4())
        service._internal_domains = set()
        participant = CRMDealParticipant(
            organisation_id=uuid4(),
            integration_id=uuid4(),
            deal_id=uuid4(),
            participant_type=CRMParticipantType.CONTACT,
            related_email="anyone@anywhere.com",
            approval_status=ApprovalStatus.APPROVED.value,
        )
        session = _FakeSession([participant])

        count = await service._reject_internal_stakeholders(session, uuid4())  # noqa: SLF001

        assert count == 0
        session.add.assert_not_called()


class _FakeDealIdQueryResult:
    def __init__(self, rows: list[tuple[object, ...]]):
        self._rows = rows

    def all(self) -> list[tuple[object, ...]]:
        return self._rows


class _FakeSyncSession:
    def __init__(self, rows: list[tuple[object, ...]] | None = None):
        self._rows = rows or []
        self.statements = []
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def execute(self, stmt):  # noqa: ANN001
        self.statements.append(stmt)
        return _FakeDealIdQueryResult(self._rows)


def _session_generator_factory(
    sessions: list[_FakeSyncSession],
) -> callable:
    queue = iter(sessions)

    def _factory(_organisation_id) -> AsyncIterator[_FakeSyncSession]:  # noqa: ANN001
        async def _generator() -> AsyncIterator[_FakeSyncSession]:
            yield next(queue)

        return _generator()

    return _factory


@pytest.mark.asyncio
async def test_sync_for_organisation_does_not_use_reserved_created_log_field(monkeypatch) -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    service = StakeholderSyncService(organisation_id)

    initial_session = _FakeSyncSession(rows=[(deal_id,)])
    deal_session = _FakeSyncSession()
    monkeypatch.setattr(
        stakeholder_sync_module,
        "acquire_org_async_session",
        _session_generator_factory([initial_session, deal_session]),
    )

    service._sync_deal_internal = AsyncMock(  # type: ignore[method-assign]
        return_value=StakeholderSyncResult(
            deal_id=deal_id,
            created=2,
            updated=1,
            deactivated=1,
            rejected=1,
        )
    )
    service._dispatch_pending_research = AsyncMock()  # type: ignore[method-assign]

    result = await service.sync_for_organisation()

    assert isinstance(result, OrgStakeholderSyncResult)
    assert result.successful_deals == 1
    assert result.stakeholders_created == 2
    deal_session.commit.assert_awaited_once()
    deal_session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_for_organisation_processes_multiple_deals_in_one_batch_session(
    monkeypatch,
) -> None:
    organisation_id = uuid4()
    first_deal_id = uuid4()
    second_deal_id = uuid4()
    service = StakeholderSyncService(organisation_id)

    initial_session = _FakeSyncSession(rows=[(first_deal_id,), (second_deal_id,)])
    batch_session = _FakeSyncSession()
    monkeypatch.setattr(
        stakeholder_sync_module,
        "acquire_org_async_session",
        _session_generator_factory([initial_session, batch_session]),
    )

    service._sync_deal_internal = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            StakeholderSyncResult(deal_id=first_deal_id, created=1),
            StakeholderSyncResult(deal_id=second_deal_id, updated=1),
        ]
    )
    service._dispatch_pending_research = AsyncMock()  # type: ignore[method-assign]

    result = await service.sync_for_organisation()

    assert result.successful_deals == 2
    assert result.stakeholders_created == 1
    assert result.stakeholders_updated == 1
    assert service._sync_deal_internal.await_count == 2  # type: ignore[attr-defined]
    assert service._sync_deal_internal.await_args_list[0].args == (  # type: ignore[attr-defined]
        batch_session,
        first_deal_id,
    )
    assert service._sync_deal_internal.await_args_list[1].args == (  # type: ignore[attr-defined]
        batch_session,
        second_deal_id,
    )
    batch_session.commit.assert_awaited_once()
    batch_session.rollback.assert_not_awaited()
    service._dispatch_pending_research.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_sync_for_organisation_selects_only_open_deals(monkeypatch) -> None:
    organisation_id = uuid4()
    service = StakeholderSyncService(organisation_id)

    initial_session = _FakeSyncSession(rows=[])
    monkeypatch.setattr(
        stakeholder_sync_module,
        "acquire_org_async_session",
        _session_generator_factory([initial_session]),
    )

    await service.sync_for_organisation()

    compiled_query = str(
        initial_session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "crm_deals.deal_status = 'open'" in compiled_query
    assert "ORDER BY crm_deals.deal_id" in compiled_query


@pytest.mark.asyncio
async def test_sync_for_deal_does_not_use_reserved_created_log_field(monkeypatch) -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    service = StakeholderSyncService(organisation_id)

    session = _FakeSyncSession()
    monkeypatch.setattr(
        stakeholder_sync_module,
        "acquire_org_async_session",
        _session_generator_factory([session]),
    )

    service._sync_deal_internal = AsyncMock(  # type: ignore[method-assign]
        return_value=StakeholderSyncResult(
            deal_id=deal_id,
            created=3,
            updated=1,
            deactivated=1,
            skipped=2,
        )
    )
    service._dispatch_pending_research = AsyncMock()  # type: ignore[method-assign]

    result = await service.sync_for_deal(deal_id)

    assert result.created == 3
    assert result.updated == 1
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()
