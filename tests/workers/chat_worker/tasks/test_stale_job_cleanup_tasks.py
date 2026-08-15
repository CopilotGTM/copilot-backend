"""Contracts for bounded cross-organisation stale-work recovery."""

from __future__ import annotations

from unittest.mock import Mock
from uuid import uuid4

from src.workers.chat_worker.tasks import stale_job_cleanup_tasks as cleanup_tasks


def test_organisation_order_resumes_after_previous_cursor() -> None:
    """A tenant behind a persistent backlog must still receive a later sweep."""
    organisation_ids = [uuid4(), uuid4(), uuid4()]

    assert cleanup_tasks._rotate_after_cursor(organisation_ids, organisation_ids[1]) == [
        organisation_ids[2],
        organisation_ids[0],
        organisation_ids[1],
    ]


def test_sweep_processes_at_most_twenty_records_without_org_task_fanout(monkeypatch) -> None:
    """One Beat tick must spend one global budget instead of publishing an org task storm."""
    organisation_ids = [uuid4(), uuid4(), uuid4()]

    class Result:
        def all(self):
            return [(organisation_id,) for organisation_id in organisation_ids]

    class Session:
        async def execute(self, _statement):
            return Result()

    async def sessions():
        yield Session()

    processed = iter([12, 8, 9])

    class Service:
        def __init__(self, organisation_id):
            self.organisation_id = organisation_id

        async def cleanup_stale_jobs_async(self, *, limit):
            return {
                "organisation_id": str(self.organisation_id),
                "processed_count": min(next(processed), limit),
            }

    release = Mock()
    monkeypatch.setattr(cleanup_tasks, "acquire_master_async_session", sessions)
    monkeypatch.setattr(cleanup_tasks, "ChatStaleJobCleanupService", Service)
    monkeypatch.setattr(
        cleanup_tasks, "acquire_task_lock", lambda *_args, **_kwargs: True, raising=False
    )
    monkeypatch.setattr(cleanup_tasks, "release_task_lock", release, raising=False)
    monkeypatch.setattr(cleanup_tasks, "load_cleanup_cursor", lambda: None, raising=False)
    monkeypatch.setattr(cleanup_tasks, "save_cleanup_cursor", Mock(), raising=False)
    result = cleanup_tasks.cleanup_delayed_work_all_orgs_task.run()

    assert result["processed_count"] == 20
    assert result["orgs_scanned"] == 2
    release.assert_called_once()


def test_overlapping_sweep_exits_without_querying_organisations(monkeypatch) -> None:
    """The distributed lock must make overlapping Beat ticks harmless."""
    monkeypatch.setattr(
        cleanup_tasks, "acquire_task_lock", lambda *_args, **_kwargs: False, raising=False
    )
    monkeypatch.setattr(
        cleanup_tasks,
        "acquire_master_async_session",
        Mock(side_effect=AssertionError("locked sweep must not query")),
    )

    result = cleanup_tasks.cleanup_delayed_work_all_orgs_task.run()

    assert result == {"status": "skipped", "reason": "already_running"}
