from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.master_tables.user_models import UserAppRole
from src.service.crm.access.record_access_policy import CRMAccessActor
from src.service.crm.tasks.contracts import TaskWriteServiceError
from src.service.crm.tasks.write_service import TaskWriteService


def _service(actor: CRMAccessActor) -> TaskWriteService:
    session = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    return TaskWriteService(session, uuid4(), actor.user_id, access_actor=actor)


def _task(*, creator_id, assignee_id=None, deal_id=None):
    return SimpleNamespace(
        task_id=uuid4(),
        deal_id=deal_id,
        status="NOT_STARTED",
        task_metadata={"actor_user_id": str(creator_id)},
        created_by_user_id=creator_id,
        assigned_to_user_id=assignee_id,
        assigned_to_email=None,
        title="Follow up",
        description=None,
        is_deleted=False,
    )


@pytest.mark.asyncio
async def test_unrelated_member_cannot_update_standalone_task() -> None:
    creator_id = uuid4()
    actor = CRMAccessActor(uuid4(), UserAppRole.MEMBER)
    service = _service(actor)

    with pytest.raises(TaskWriteServiceError, match="Task not found"):
        await service._require_task_update(_task(creator_id=creator_id), {"subject": "Changed"})


@pytest.mark.asyncio
async def test_standalone_assignee_cannot_reassign_task() -> None:
    creator_id = uuid4()
    actor = CRMAccessActor(uuid4(), UserAppRole.MEMBER)
    service = _service(actor)

    with pytest.raises(TaskWriteServiceError, match="Only the task creator"):
        await service._require_task_update(
            _task(creator_id=creator_id, assignee_id=actor.user_id),
            {"owner_user_id": uuid4()},
        )


@pytest.mark.asyncio
async def test_unrelated_member_cannot_delete_standalone_task() -> None:
    creator_id = uuid4()
    actor = CRMAccessActor(uuid4(), UserAppRole.MEMBER)
    service = _service(actor)
    task = _task(creator_id=creator_id)
    service._load_user_task = AsyncMock(return_value=task)
    service._reminders.cancel_task_reminders = AsyncMock()
    service._projection.soft_delete_activity_projection = AsyncMock()

    with pytest.raises(TaskWriteServiceError, match="Task not found"):
        await service.delete_task(task.task_id)


@pytest.mark.asyncio
async def test_task_relink_requires_access_to_target_deal() -> None:
    actor = CRMAccessActor(uuid4(), UserAppRole.MEMBER)
    service = _service(actor)
    task = _task(creator_id=actor.user_id)
    target_deal_id = uuid4()
    service._load_user_task = AsyncMock(return_value=task)
    service._require_task_update = AsyncMock()
    service._load_deal = AsyncMock(return_value=SimpleNamespace(deal_id=target_deal_id))
    service._require_deal_access = AsyncMock()
    service._projection.upsert_task = AsyncMock()

    await service.update_task(task.task_id, {"deal_id": target_deal_id})

    service._require_deal_access.assert_awaited_once_with(target_deal_id, "collaborate")
