"""Tests for communication-driven deal mapping triggers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import src.workers.deal_worker.services.deal_mapper_services.communication_deal_mapping_trigger_service as trigger_module
from src.domain.models.organisation_tables.job_models import DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStatus, CRMParticipantType
from src.workers.deal_worker.services.deal_mapper_services.communication_deal_mapping_trigger_service import (
    COMMUNICATION_SYNC_CHANGE_TYPE,
    COMMUNICATION_SYNC_MAPPING_PURPOSE,
    CommunicationDealMappingTriggerService,
)


@pytest.mark.asyncio
async def test_internal_only_email_dispatches_no_jobs(monkeypatch) -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    service = CommunicationDealMappingTriggerService(organisation_id)
    service._load_changed_records = AsyncMock(  # noqa: SLF001
        return_value=[
            SimpleNamespace(
                record_id=uuid4(),
                participant_emails=["owner@example.com", "team@example.com"],
                existing_deal_ids=[],
            )
        ]
    )
    service._load_public_user_emails = AsyncMock(  # noqa: SLF001
        return_value={"owner@example.com", "team@example.com"}
    )
    service._load_integration_emails = AsyncMock(return_value=set())  # noqa: SLF001
    service._load_organisation_domains = AsyncMock(return_value={"example.com"})  # noqa: SLF001
    service._load_internal_anchored_open_deals = AsyncMock()  # noqa: SLF001

    result = await service.trigger_for_changed_communications(
        integration_id=integration_id,
        source_kind="email",
        trigger_source="initial_sync",
        changed_record_ids=[uuid4()],
        sync_started_at=datetime.now(UTC),
    )

    assert result.to_dict()["skipped_internal_only"] == 1
    assert result.to_dict()["jobs_dispatched"] == 0
    service._load_internal_anchored_open_deals.assert_not_awaited()  # noqa: SLF001


@pytest.mark.asyncio
async def test_org_domain_participants_are_internal_even_when_not_public_users() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    service = CommunicationDealMappingTriggerService(organisation_id)
    service._load_changed_records = AsyncMock(  # noqa: SLF001
        return_value=[
            SimpleNamespace(
                record_id=uuid4(),
                participant_emails=[
                    "owner@acme.com",
                    "non.onboarded.one@acme.com",
                    "non.onboarded.two@acme.com",
                ],
                existing_deal_ids=[],
            )
        ]
    )
    service._load_public_user_emails = AsyncMock(return_value={"owner@acme.com"})  # noqa: SLF001
    service._load_integration_emails = AsyncMock(return_value=set())  # noqa: SLF001
    service._load_organisation_domains = AsyncMock(return_value={"acme.com"})  # noqa: SLF001
    service._load_internal_anchored_open_deals = AsyncMock()  # noqa: SLF001

    result = await service.trigger_for_changed_communications(
        integration_id=integration_id,
        source_kind="email",
        trigger_source="initial_sync",
        changed_record_ids=[uuid4()],
        sync_started_at=datetime.now(UTC),
    )

    assert result.to_dict()["skipped_internal_only"] == 1
    assert result.to_dict()["jobs_dispatched"] == 0
    service._load_internal_anchored_open_deals.assert_not_awaited()  # noqa: SLF001


@pytest.mark.asyncio
async def test_exact_external_contact_and_internal_user_dispatches_stakeholder_job(
    monkeypatch,
) -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    service = CommunicationDealMappingTriggerService(organisation_id)
    service._load_changed_records = AsyncMock(  # noqa: SLF001
        return_value=[
            SimpleNamespace(
                record_id=uuid4(),
                participant_emails=["owner@example.com", "buyer@customer.com"],
                existing_deal_ids=[],
            )
        ]
    )
    service._load_public_user_emails = AsyncMock(return_value={"owner@example.com"})  # noqa: SLF001
    service._load_integration_emails = AsyncMock(return_value=set())  # noqa: SLF001
    service._load_organisation_domains = AsyncMock(return_value={"example.com"})  # noqa: SLF001
    service._load_internal_anchored_open_deals = AsyncMock(  # noqa: SLF001
        return_value=[
            _deal(
                deal_id,
                owner_email="owner@example.com",
                primary_contact_email="buyer@customer.com",
            )
        ]
    )
    service._eligibility = _eligibility_stub(["owner@example.com"])  # noqa: SLF001
    session = _FakeSession(existing_jobs=[])
    monkeypatch.setattr(
        trigger_module,
        "acquire_org_async_session",
        _session_generator_factory([session]),
    )
    dispatched: list[dict] = []

    async def _fake_send_deal_task_async(*args, **kwargs):
        dispatched.append({"args": args, "kwargs": kwargs})
        return "task-1"

    monkeypatch.setattr(trigger_module, "send_deal_task_async", _fake_send_deal_task_async)

    result = await service.trigger_for_changed_communications(
        integration_id=integration_id,
        source_kind="email",
        trigger_source="initial_sync",
        changed_record_ids=[uuid4()],
        sync_started_at=datetime.now(UTC),
    )

    assert result.to_dict()["eligible_deals"] == 1
    assert result.to_dict()["jobs_created"] == 1
    assert result.to_dict()["jobs_dispatched"] == 1
    assert session.added[0].job_type == DealJobType.STAKEHOLDER_SYNC
    assert session.added[0].job_metadata["mapping_purpose"] == COMMUNICATION_SYNC_MAPPING_PURPOSE
    assert dispatched[0]["args"] == ("stakeholder.sync_deal_stakeholders",)
    assert dispatched[0]["kwargs"]["kwargs"]["change_type"] == COMMUNICATION_SYNC_CHANGE_TYPE
    assert dispatched[0]["kwargs"]["kwargs"]["job_id"] == str(session.added[0].job_id)


@pytest.mark.asyncio
async def test_webhook_uses_targeted_external_contact_lookup_not_internal_deal_fanout(
    monkeypatch,
) -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    service = CommunicationDealMappingTriggerService(organisation_id)
    service._load_changed_records = AsyncMock(  # noqa: SLF001
        return_value=[
            SimpleNamespace(
                record_id=uuid4(),
                participant_emails=["owner@example.com", "buyer@customer.com"],
                existing_deal_ids=[],
            )
        ]
    )
    service._load_public_user_emails = AsyncMock(return_value={"owner@example.com"})  # noqa: SLF001
    service._load_integration_emails = AsyncMock(return_value=set())  # noqa: SLF001
    service._load_organisation_domains = AsyncMock(return_value={"example.com"})  # noqa: SLF001
    service._load_internal_anchored_open_deals = AsyncMock(  # noqa: SLF001
        side_effect=AssertionError("webhook must not fan out through internal-user deals")
    )
    service._load_targeted_open_deals_for_webhook = AsyncMock(  # noqa: SLF001
        return_value=[
            _deal(
                deal_id,
                owner_email="owner@example.com",
                primary_contact_email="buyer@customer.com",
            )
        ]
    )
    service._eligibility = _eligibility_stub(["owner@example.com"])  # noqa: SLF001
    session = _FakeSession(existing_jobs=[])
    monkeypatch.setattr(
        trigger_module,
        "acquire_org_async_session",
        _session_generator_factory([session]),
    )

    async def _fake_send_deal_task_async(*_args, **_kwargs):
        return "task-1"

    monkeypatch.setattr(trigger_module, "send_deal_task_async", _fake_send_deal_task_async)

    result = await service.trigger_for_changed_communications(
        integration_id=integration_id,
        source_kind="email",
        trigger_source="webhook",
        changed_record_ids=[uuid4()],
        sync_started_at=datetime.now(UTC),
    )

    assert result.to_dict()["candidate_deals"] == 1
    assert result.to_dict()["jobs_dispatched"] == 1
    service._load_internal_anchored_open_deals.assert_not_awaited()  # noqa: SLF001
    service._load_targeted_open_deals_for_webhook.assert_awaited_once()  # noqa: SLF001


@pytest.mark.asyncio
async def test_existing_association_dispatches_even_when_record_is_internal_only(
    monkeypatch,
) -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    record_id = uuid4()
    service = CommunicationDealMappingTriggerService(organisation_id)
    service._load_changed_records = AsyncMock(  # noqa: SLF001
        return_value=[
            SimpleNamespace(
                record_id=record_id,
                participant_emails=["owner@example.com", "teammate@example.com"],
                existing_deal_ids=[deal_id],
            )
        ]
    )
    service._load_public_user_emails = AsyncMock(  # noqa: SLF001
        return_value={"owner@example.com", "teammate@example.com"}
    )
    service._load_integration_emails = AsyncMock(return_value=set())  # noqa: SLF001
    service._load_organisation_domains = AsyncMock(return_value={"example.com"})  # noqa: SLF001
    service._load_targeted_open_deals_for_webhook = AsyncMock(  # noqa: SLF001
        return_value=[_deal(deal_id, owner_email="owner@example.com")]
    )
    service._eligibility = _eligibility_stub(["owner@example.com"])  # noqa: SLF001
    session = _FakeSession(existing_jobs=[])
    monkeypatch.setattr(
        trigger_module,
        "acquire_org_async_session",
        _session_generator_factory([session]),
    )
    dispatched: list[dict] = []

    async def _fake_send_deal_task_async(*args, **kwargs):
        dispatched.append({"args": args, "kwargs": kwargs})
        return "task-1"

    monkeypatch.setattr(trigger_module, "send_deal_task_async", _fake_send_deal_task_async)

    result = await service.trigger_for_changed_communications(
        integration_id=integration_id,
        source_kind="calendar",
        trigger_source="webhook",
        changed_record_ids=[record_id],
        sync_started_at=datetime.now(UTC),
    )

    assert result.to_dict()["candidate_deals"] == 1
    assert result.to_dict()["jobs_created"] == 1
    assert result.to_dict()["jobs_dispatched"] == 1
    assert session.added[0].job_metadata["candidate_reason"] == (
        "existing_communication_association"
    )
    assert session.added[0].job_metadata["changed_record_sample"] == [str(record_id)]
    assert dispatched[0]["args"] == ("stakeholder.sync_deal_stakeholders",)
    service._load_targeted_open_deals_for_webhook.assert_awaited_once()  # noqa: SLF001


@pytest.mark.asyncio
async def test_existing_association_webhook_does_not_need_external_participant(
    monkeypatch,
) -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    service = CommunicationDealMappingTriggerService(organisation_id)
    service._load_changed_records = AsyncMock(  # noqa: SLF001
        return_value=[
            SimpleNamespace(
                record_id=uuid4(),
                participant_emails=["owner@example.com"],
                existing_deal_ids=[deal_id],
            )
        ]
    )
    service._load_public_user_emails = AsyncMock(return_value={"owner@example.com"})  # noqa: SLF001
    service._load_integration_emails = AsyncMock(return_value=set())  # noqa: SLF001
    service._load_organisation_domains = AsyncMock(return_value={"example.com"})  # noqa: SLF001
    service._load_internal_anchored_open_deals = AsyncMock(  # noqa: SLF001
        side_effect=AssertionError("webhook must not fan out through internal-user deals")
    )
    service._load_targeted_open_deals_for_webhook = AsyncMock(  # noqa: SLF001
        return_value=[_deal(deal_id, owner_email="owner@example.com")]
    )
    service._eligibility = _eligibility_stub(["owner@example.com"])  # noqa: SLF001
    session = _FakeSession(existing_jobs=[])
    monkeypatch.setattr(
        trigger_module,
        "acquire_org_async_session",
        _session_generator_factory([session]),
    )

    async def _fake_send_deal_task_async(*_args, **_kwargs):
        return "task-1"

    monkeypatch.setattr(trigger_module, "send_deal_task_async", _fake_send_deal_task_async)

    result = await service.trigger_for_changed_communications(
        integration_id=integration_id,
        source_kind="email",
        trigger_source="webhook",
        changed_record_ids=[uuid4()],
        sync_started_at=datetime.now(UTC),
    )

    assert result.to_dict()["candidate_deals"] == 1
    assert result.to_dict()["jobs_dispatched"] == 1
    service._load_internal_anchored_open_deals.assert_not_awaited()  # noqa: SLF001
    service._load_targeted_open_deals_for_webhook.assert_awaited_once()  # noqa: SLF001


@pytest.mark.asyncio
async def test_existing_association_batch_loads_associated_deal_without_internal_anchor(
    monkeypatch,
) -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    service = CommunicationDealMappingTriggerService(organisation_id)
    service._load_changed_records = AsyncMock(  # noqa: SLF001
        return_value=[
            SimpleNamespace(
                record_id=uuid4(),
                participant_emails=["sender@example.com", "buyer@customer.com"],
                existing_deal_ids=[deal_id],
            )
        ]
    )
    service._load_public_user_emails = AsyncMock(return_value={"sender@example.com"})  # noqa: SLF001
    service._load_integration_emails = AsyncMock(return_value=set())  # noqa: SLF001
    service._load_organisation_domains = AsyncMock(return_value={"example.com"})  # noqa: SLF001
    service._load_internal_anchored_open_deals = AsyncMock(return_value=[])  # noqa: SLF001
    service._load_existing_associated_open_deals = AsyncMock(  # noqa: SLF001
        return_value=[_deal(deal_id, owner_email="owner@example.com")]
    )
    service._eligibility = _eligibility_stub(["owner@example.com"])  # noqa: SLF001
    session = _FakeSession(existing_jobs=[])
    monkeypatch.setattr(
        trigger_module,
        "acquire_org_async_session",
        _session_generator_factory([session]),
    )

    async def _fake_send_deal_task_async(*_args, **_kwargs):
        return "task-1"

    monkeypatch.setattr(trigger_module, "send_deal_task_async", _fake_send_deal_task_async)

    result = await service.trigger_for_changed_communications(
        integration_id=integration_id,
        source_kind="email",
        trigger_source="incremental_sync",
        changed_record_ids=[uuid4()],
        sync_started_at=datetime.now(UTC),
    )

    assert result.to_dict()["candidate_deals"] == 1
    assert result.to_dict()["jobs_dispatched"] == 1
    service._load_existing_associated_open_deals.assert_awaited_once()  # noqa: SLF001


@pytest.mark.asyncio
async def test_active_event_rematch_job_is_reused_but_completed_does_not_block(
    monkeypatch,
) -> None:
    organisation_id = uuid4()
    active_deal_id = uuid4()
    completed_deal_id = uuid4()
    active_job_id = uuid4()
    session = _FakeSession(
        existing_jobs=[
            _job(active_job_id, active_deal_id, JobStatus.PENDING),
            _job(uuid4(), completed_deal_id, JobStatus.COMPLETED),
        ]
    )
    monkeypatch.setattr(
        trigger_module,
        "acquire_org_async_session",
        _session_generator_factory([session]),
    )

    jobs = await CommunicationDealMappingTriggerService(
        organisation_id
    )._claim_stakeholder_jobs_for_deals(  # noqa: SLF001
        deal_ids=[active_deal_id, completed_deal_id],
        integration_id=uuid4(),
        source_kind="email",
        trigger_source="incremental_sync",
        candidate_reason_by_deal_id={
            active_deal_id: "exact_external_contact_and_internal_user",
            completed_deal_id: "exact_external_contact_and_internal_user",
        },
        changed_record_sample_by_deal_id={active_deal_id: [], completed_deal_id: []},
    )

    assert jobs[0]["job_id"] == str(active_job_id)
    assert jobs[0]["created"] is False
    assert jobs[1]["created"] is True
    assert len(session.added) == 1
    assert session.added[0].deal_id == completed_deal_id


@pytest.mark.asyncio
async def test_reused_active_event_rematch_job_is_not_dispatched_again(
    monkeypatch,
) -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    existing_job_id = uuid4()
    service = CommunicationDealMappingTriggerService(organisation_id)
    service._load_changed_records = AsyncMock(  # noqa: SLF001
        return_value=[
            SimpleNamespace(
                record_id=uuid4(),
                participant_emails=["owner@example.com", "buyer@customer.com"],
                existing_deal_ids=[],
            )
        ]
    )
    service._load_public_user_emails = AsyncMock(return_value={"owner@example.com"})  # noqa: SLF001
    service._load_integration_emails = AsyncMock(return_value=set())  # noqa: SLF001
    service._load_organisation_domains = AsyncMock(return_value={"example.com"})  # noqa: SLF001
    service._load_internal_anchored_open_deals = AsyncMock(  # noqa: SLF001
        return_value=[
            _deal(
                deal_id,
                owner_email="owner@example.com",
                primary_contact_email="buyer@customer.com",
            )
        ]
    )
    service._eligibility = _eligibility_stub(["owner@example.com"])  # noqa: SLF001
    session = _FakeSession(existing_jobs=[_job(existing_job_id, deal_id, JobStatus.IN_PROGRESS)])
    monkeypatch.setattr(
        trigger_module,
        "acquire_org_async_session",
        _session_generator_factory([session]),
    )
    send_task = AsyncMock()
    monkeypatch.setattr(trigger_module, "send_deal_task_async", send_task)

    result = await service.trigger_for_changed_communications(
        integration_id=integration_id,
        source_kind="email",
        trigger_source="incremental_sync",
        changed_record_ids=[uuid4()],
        sync_started_at=datetime.now(UTC),
    )

    assert result.to_dict()["jobs_created"] == 0
    assert result.to_dict()["jobs_reused"] == 1
    assert result.to_dict()["jobs_dispatched"] == 0
    send_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_active_event_rematch_job_is_failed_and_replaced(
    monkeypatch,
    caplog,
) -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    stale_job_id = uuid4()
    stale_job = _job(
        stale_job_id,
        deal_id,
        JobStatus.PENDING,
        created_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    session = _FakeSession(existing_jobs=[stale_job])
    caplog.set_level("INFO", logger=trigger_module.__name__)
    monkeypatch.setattr(
        trigger_module,
        "acquire_org_async_session",
        _session_generator_factory([session]),
    )

    jobs = await CommunicationDealMappingTriggerService(
        organisation_id
    )._claim_stakeholder_jobs_for_deals(  # noqa: SLF001
        deal_ids=[deal_id],
        integration_id=uuid4(),
        source_kind="email",
        trigger_source="incremental_sync",
        candidate_reason_by_deal_id={
            deal_id: "exact_external_contact_and_internal_user",
        },
        changed_record_sample_by_deal_id={deal_id: []},
    )

    assert stale_job.status == JobStatus.FAILED
    assert stale_job.error_message == "Stale event-rematch job replaced"
    assert stale_job.job_metadata["stale_replaced"] is True
    assert jobs[0]["created"] is True
    assert jobs[0]["job_id"] == str(session.added[0].job_id)
    assert session.added[0].deal_id == deal_id
    assert "Stale event-rematch stakeholder job replaced" in caplog.text


def test_event_rematch_stale_thresholds_use_environment(monkeypatch) -> None:
    job = _job(
        uuid4(),
        uuid4(),
        JobStatus.PENDING,
        created_at=datetime.now(UTC) - timedelta(minutes=10),
    )

    monkeypatch.setenv("DEAL_MAPPING_EVENT_REMATCH_STALE_PENDING_MINUTES", "15")
    assert CommunicationDealMappingTriggerService._is_stale_active_job(job) is False  # noqa: SLF001

    monkeypatch.setenv("DEAL_MAPPING_EVENT_REMATCH_STALE_PENDING_MINUTES", "5")
    assert CommunicationDealMappingTriggerService._is_stale_active_job(job) is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_dispatch_cap_is_enforced(monkeypatch, caplog) -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    service = CommunicationDealMappingTriggerService(organisation_id, deal_dispatch_cap=1)
    deal_ids = [uuid4(), uuid4()]
    service._load_changed_records = AsyncMock(  # noqa: SLF001
        return_value=[
            SimpleNamespace(
                record_id=uuid4(),
                participant_emails=["owner@example.com", "buyer@customer.com"],
                existing_deal_ids=[],
            )
        ]
    )
    service._load_public_user_emails = AsyncMock(return_value={"owner@example.com"})  # noqa: SLF001
    service._load_integration_emails = AsyncMock(return_value=set())  # noqa: SLF001
    service._load_organisation_domains = AsyncMock(return_value={"example.com"})  # noqa: SLF001
    service._load_internal_anchored_open_deals = AsyncMock(  # noqa: SLF001
        return_value=[
            _deal(deal_ids[0], owner_email="owner@example.com", company_domain="customer.com"),
            _deal(deal_ids[1], owner_email="owner@example.com", company_domain="customer.com"),
        ]
    )
    service._eligibility = _eligibility_stub(["owner@example.com"])  # noqa: SLF001
    session = _FakeSession(existing_jobs=[])
    caplog.set_level("INFO", logger=trigger_module.__name__)
    monkeypatch.setattr(
        trigger_module,
        "acquire_org_async_session",
        _session_generator_factory([session]),
    )

    async def _fake_send_deal_task_async(*_args, **_kwargs):
        return "task-1"

    monkeypatch.setattr(trigger_module, "send_deal_task_async", _fake_send_deal_task_async)

    result = await service.trigger_for_changed_communications(
        integration_id=integration_id,
        source_kind="email",
        trigger_source="incremental_sync",
        changed_record_ids=[uuid4()],
        sync_started_at=datetime.now(UTC),
    )

    assert result.to_dict()["candidate_deals"] == 2
    assert result.to_dict()["jobs_dispatched"] == 1
    assert result.to_dict()["capped"] is True
    assert "Communication deal-mapping trigger capped" in caplog.text


def _deal(
    deal_id,
    *,
    owner_email: str,
    primary_contact_email: str | None = None,
    company_domain: str | None = None,
):
    return SimpleNamespace(
        deal_id=deal_id,
        organisation_id=uuid4(),
        is_deleted=False,
        deal_status=CRMDealStatus.OPEN,
        owner_email=owner_email,
        primary_contact_email=primary_contact_email,
        company_domain=company_domain,
        participants=[
            SimpleNamespace(
                participant_type=CRMParticipantType.USER,
                related_email=owner_email,
                is_deleted=False,
            )
        ],
    )


def _eligibility_stub(matching_public_user_emails: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        collect_internal_user_emails=lambda deal: [
            participant.related_email
            for participant in getattr(deal, "participants", [])
            if participant.participant_type == CRMParticipantType.USER
            and not participant.is_deleted
            and participant.related_email
        ],
        evaluate_loaded_deal=lambda deal, *, public_user_emails: SimpleNamespace(
            is_eligible=True,
            deal_id=deal.deal_id,
            matching_public_user_emails=matching_public_user_emails,
        ),
    )


def _job(
    job_id,
    deal_id,
    status: JobStatus,
    *,
    created_at=None,
    updated_at=None,
) -> SimpleNamespace:
    return SimpleNamespace(
        job_id=job_id,
        deal_id=deal_id,
        job_type=DealJobType.STAKEHOLDER_SYNC,
        status=status,
        created_at=created_at or datetime.now(UTC),
        updated_at=updated_at or created_at or datetime.now(UTC),
        error_message=None,
        job_metadata={"mapping_purpose": COMMUNICATION_SYNC_MAPPING_PURPOSE},
    )


class _FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self) -> _FakeScalarResult:
        return _FakeScalarResult(self._rows)


class _FakeSession:
    def __init__(self, *, existing_jobs):
        self.existing_jobs = existing_jobs
        self.added = []
        self.commit = AsyncMock()

    async def execute(self, _stmt):
        return _FakeExecuteResult(self.existing_jobs)

    def add(self, job):
        self.added.append(job)


def _session_generator_factory(sessions: list[object]):
    queue = iter(sessions)

    def _factory(*_args: object) -> AsyncIterator[object]:
        async def _generator() -> AsyncIterator[object]:
            yield next(queue)

        return _generator()

    return _factory
