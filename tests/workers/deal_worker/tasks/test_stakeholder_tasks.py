import asyncio
from types import SimpleNamespace

import src.workers.deal_worker.tasks.deal_mappers.stakeholder_tasks as stakeholder_runtime


def test_sync_deal_stakeholders_dispatches_deal_hydration(mocker):
    sync_result = SimpleNamespace(
        success=True,
        created=1,
        updated=0,
        deactivated=0,
        skipped=0,
        errors=[],
        requires_rematching=True,
    )
    sync_service = SimpleNamespace(sync_for_deal=mocker.AsyncMock(return_value=sync_result))
    eligibility_service = SimpleNamespace(
        get_deal_eligibility=mocker.AsyncMock(
            return_value=SimpleNamespace(is_eligible=True, reason=None)
        )
    )
    tracker = SimpleNamespace(
        start=mocker.AsyncMock(), complete=mocker.AsyncMock(), fail=mocker.AsyncMock()
    )
    dispatch = mocker.patch.object(
        stakeholder_runtime,
        "dispatch_contact_communication_hydration_async",
        new=mocker.AsyncMock(return_value=["task-id"]),
    )
    mocker.patch.object(stakeholder_runtime, "StakeholderSyncService", return_value=sync_service)
    mocker.patch.object(
        stakeholder_runtime,
        "DealMappingEligibilityService",
        return_value=eligibility_service,
    )
    mocker.patch.object(stakeholder_runtime, "VisibleDealJobTracker", return_value=tracker)
    mocker.patch.object(
        stakeholder_runtime,
        "run_async",
        side_effect=lambda coroutine: asyncio.run(coroutine),
    )

    result = stakeholder_runtime.sync_deal_stakeholders_task.run(
        organisation_id="00000000-0000-0000-0000-000000000001",
        deal_id="00000000-0000-0000-0000-000000000002",
        change_type="PARTICIPANT_ADDED",
    )

    assert result["rematching_triggered"] is True
    dispatch.assert_awaited_once()
    assert dispatch.await_args.kwargs["deal_ids"] == [
        stakeholder_runtime.UUID("00000000-0000-0000-0000-000000000002")
    ]
    assert dispatch.await_args.kwargs["trigger"] == "deal_participants_changed"
