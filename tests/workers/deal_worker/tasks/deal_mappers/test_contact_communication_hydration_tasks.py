from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
import src.workers.deal_worker.tasks.deal_mappers.contact_communication_hydration_tasks as hydration_tasks
from src.domain.models.organisation_tables.job_models import DealJobType
from src.workers.deal_worker.config import DealWorkerConfig
from src.workers.deal_worker.tasks.deal_mappers.contact_communication_hydration_tasks import (
    _dispatch_association_followups,
    hydrate_contact_communications_task,
)
from src.workers.deal_worker.utils import contact_communication_hydration_dispatch


def test_hydration_task_is_owned_by_the_bounded_deal_queue() -> None:
    assert hydrate_contact_communications_task.name == "deal.hydrate_contact_communications"
    assert DealWorkerConfig().task_routes[hydrate_contact_communications_task.name] == {
        "queue": "deal",
        "routing_key": "deal",
    }


def test_hydration_task_rejects_mixed_contact_and_source_payloads() -> None:
    with pytest.raises(ValueError, match="cannot be mixed"):
        hydrate_contact_communications_task.run(
            organisation_id=str(uuid4()),
            changed_email_ids=[],
            contact_ids=[],
        )


def test_hydration_task_rejects_mixed_deal_and_contact_payloads() -> None:
    """Each task uses exactly one replay mode so no payload can be silently ignored."""
    with pytest.raises(ValueError, match="cannot be mixed"):
        hydrate_contact_communications_task.run(
            organisation_id=str(uuid4()),
            contact_ids=[],
            deal_ids=[],
        )


@pytest.mark.parametrize(
    ("job_type", "starting_method"),
    [
        (DealJobType.STAKEHOLDER_SYNC, "progress"),
        (DealJobType.DEAL_COMMUNICATION_HYDRATION, "start"),
    ],
)
def test_requested_hydration_completes_the_supplied_visible_job(
    monkeypatch,
    job_type,
    starting_method,
) -> None:
    """Requested hydration publishes stages and owns the parent job's terminal state."""
    organisation_id = uuid4()
    deal_id = uuid4()
    job_id = uuid4()
    result = SimpleNamespace(
        email_deal_ids=set(),
        discovered_contact_ids=set(),
        attachment_deal_ids=set(),
        to_dict=lambda: {"associations_created": 2},
    )
    service = SimpleNamespace(backfill_deals=AsyncMock(return_value=result))
    tracker = SimpleNamespace(
        start=AsyncMock(),
        progress=AsyncMock(),
        complete=AsyncMock(),
        fail=AsyncMock(),
    )
    tracker_factory = Mock(return_value=tracker)

    class _Scope:
        def __init__(self, value):
            self.value = value

        async def __aenter__(self):
            return self.value

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(
        hydration_tasks,
        "master_async_session_scope",
        lambda: _Scope(SimpleNamespace()),
    )
    monkeypatch.setattr(
        hydration_tasks,
        "org_async_session_scope",
        lambda _organisation_id: _Scope(SimpleNamespace(commit=AsyncMock())),
    )
    monkeypatch.setattr(
        hydration_tasks,
        "ContactCommunicationHydrationService",
        lambda *_args, **_kwargs: service,
    )
    monkeypatch.setattr(
        hydration_tasks,
        "_dispatch_association_followups",
        AsyncMock(),
    )
    monkeypatch.setattr(hydration_tasks, "VisibleDealJobTracker", tracker_factory, raising=False)

    response = hydrate_contact_communications_task.run(
        organisation_id=str(organisation_id),
        deal_ids=[str(deal_id)],
        trigger="deal_participants_changed",
        job_id=str(job_id),
        job_type=job_type.value,
        emit_events=True,
    )

    tracker_factory.assert_called_once_with(
        organisation_id,
        str(job_id),
        job_type,
        emit_events=True,
    )
    getattr(tracker, starting_method).assert_awaited_once()
    tracker.complete.assert_awaited_once()
    assert response["associations_created"] == 2


