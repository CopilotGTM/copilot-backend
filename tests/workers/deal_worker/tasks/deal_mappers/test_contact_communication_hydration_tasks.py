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


def test_hydration_task_allows_twenty_minutes_before_soft_timeout() -> None:
    """Long tenant batches get 20 minutes plus one minute to unwind before termination."""
    assert hydrate_contact_communications_task.soft_time_limit == 1200
    assert hydrate_contact_communications_task.time_limit == 1260


def test_hydration_task_retries_org_lock_contention_without_using_a_worker_slot(
    monkeypatch,
) -> None:
    """A long hydration gets enough retry runway without blocking unrelated deals."""
    busy_error = hydration_tasks.CommunicationHydrationBusyError("already in progress")
    retry = Mock(side_effect=RuntimeError("retry scheduled"))

    def raise_busy(coroutine):
        coroutine.close()
        raise busy_error

    monkeypatch.setattr(hydration_tasks, "run_async", raise_busy)
    monkeypatch.setattr(hydrate_contact_communications_task, "retry", retry)

    with pytest.raises(RuntimeError, match="retry scheduled"):
        hydrate_contact_communications_task.run(
            organisation_id=str(uuid4()),
            deal_ids=[str(uuid4())],
        )

    retry.assert_called_once_with(
        exc=busy_error,
        countdown=60,
        max_retries=5,
    )


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


def test_initial_sync_failure_reporting_does_not_mask_hydration_error(monkeypatch) -> None:
    """Progress terminalization is secondary to the original hydration failure."""

    def fail_run(coroutine):
        coroutine.close()
        raise RuntimeError("progress unavailable")

    monkeypatch.setattr(hydration_tasks, "run_async", fail_run)

    hydration_tasks._fail_initial_sync_chunk_safely(  # noqa: SLF001
        organisation_id=uuid4(),
        sync_id=uuid4(),
        branch="email",
        chunk_key="email:0",
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
        attachment_email_ids_by_deal={},
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


def test_tracked_hydration_records_metrics_before_its_data_commit(monkeypatch) -> None:
    """The one-time created flags must be written in the same transaction as CRM rows."""
    order: list[str] = []
    organisation_id = uuid4()
    integration_id = uuid4()
    sync_id = uuid4()
    email_id = uuid4()
    result = SimpleNamespace(
        email_deal_ids=set(),
        discovered_contact_ids=set(),
        attachment_email_ids_by_deal={},
        contacts_created=4,
        accounts_created=2,
        emails_linked=1,
        to_dict=lambda: {},
    )
    service = SimpleNamespace(hydrate_changed_sources=AsyncMock(return_value=result))

    class _Scope:
        def __init__(self, value):
            self.value = value

        async def __aenter__(self):
            return self.value

        async def __aexit__(self, *_args):
            return None

    async def commit():
        order.append("commit")

    async def complete_in_session(*_args, **_kwargs):
        order.append("progress")
        return {"revision": 3}

    org_session = SimpleNamespace(commit=commit)
    monkeypatch.setattr(hydration_tasks, "master_async_session_scope", lambda: _Scope(object()))
    monkeypatch.setattr(
        hydration_tasks,
        "org_async_session_scope",
        lambda _organisation_id: _Scope(org_session),
    )
    monkeypatch.setattr(
        hydration_tasks,
        "ContactCommunicationHydrationService",
        lambda *_args, **_kwargs: service,
    )
    monkeypatch.setattr(hydration_tasks, "_dispatch_association_followups", AsyncMock())
    monkeypatch.setattr(
        hydration_tasks.InitialSyncProgressService,
        "try_complete_hydration_chunk_in_session",
        complete_in_session,
        raising=False,
    )
    monkeypatch.setattr(
        hydration_tasks.InitialSyncProgressService,
        "publish_committed_snapshot",
        Mock(),
        raising=False,
    )
    monkeypatch.setattr(
        hydration_tasks.InitialSyncProgressService,
        "complete_hydration_chunk",
        AsyncMock(side_effect=lambda *_args, **_kwargs: order.append("late-progress")),
    )

    hydrate_contact_communications_task.run(
        organisation_id=str(organisation_id),
        integration_id=str(integration_id),
        changed_email_ids=[str(email_id)],
        trigger="email_sync",
        initial_sync_id=str(sync_id),
        initial_sync_branch="email",
        initial_sync_chunk_key="email:0",
    )

    assert order[:2] == ["progress", "commit"]


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
    email_id = uuid4()

    with (
        patch(
            "src.workers.deal_worker.tasks.deal_mappers."
            "contact_communication_hydration_tasks.dispatch_contact_research_for_contact",
            new=AsyncMock(),
        ) as research,
        patch(
            "src.workers.deal_worker.tasks.deal_mappers."
            "contact_communication_hydration_tasks."
            "dispatch_email_attachment_extraction_async",
            new=AsyncMock(),
            create=True,
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
            attachment_email_ids_by_deal={deal_id: {email_id}},
        )

    research.assert_awaited_once_with(
        organisation_id=organisation_id,
        contact_id=contact_id,
        trigger="communication_participant_discovery",
    )
    attachments.assert_awaited_once_with(
        organisation_id=organisation_id,
        deal_id=deal_id,
        email_ids=[email_id],
        trigger="communication_association",
    )
    insights.assert_called_once_with(
        str(organisation_id),
        str(deal_id),
        str(memory_job_id),
    )


@pytest.mark.asyncio
async def test_hydration_followups_chunk_exact_attachment_email_ids() -> None:
    """Large replays stay bounded and never fall back to a deal-wide scan."""
    organisation_id = uuid4()
    deal_id = uuid4()
    email_ids = {uuid4() for _ in range(101)}

    with (
        patch(
            "src.workers.deal_worker.tasks.deal_mappers."
            "contact_communication_hydration_tasks."
            "dispatch_email_attachment_extraction_async",
            new=AsyncMock(),
            create=True,
        ) as attachments,
        patch(
            "src.workers.deal_worker.tasks.deal_mappers."
            "contact_communication_hydration_tasks.dispatch_deal_insight_pipeline"
        ),
    ):
        await _dispatch_association_followups(
            organisation_id=organisation_id,
            discovered_contact_ids=set(),
            email_deal_jobs={deal_id: uuid4()},
            attachment_email_ids_by_deal={deal_id: email_ids},
        )

    assert attachments.await_count == 2
    payloads = [call.kwargs["email_ids"] for call in attachments.await_args_list]
    assert [len(payload) for payload in payloads] == [100, 1]
    assert {value for payload in payloads for value in payload} == email_ids


@pytest.mark.asyncio
async def test_hydration_followups_skip_attachments_without_signal_but_keep_insights() -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    memory_job_id = uuid4()

    with (
        patch(
            "src.workers.deal_worker.tasks.deal_mappers."
            "contact_communication_hydration_tasks."
            "dispatch_email_attachment_extraction_async",
            new=AsyncMock(),
            create=True,
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
            attachment_email_ids_by_deal={},
        )

    attachments.assert_not_awaited()
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
            "contact_communication_hydration_tasks."
            "dispatch_email_attachment_extraction_async",
            new=AsyncMock(side_effect=RuntimeError("broker unavailable")),
            create=True,
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
            attachment_email_ids_by_deal={deal_id: {uuid4()}},
        )

    assert attachments.await_count == 1
    insights.assert_called_once_with(
        str(organisation_id),
        str(deal_id),
        str(memory_job_id),
    )
