"""Tests for stakeholder sync manual override protections."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import src.workers.deal_worker.services.deal_mapper_services.stakeholder_sync_service as stakeholder_sync_module
from sqlalchemy.dialects import postgresql
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    StakeholderSource,
)
from src.workers.deal_worker.services.deal_mapper_services.stakeholder_sync_service import (
    OrgStakeholderSyncResult,
    StakeholderSyncResult,
    StakeholderSyncService,
)


def test_get_manual_overrides_reads_stakeholder_data() -> None:
    service = StakeholderSyncService(uuid4())
    stakeholder = SimpleNamespace(stakeholder_data={"manual_overrides": ["name", "job_title"]})

    overrides = service._get_manual_overrides(stakeholder)  # noqa: SLF001 - internal behavior test

    assert overrides == {"name", "job_title"}


@pytest.mark.asyncio
async def test_ensure_stakeholder_keeps_rejected_status_and_manual_fields() -> None:
    service = StakeholderSyncService(uuid4())
    service._enrich_from_contact = AsyncMock(return_value=False)  # type: ignore[method-assign]

    session = SimpleNamespace(add=MagicMock())
    participant_id = uuid4()
    participant = SimpleNamespace(
        related_email="person@example.com",
        related_name="CRM Name",
        participant_id=participant_id,
    )
    deal = SimpleNamespace(deal_id=uuid4(), integration_id=uuid4())
    existing = SimpleNamespace(
        approval_status=ApprovalStatus.REJECTED,
        source=StakeholderSource.CRM,
        source_reference_id=None,
        is_deleted=True,
        updated_at=None,
        name="Manual Name",
        stakeholder_data={"manual_overrides": ["name", "job_title"]},
    )

    was_created = await service._ensure_stakeholder(  # noqa: SLF001 - internal behavior test
        session=session,
        deal=deal,
        participant=participant,
        existing=existing,
    )

    assert was_created is False
    assert existing.approval_status == ApprovalStatus.REJECTED
    assert existing.is_deleted is True
    assert existing.name == "Manual Name"
    assert existing.source_reference_id == participant_id
    session.add.assert_called_once_with(existing)
    service._enrich_from_contact.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_ensure_stakeholder_does_not_convert_manual_source() -> None:
    service = StakeholderSyncService(uuid4())
    service._enrich_from_contact = AsyncMock(return_value=False)  # type: ignore[method-assign]

    session = SimpleNamespace(add=MagicMock())
    participant = SimpleNamespace(
        related_email="person@example.com",
        related_name="CRM Name",
        participant_id=uuid4(),
    )
    deal = SimpleNamespace(deal_id=uuid4(), integration_id=uuid4())
    existing = SimpleNamespace(
        approval_status=ApprovalStatus.APPROVED,
        source=StakeholderSource.MANUAL,
        source_reference_id=None,
        is_deleted=True,
        updated_at=None,
        name=None,
        stakeholder_data={},
    )

    was_created = await service._ensure_stakeholder(  # noqa: SLF001 - internal behavior test
        session=session,
        deal=deal,
        participant=participant,
        existing=existing,
    )

    assert was_created is False
    assert existing.source == StakeholderSource.MANUAL
    assert existing.approval_status == ApprovalStatus.APPROVED
    assert existing.is_deleted is False


@pytest.mark.asyncio
async def test_ensure_stakeholder_skips_write_when_no_values_change() -> None:
    service = StakeholderSyncService(uuid4())
    service._enrich_from_contact = AsyncMock(return_value=False)  # type: ignore[method-assign]

    session = SimpleNamespace(add=MagicMock())
    participant_id = uuid4()
    participant = SimpleNamespace(
        related_email="person@example.com",
        related_name="Same Name",
        participant_id=participant_id,
    )
    deal = SimpleNamespace(deal_id=uuid4(), integration_id=uuid4())
    original_updated_at = object()
    existing = SimpleNamespace(
        approval_status=ApprovalStatus.APPROVED,
        source=StakeholderSource.CRM,
        source_reference_id=participant_id,
        is_deleted=False,
        updated_at=original_updated_at,
        name="Same Name",
        stakeholder_data={},
    )

    was_created = await service._ensure_stakeholder(  # noqa: SLF001 - internal behavior test
        session=session,
        deal=deal,
        participant=participant,
        existing=existing,
    )

    assert was_created is False
    assert existing.updated_at is original_updated_at
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_enrich_from_contact_respects_manual_override_fields() -> None:
    service = StakeholderSyncService(uuid4())
    contact = SimpleNamespace(
        contact_id=uuid4(), full_name="CRM Name", job_title="CRM Title", phone="+1 800-CRM"
    )

    scalars_result = MagicMock()
    scalars_result.first.return_value = contact
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session = SimpleNamespace(execute=AsyncMock(return_value=execute_result))

    stakeholder = SimpleNamespace(
        contact_id=None, name="Manual Name", job_title="Manual Title", phone_number="+1 800-MANUAL"
    )

    await service._enrich_from_contact(  # noqa: SLF001 - internal behavior test
        session=session,
        stakeholder=stakeholder,
        email="person@example.com",
        manual_overrides={"name", "job_title"},
    )

    assert stakeholder.contact_id == contact.contact_id
    assert stakeholder.name == "Manual Name"
    assert stakeholder.job_title == "Manual Title"


# ---------------------------------------------------------------------------
# _reject_internal_stakeholders
# ---------------------------------------------------------------------------


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

    async def execute(self, _stmt):  # noqa: ANN001
        return _FakeExecuteResult(self._rows)


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


class TestRejectInternalStakeholders:
    @pytest.mark.asyncio
    async def test_rejects_internal_emails(self) -> None:
        service = StakeholderSyncService(uuid4())
        service._internal_domains = {"copilotgtm.com"}

        s_internal = SimpleNamespace(
            email="saksham@copilotgtm.com",
            approval_status=ApprovalStatus.APPROVED,
        )
        s_external = SimpleNamespace(
            email="buyer@acme.com",
            approval_status=ApprovalStatus.APPROVED,
        )
        session = _FakeSession([s_internal, s_external])
        deal_id = uuid4()

        count = await service._reject_internal_stakeholders(session, deal_id)  # noqa: SLF001

        assert count == 1
        assert s_internal.approval_status == ApprovalStatus.REJECTED
        assert s_external.approval_status == ApprovalStatus.APPROVED

    @pytest.mark.asyncio
    async def test_preserves_external_emails(self) -> None:
        service = StakeholderSyncService(uuid4())
        service._internal_domains = {"copilotgtm.com"}

        s1 = SimpleNamespace(email="buyer@acme.com", approval_status=ApprovalStatus.APPROVED)
        s2 = SimpleNamespace(email="cto@bigco.com", approval_status=ApprovalStatus.PENDING)
        session = _FakeSession([s1, s2])
        deal_id = uuid4()

        count = await service._reject_internal_stakeholders(session, deal_id)  # noqa: SLF001

        assert count == 0
        assert s1.approval_status == ApprovalStatus.APPROVED
        assert s2.approval_status == ApprovalStatus.PENDING

    @pytest.mark.asyncio
    async def test_skips_already_rejected(self) -> None:
        """Already-REJECTED stakeholders are filtered by WHERE clause.

        The fake session returns all rows, so we verify the method
        is idempotent: an already-REJECTED stakeholder stays REJECTED.
        """
        service = StakeholderSyncService(uuid4())
        service._internal_domains = {"copilotgtm.com"}

        already_rejected = SimpleNamespace(
            email="old@copilotgtm.com",
            approval_status=ApprovalStatus.REJECTED,
        )
        session = _FakeSession([already_rejected])
        deal_id = uuid4()

        await service._reject_internal_stakeholders(session, deal_id)  # noqa: SLF001

        assert already_rejected.approval_status == ApprovalStatus.REJECTED

    @pytest.mark.asyncio
    async def test_no_internal_domains_returns_zero(self) -> None:
        service = StakeholderSyncService(uuid4())
        service._internal_domains = set()

        s1 = SimpleNamespace(email="anyone@anywhere.com", approval_status=ApprovalStatus.APPROVED)
        session = _FakeSession([s1])
        deal_id = uuid4()

        count = await service._reject_internal_stakeholders(session, deal_id)  # noqa: SLF001

        assert count == 0
        assert s1.approval_status == ApprovalStatus.APPROVED

    @pytest.mark.asyncio
    async def test_rejects_multiple_internal_domains(self) -> None:
        service = StakeholderSyncService(uuid4())
        service._internal_domains = {"copilotgtm.com", "copilot.io"}

        s1 = SimpleNamespace(email="a@copilotgtm.com", approval_status=ApprovalStatus.APPROVED)
        s2 = SimpleNamespace(email="b@copilot.io", approval_status=ApprovalStatus.APPROVED)
        s3 = SimpleNamespace(email="buyer@acme.com", approval_status=ApprovalStatus.APPROVED)
        session = _FakeSession([s1, s2, s3])
        deal_id = uuid4()

        count = await service._reject_internal_stakeholders(session, deal_id)  # noqa: SLF001

        assert count == 2
        assert s1.approval_status == ApprovalStatus.REJECTED
        assert s2.approval_status == ApprovalStatus.REJECTED
        assert s3.approval_status == ApprovalStatus.APPROVED


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