@pytest.mark.asyncio
async def test_dispatch_chunks_changed_ids_instead_of_sending_one_task_per_contact(
    monkeypatch,
) -> None:
    send = AsyncMock(side_effect=lambda *args, **kwargs: f"task-{len(send.await_args_list)}")
    monkeypatch.setattr(contact_communication_hydration_dispatch, "send_deal_task_async", send)
    changed_ids = [uuid4() for _ in range(101)]

    task_ids = await contact_communication_hydration_dispatch.dispatch_contact_communication_hydration_async(
        organisation_id=uuid4(),
        integration_id=uuid4(),
        changed_email_ids=changed_ids,
        trigger="email_sync",
    )

    assert len(task_ids) == 2
    assert send.await_count == 2
    assert len(send.await_args_list[0].kwargs["kwargs"]["changed_email_ids"]) == 100
    assert len(send.await_args_list[1].kwargs["kwargs"]["changed_email_ids"]) == 1


@pytest.mark.asyncio
async def test_dispatch_batches_manual_contact_backfill(monkeypatch) -> None:
    send = AsyncMock(return_value="task-1")
    monkeypatch.setattr(contact_communication_hydration_dispatch, "send_deal_task_async", send)
    contact_ids = [uuid4(), uuid4()]

    await contact_communication_hydration_dispatch.dispatch_contact_communication_hydration_async(
        organisation_id=uuid4(),
        contact_ids=contact_ids,
        trigger="manual_contact_create",
    )

    assert send.await_count == 1
    assert send.await_args.kwargs["kwargs"]["contact_ids"] == [str(value) for value in contact_ids]


@pytest.mark.asyncio
async def test_dispatch_batches_targeted_deal_backfill(monkeypatch) -> None:
    """Deal creation/import replays only the affected deals through the hydration queue."""
    send = AsyncMock(return_value="task-1")
    monkeypatch.setattr(contact_communication_hydration_dispatch, "send_deal_task_async", send)
    deal_ids = [uuid4(), uuid4()]

    await contact_communication_hydration_dispatch.dispatch_contact_communication_hydration_async(
        organisation_id=uuid4(),
        deal_ids=deal_ids,
        trigger="deal_participants_changed",
    )

    assert send.await_count == 1
    assert send.await_args.kwargs["kwargs"]["deal_ids"] == [str(value) for value in deal_ids]


@pytest.mark.asyncio
async def test_dispatch_adds_visibility_only_when_caller_supplies_a_job(monkeypatch) -> None:
    """Bulk callers stay silent while one requested job is forwarded to its hydration task."""
    send = AsyncMock(side_effect=["background-task", "visible-task"])
    monkeypatch.setattr(contact_communication_hydration_dispatch, "send_deal_task_async", send)
    organisation_id = uuid4()
    deal_id = uuid4()
    job_id = uuid4()

    await contact_communication_hydration_dispatch.dispatch_contact_communication_hydration_async(
        organisation_id=organisation_id,
        deal_ids=[deal_id],
        trigger="integration_backfill",
    )
    await contact_communication_hydration_dispatch.dispatch_contact_communication_hydration_async(
        organisation_id=organisation_id,
        deal_ids=[deal_id],
        trigger="stakeholder_requested",
        job_id=job_id,
        job_type=DealJobType.STAKEHOLDER_SYNC,
        emit_events=True,
    )

    background_kwargs = send.await_args_list[0].kwargs["kwargs"]
    visible_kwargs = send.await_args_list[1].kwargs["kwargs"]
    assert "job_id" not in background_kwargs
    assert "job_type" not in background_kwargs
    assert visible_kwargs["job_id"] == str(job_id)
    assert visible_kwargs["job_type"] == DealJobType.STAKEHOLDER_SYNC.value


