from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.workers.deal_worker.config import DealWorkerConfig
from src.workers.deal_worker.tasks.deal_mappers.contact_communication_hydration_tasks import (
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
