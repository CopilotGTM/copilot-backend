from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.job_models import DealJobType
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivity,
    CRMActivitySource,
    CRMActivityStatus,
    CRMActivityType,
)
from src.service.crm.internal.deal_task_automation_service import (
    DealTaskAutomationError,
    DealTaskAutomationService,
)
from src.workers.deal_worker.utils import pipeline_dispatch


class _FakeSession:
    def __init__(self, *, get_result: object | None = None):
        self.get_result = get_result
        self.added: list[object] = []
        self.flushed = False
        self.refreshed: list[object] = []
        self.committed = False

    async def get(self, _model: object, _entity_id: object) -> object | None:
        return self.get_result

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed = True

    async def refresh(self, obj: object) -> None:
        self.refreshed.append(obj)

    def flush_sync(self) -> None:
        self.flushed = True

    def commit(self) -> None:
        self.committed = True


class _PipelineSession:
    def __init__(self) -> None:
        self.added = []

    def add(self, item) -> None:
        self.added.append(item)

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        return None


def _deal(*, organisation_id):
    return SimpleNamespace(
        deal_id=uuid4(),
        organisation_id=organisation_id,
        external_id="deal-ext-1",
        name="Acme Expansion",
        is_deleted=False,
    )


def _task(*, organisation_id, deal_id, integration_id=None) -> CRMActivity:
    return CRMActivity(
        activity_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id=str(uuid4()),
        activity_type=CRMActivityType.TASK.value,
        status=CRMActivityStatus.NOT_STARTED.value,
        subject="Send pricing proposal",
        deal_id=deal_id,
        source=CRMActivitySource.AI_MEETING.value,
        properties={},
    )


def test_meeting_summary_pipeline_creates_final_task_sync_job(monkeypatch) -> None:
    monkeypatch.delenv("ENABLE_DEAL_UPDATE_AGENT_PIPELINE", raising=False)
    session = _PipelineSession()

    @contextmanager
    def fake_org_session_scope(_organisation_id):
        yield session

    monkeypatch.setattr(pipeline_dispatch, "org_session_scope", fake_org_session_scope)

    _, _, task_job_id, deal_update_job_id = pipeline_dispatch._create_pipeline_jobs(
        str(uuid4()), str(uuid4())
    )

    assert task_job_id
    assert deal_update_job_id is None
    assert session.added[-1].job_type == DealJobType.DEAL_TASK_SYNC


@pytest.mark.asyncio
async def test_task_sync_creates_task_with_no_assignee() -> None:
    organisation_id = uuid4()
    deal = _deal(organisation_id=organisation_id)
    service = DealTaskAutomationService(_FakeSession(), organisation_id)
    service._load_deal = AsyncMock(return_value=deal)  # type: ignore[method-assign]
    service._find_existing_task = AsyncMock(return_value=None)  # type: ignore[method-assign]
    service._resolve_assignee = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, None, None, {"status": "not_provided"})
    )

    result = await service.create_task(
        deal_id=deal.deal_id,
        subject="Confirm integration timeline",
        body=None,
        assignee_name_or_email=None,
        source_event_id=uuid4(),
        source_association_id=uuid4(),
        source_summary_job_id=uuid4(),
    )

    assert result.created is True
    assert result.task.owner_external_id is None
    assert result.task.properties["assignee_resolution"]["status"] == "not_provided"


@pytest.mark.asyncio
async def test_task_sync_creates_task_with_resolved_assignee() -> None:
    organisation_id = uuid4()
    deal = _deal(organisation_id=organisation_id)
    owner_id = uuid4()
    service = DealTaskAutomationService(_FakeSession(), organisation_id)
    service._load_deal = AsyncMock(return_value=deal)  # type: ignore[method-assign]
    service._find_existing_task = AsyncMock(return_value=None)  # type: ignore[method-assign]
    service._resolve_assignee = AsyncMock(  # type: ignore[method-assign]
        return_value=(owner_id, "Alex Morgan", "alex@example.com", {"status": "resolved"})
    )

    result = await service.create_task(
        deal_id=deal.deal_id,
        subject="Send pricing proposal",
        body="Send revised proposal.",
        assignee_name_or_email="alex@example.com",
        source_event_id=uuid4(),
        source_association_id=uuid4(),
        source_summary_job_id=uuid4(),
    )

    assert result.created is True
    assert result.task.owner_external_id == str(owner_id)
    assert result.task.owner_name == "Alex Morgan"


@pytest.mark.asyncio
async def test_repeated_task_sync_does_not_duplicate_task() -> None:
    organisation_id = uuid4()
    deal = _deal(organisation_id=organisation_id)
    existing_task = _task(organisation_id=organisation_id, deal_id=deal.deal_id)
    service = DealTaskAutomationService(_FakeSession(), organisation_id)
    service._load_deal = AsyncMock(return_value=deal)  # type: ignore[method-assign]
    service._find_existing_task = AsyncMock(return_value=existing_task)  # type: ignore[method-assign]

    result = await service.create_task(
        deal_id=deal.deal_id,
        subject="Send pricing proposal",
        body=None,
        assignee_name_or_email=None,
        source_event_id=uuid4(),
        source_association_id=uuid4(),
        source_summary_job_id=uuid4(),
    )

    assert result.created is False
    assert result.reason == "duplicate"
    assert result.task is existing_task


@pytest.mark.asyncio
async def test_task_sync_completes_existing_internal_task() -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    task = _task(organisation_id=organisation_id, deal_id=deal_id)
    session = _FakeSession(get_result=task)
    service = DealTaskAutomationService(session, organisation_id)

    completed = await service.complete_task(
        task.activity_id,
        deal_id=deal_id,
        completion_reason="Summary says proposal was sent.",
    )

    assert completed.status == CRMActivityStatus.COMPLETED.value
    assert completed.properties["completed_via"] == "deal_task_agent"
    assert session.flushed is True


@pytest.mark.asyncio
async def test_task_sync_skips_crm_synced_task_completion() -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    task = _task(organisation_id=organisation_id, deal_id=deal_id, integration_id=uuid4())
    service = DealTaskAutomationService(_FakeSession(get_result=task), organisation_id)

    with pytest.raises(DealTaskAutomationError) as exc:
        await service.complete_task(
            task.activity_id,
            deal_id=deal_id,
            completion_reason="Summary says proposal was sent.",
        )

    assert exc.value.status_code == 409
    assert task.status == CRMActivityStatus.NOT_STARTED.value