@pytest.mark.asyncio
async def test_dispatch_rejects_one_visible_job_across_multiple_batches(monkeypatch) -> None:
    """One SSE job cannot be finalized independently by several hydration chunks."""
    send = AsyncMock()
    monkeypatch.setattr(contact_communication_hydration_dispatch, "send_deal_task_async", send)

    with pytest.raises(ValueError, match="single hydration batch"):
        await (
            contact_communication_hydration_dispatch.dispatch_contact_communication_hydration_async(
                organisation_id=uuid4(),
                deal_ids=[uuid4() for _ in range(101)],
                trigger="requested_bulk_replay",
                job_id=uuid4(),
                job_type=DealJobType.STAKEHOLDER_SYNC,
                emit_events=True,
            )
        )

    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_rejects_job_context_without_event_request(monkeypatch) -> None:
    """An internal job ID cannot accidentally opt a background hydration into SSE."""
    send = AsyncMock()
    monkeypatch.setattr(contact_communication_hydration_dispatch, "send_deal_task_async", send)

    with pytest.raises(ValueError, match="emit_events"):
        await (
            contact_communication_hydration_dispatch.dispatch_contact_communication_hydration_async(
                organisation_id=uuid4(),
                deal_ids=[uuid4()],
                trigger="initial_backfill",
                job_id=uuid4(),
                job_type=DealJobType.STAKEHOLDER_SYNC,
            )
        )

    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_hydration_followups_research_contacts_and_process_email_deals() -> None:
    organisation_id = uuid4()
    contact_id = uuid4()
    deal_id = uuid4()
    memory_job_id = uuid4()

    with (
        patch(
            "src.workers.deal_worker.tasks.deal_mappers."
            "contact_communication_hydration_tasks.dispatch_contact_research_for_contact",
            new=AsyncMock(),
        ) as research,
        patch(
            "src.workers.deal_worker.tasks.deal_mappers."
            "contact_communication_hydration_tasks.extract_email_attachments_deal_task"
        ) as attachments,
        patch(
            "src.workers.deal_worker.tasks.deal_mappers."
            "contact_communication_hydration_tasks.dispatch_deal_insight_pipeline"
        ) as insights,
    ):
        await _dispatch_association_followups(
            organisation_id=organisation_id,
            discovered_contact_ids={contact_id},
            email_deal_jobs={deal_id: memory_job_id},
            attachment_deal_ids={deal_id},
        )

    research.assert_awaited_once_with(
        organisation_id=organisation_id,
        contact_id=contact_id,
        trigger="communication_participant_discovery",
    )
    attachments.apply_async.assert_called_once()
    assert attachments.apply_async.call_args.kwargs["retry"] is True
    assert attachments.apply_async.call_args.kwargs["retry_policy"]["max_retries"] == 3
    insights.assert_called_once_with(
        str(organisation_id),
        str(deal_id),
        str(memory_job_id),
    )


@pytest.mark.asyncio
async def test_hydration_followups_skip_attachments_without_signal_but_keep_insights() -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    memory_job_id = uuid4()

    with (
        patch(
            "src.workers.deal_worker.tasks.deal_mappers."
            "contact_communication_hydration_tasks.extract_email_attachments_deal_task"
        ) as attachments,
        patch(
            "src.workers.deal_worker.tasks.deal_mappers."
            "contact_communication_hydration_tasks.dispatch_deal_insight_pipeline"
        ) as insights,
    ):
        await _dispatch_association_followups(
            organisation_id=organisation_id,
            discovered_contact_ids=set(),
            email_deal_jobs={deal_id: memory_job_id},
            attachment_deal_ids=set(),
        )

    attachments.apply_async.assert_not_called()
    insights.assert_called_once_with(
        str(organisation_id),
        str(deal_id),
        str(memory_job_id),
    )


@pytest.mark.asyncio
async def test_hydration_followups_keep_insights_when_attachment_publish_fails() -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    memory_job_id = uuid4()

    with (
        patch(
            "src.workers.deal_worker.tasks.deal_mappers."
            "contact_communication_hydration_tasks.extract_email_attachments_deal_task"
        ) as attachments,
        patch(
            "src.workers.deal_worker.tasks.deal_mappers."
            "contact_communication_hydration_tasks.dispatch_deal_insight_pipeline"
        ) as insights,
    ):
        attachments.apply_async.side_effect = RuntimeError("broker unavailable")
        with pytest.raises(RuntimeError, match="broker unavailable"):
            await _dispatch_association_followups(
                organisation_id=organisation_id,
                discovered_contact_ids=set(),
                email_deal_jobs={deal_id: memory_job_id},
                attachment_deal_ids={deal_id},
            )

    insights.assert_called_once_with(
        str(organisation_id),
        str(deal_id),
        str(memory_job_id),
    )
