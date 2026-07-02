import asyncio
from types import SimpleNamespace

import src.workers.deal_worker.tasks.deal_mappers.stakeholder_tasks as stakeholder_runtime


def test_sync_deal_stakeholders_propagates_correlation_headers_to_rematching_tasks(mocker):
    class _FakeSyncResult:
        success = True
        created = 1
        updated = 0
        deactivated = 0
        skipped = 0
        errors = []
        requires_rematching = True

    class _FakeService:
        def __init__(self, organisation_id):
            self.organisation_id = organisation_id

        async def sync_for_deal(self, deal_id):
            return _FakeSyncResult()

    class _FakeEligibilityService:
        def __init__(self, organisation_id):
            self.organisation_id = organisation_id

        async def get_deal_eligibility(self, deal_id):
            return SimpleNamespace(is_eligible=True, reason=None)

    class _FakeDownstreamJobService:
        def __init__(self, organisation_id):
            self.organisation_id = organisation_id

        async def claim_rematch_jobs(self, **_kwargs):
            return {
                "calendar_job_id": "00000000-0000-0000-0000-000000000003",
                "email_job_id": "00000000-0000-0000-0000-000000000004",
                "calendar_dispatch_required": True,
                "email_dispatch_required": True,
            }

    class _FakeTracker:
        def __init__(self, *_args, **_kwargs):
            pass

        async def start(self, *_args, **_kwargs):
            return None

        async def complete(self, *_args, **_kwargs):
            return None

        async def fail(self, *_args, **_kwargs):
            return None

    dispatched: list[dict] = []

    def _capture_dispatch(*, kwargs=None, headers=None, **_extra):
        dispatched.append({"kwargs": kwargs, "headers": headers})
        return SimpleNamespace(id="task-id")

    mocker.patch.object(stakeholder_runtime, "StakeholderSyncService", _FakeService)
    mocker.patch.object(
        stakeholder_runtime,
        "DealMappingEligibilityService",
        _FakeEligibilityService,
    )
    mocker.patch.object(
        stakeholder_runtime,
        "DealMappingDownstreamJobService",
        _FakeDownstreamJobService,
    )
    mocker.patch.object(stakeholder_runtime, "VisibleDealJobTracker", _FakeTracker)
    mocker.patch.object(
        stakeholder_runtime,
        "run_async",
        side_effect=lambda coroutine: asyncio.run(coroutine),
    )
    mocker.patch.object(
        stakeholder_runtime,
        "correlation_headers",
        return_value={"request_id": "req-123"},
    )
    mocker.patch.object(
        stakeholder_runtime.match_calendar_to_deals_task,
        "apply_async",
        side_effect=_capture_dispatch,
    )
    mocker.patch.object(
        stakeholder_runtime.match_email_to_deals_task,
        "apply_async",
        side_effect=_capture_dispatch,
    )

    result = stakeholder_runtime.sync_deal_stakeholders_task.run(
        organisation_id="00000000-0000-0000-0000-000000000001",
        deal_id="00000000-0000-0000-0000-000000000002",
        change_type="PARTICIPANT_ADDED",
    )

    # Both sibling rematching tasks should inherit the exact same correlation payload.
    assert result["rematching_triggered"] is True
    assert len(dispatched) == 2
    assert dispatched[0]["kwargs"] == {
        "organisation_id": "00000000-0000-0000-0000-000000000001",
        "deal_id": "00000000-0000-0000-0000-000000000002",
        "job_id": "00000000-0000-0000-0000-000000000003",
    }
    assert dispatched[0]["headers"] == {"request_id": "req-123"}
    assert dispatched[1]["kwargs"] == {
        "organisation_id": "00000000-0000-0000-0000-000000000001",
        "deal_id": "00000000-0000-0000-0000-000000000002",
        "job_id": "00000000-0000-0000-0000-000000000004",
    }
    assert dispatched[1]["headers"] == {"request_id": "req-123"}
