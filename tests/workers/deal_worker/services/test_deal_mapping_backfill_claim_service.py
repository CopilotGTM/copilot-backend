"""Tests for durable initial deal-mapping backfill claims."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import src.workers.deal_worker.services.deal_mapper_services.deal_mapping_backfill_claim_service as backfill_module
from src.domain.models.organisation_tables.job_models import DealJobType, JobStatus
from src.workers.deal_worker.services.deal_mapper_services.deal_mapping_backfill_claim_service import (
    INITIAL_BACKFILL_CANDIDATE_SCAN_LIMIT,
    DealMappingBackfillClaimService,
)


@pytest.mark.asyncio
async def test_claim_stakeholder_backfill_batch_limits_claims_to_requested_size(
    monkeypatch,
) -> None:
    organisation_id = uuid4()
    eligible_deal_ids = [uuid4() for _ in range(101)]
    session = _FakeSession(existing_jobs=[])
    service = DealMappingBackfillClaimService(organisation_id)
    service._eligibility = SimpleNamespace(  # noqa: SLF001
        list_eligible_deal_ids_with_scan_metrics=AsyncMock(
            return_value=_eligibility_result(eligible_deal_ids, scanned=101, capped=False)
        )
    )
    monkeypatch.setattr(
        backfill_module,
        "acquire_org_async_session",
        _session_generator_factory([session]),
    )

    result = await service.claim_stakeholder_backfill_batch(limit=100)

    assert result["eligible_deals"] == 101
    assert result["candidate_deals_scanned"] == 101
    assert result["candidate_scan_limit"] == INITIAL_BACKFILL_CANDIDATE_SCAN_LIMIT
    assert result["candidate_scan_capped"] is False
    assert result["claimable_deals"] == 101
    assert result["claimed_deals"] == 100
    assert len(result["deal_tasks"]) == 100
    assert len(session.added) == 100
    assert all(job.job_type == DealJobType.STAKEHOLDER_SYNC for job in session.added)
    assert all(job.status == JobStatus.PENDING for job in session.added)
    assert all(job.job_metadata["mapping_purpose"] == "initial_backfill" for job in session.added)
    assert session.lock_attempted is True
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_claim_stakeholder_backfill_batch_skips_active_and_completed_only(
    monkeypatch,
) -> None:
    organisation_id = uuid4()
    active_deal_id = uuid4()
    completed_deal_id = uuid4()
    failed_deal_id = uuid4()
    new_deal_id = uuid4()
    session = _FakeSession(
        existing_jobs=[
            _job(active_deal_id, JobStatus.PENDING),
            _job(completed_deal_id, JobStatus.COMPLETED),
            _job(failed_deal_id, JobStatus.FAILED),
        ]
    )
    service = DealMappingBackfillClaimService(organisation_id)
    service._eligibility = SimpleNamespace(  # noqa: SLF001
        list_eligible_deal_ids_with_scan_metrics=AsyncMock(
            return_value=_eligibility_result(
                [active_deal_id, completed_deal_id, failed_deal_id, new_deal_id],
                scanned=4,
                capped=False,
            )
        )
    )
    monkeypatch.setattr(
        backfill_module,
        "acquire_org_async_session",
        _session_generator_factory([session]),
    )

    result = await service.claim_stakeholder_backfill_batch(limit=100)

    assert result["already_active"] == 1
    assert result["already_completed"] == 1
    assert result["claimable_deals"] == 2
    assert [job.deal_id for job in session.added] == [failed_deal_id, new_deal_id]


@pytest.mark.asyncio
async def test_claim_stakeholder_backfill_batch_excludes_existing_claims_before_scan(
    monkeypatch,
) -> None:
    organisation_id = uuid4()
    active_deal_id = uuid4()
    completed_deal_id = uuid4()
    new_deal_id = uuid4()
    session = _FakeSession(
        existing_jobs=[
            _job(active_deal_id, JobStatus.PENDING),
            _job(completed_deal_id, JobStatus.COMPLETED),
        ]
    )
    eligibility = SimpleNamespace(
        list_eligible_deal_ids_with_scan_metrics=AsyncMock(
            return_value=_eligibility_result([new_deal_id], scanned=1, capped=False)
        )
    )
    service = DealMappingBackfillClaimService(organisation_id)
    service._eligibility = eligibility  # noqa: SLF001
    monkeypatch.setattr(
        backfill_module,
        "acquire_org_async_session",
        _session_generator_factory([session]),
    )

    result = await service.claim_stakeholder_backfill_batch(limit=100)

    eligibility.list_eligible_deal_ids_with_scan_metrics.assert_awaited_once_with(
        exclude_deal_ids={active_deal_id, completed_deal_id},
        candidate_scan_limit=INITIAL_BACKFILL_CANDIDATE_SCAN_LIMIT,
    )
    assert result["eligible_deals"] == 1
    assert result["claimable_deals"] == 1
    assert [job.deal_id for job in session.added] == [new_deal_id]


@pytest.mark.asyncio
async def test_claim_stakeholder_backfill_batch_replaces_stale_pending_job(
    monkeypatch,
) -> None:
    organisation_id = uuid4()
    stale_deal_id = uuid4()
    fresh_deal_id = uuid4()
    stale_job = _job(
        stale_deal_id,
        JobStatus.PENDING,
        created_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    fresh_job = _job(
        fresh_deal_id,
        JobStatus.PENDING,
        created_at=datetime.now(UTC),
    )
    session = _FakeSession(existing_jobs=[stale_job, fresh_job])
    service = DealMappingBackfillClaimService(organisation_id)
    service._eligibility = SimpleNamespace(  # noqa: SLF001
        list_eligible_deal_ids_with_scan_metrics=AsyncMock(
            return_value=_eligibility_result([stale_deal_id, fresh_deal_id])
        )
    )
    monkeypatch.setattr(
        backfill_module,
        "acquire_org_async_session",
        _session_generator_factory([session]),
    )

    result = await service.claim_stakeholder_backfill_batch(limit=100)

    assert result["already_active"] == 1
    assert result["claimable_deals"] == 1
    assert [job.deal_id for job in session.added] == [stale_deal_id]
    assert stale_job.status == JobStatus.FAILED
    assert stale_job.error_message == "Stale initial backfill job replaced"
    assert stale_job.job_metadata["stale_replaced"] is True
    assert fresh_job.status == JobStatus.PENDING


@pytest.mark.asyncio
async def test_claim_stakeholder_backfill_batch_reports_capped_candidate_scan(
    monkeypatch,
    caplog,
) -> None:
    organisation_id = uuid4()
    eligible_deal_id = uuid4()
    session = _FakeSession(existing_jobs=[])
    eligibility = SimpleNamespace(
        list_eligible_deal_ids_with_scan_metrics=AsyncMock(
            return_value=_eligibility_result([eligible_deal_id], scanned=1000, capped=True)
        )
    )
    service = DealMappingBackfillClaimService(organisation_id)
    service._eligibility = eligibility  # noqa: SLF001
    caplog.set_level("INFO", logger=backfill_module.__name__)
    monkeypatch.setattr(
        backfill_module,
        "acquire_org_async_session",
        _session_generator_factory([session]),
    )

    result = await service.claim_stakeholder_backfill_batch(limit=100)

    eligibility.list_eligible_deal_ids_with_scan_metrics.assert_awaited_once_with(
        exclude_deal_ids=set(),
        candidate_scan_limit=INITIAL_BACKFILL_CANDIDATE_SCAN_LIMIT,
    )
    assert result["candidate_deals_scanned"] == 1000
    assert result["candidate_scan_limit"] == INITIAL_BACKFILL_CANDIDATE_SCAN_LIMIT
    assert result["candidate_scan_capped"] is True
    assert "Initial deal-mapping backfill candidate scan capped" in caplog.text


@pytest.mark.asyncio
async def test_claim_stakeholder_backfill_batch_skips_when_claim_lock_is_busy(
    monkeypatch,
) -> None:
    organisation_id = uuid4()
    session = _FakeSession(existing_jobs=[], lock_acquired=False)
    eligibility = SimpleNamespace(list_eligible_deal_ids_with_scan_metrics=AsyncMock())
    service = DealMappingBackfillClaimService(organisation_id)
    service._eligibility = eligibility  # noqa: SLF001
    monkeypatch.setattr(
        backfill_module,
        "acquire_org_async_session",
        _session_generator_factory([session]),
    )

    result = await service.claim_stakeholder_backfill_batch(limit=100)

    assert result["status"] == "skipped"
    assert result["reason"] == "claim_lock_not_acquired"
    assert result["claimed_deals"] == 0
    assert result["deal_tasks"] == []
    assert session.added == []
    assert session.lock_attempted is True
    eligibility.list_eligible_deal_ids_with_scan_metrics.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_mark_dispatch_failed_marks_initial_backfill_job_failed(
    monkeypatch,
    caplog,
) -> None:
    organisation_id = uuid4()
    job_id = uuid4()
    job = _job(uuid4(), JobStatus.PENDING, job_id=job_id)
    session = _FakeSession(existing_jobs=[job])
    caplog.set_level("WARNING", logger=backfill_module.__name__)
    monkeypatch.setattr(
        backfill_module,
        "acquire_org_async_session",
        _session_generator_factory([session]),
    )

    await DealMappingBackfillClaimService(organisation_id).mark_dispatch_failed(
        job_id=job_id,
        error="broker unavailable",
    )

    assert job.status == JobStatus.FAILED
    assert job.error_message == "Initial backfill dispatch failed: broker unavailable"
    assert job.job_metadata["dispatch_status"] == "failed"
    assert job.job_metadata["dispatch_error"] == "broker unavailable"
    assert "Initial backfill dispatch failed after claim" in caplog.text
    session.commit.assert_awaited_once()


def test_initial_backfill_stale_thresholds_use_environment(monkeypatch) -> None:
    job = _job(
        uuid4(),
        JobStatus.PENDING,
        created_at=datetime.now(UTC) - timedelta(minutes=10),
    )

    monkeypatch.setenv("DEAL_MAPPING_INITIAL_BACKFILL_STALE_PENDING_MINUTES", "15")
    assert DealMappingBackfillClaimService._is_stale_active_job(job) is False  # noqa: SLF001

    monkeypatch.setenv("DEAL_MAPPING_INITIAL_BACKFILL_STALE_PENDING_MINUTES", "5")
    assert DealMappingBackfillClaimService._is_stale_active_job(job) is True  # noqa: SLF001


def _job(
    deal_id,
    status: JobStatus,
    *,
    job_id=None,
    created_at=None,
    updated_at=None,
) -> SimpleNamespace:
    return SimpleNamespace(
        job_id=job_id or uuid4(),
        deal_id=deal_id,
        status=status,
        created_at=created_at or datetime.now(UTC),
        updated_at=updated_at or created_at or datetime.now(UTC),
        error_message=None,
        job_metadata={"mapping_purpose": "initial_backfill"},
    )


def _eligibility_result(
    eligible_deal_ids,
    *,
    scanned: int | None = None,
    capped: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        eligible_deal_ids=eligible_deal_ids,
        candidate_deals_scanned=len(eligible_deal_ids) if scanned is None else scanned,
        candidate_scan_limit=INITIAL_BACKFILL_CANDIDATE_SCAN_LIMIT,
        candidate_scan_capped=capped,
    )


class _FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeExecuteResult:
    def __init__(self, rows=None, scalar_value=None):
        self._rows = rows
        self._scalar_value = scalar_value

    def scalars(self) -> _FakeScalarResult:
        return _FakeScalarResult(self._rows)

    def scalar(self):
        return self._scalar_value


class _FakeSession:
    def __init__(self, *, existing_jobs, lock_acquired: bool = True):
        self.existing_jobs = existing_jobs
        self.added = []
        self.commit = AsyncMock()
        self.lock_acquired = lock_acquired
        self.lock_attempted = False

    async def execute(self, stmt, params=None):
        if params and "lock_key" in params:
            self.lock_attempted = True
            return _FakeExecuteResult(scalar_value=self.lock_acquired)
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
