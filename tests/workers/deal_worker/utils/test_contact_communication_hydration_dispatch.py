from uuid import uuid4

import pytest
from src.workers.deal_worker.utils import contact_communication_hydration_dispatch as dispatch


@pytest.mark.asyncio
async def test_tracked_dispatch_registers_all_chunk_keys_before_first_send(monkeypatch) -> None:
    order: list[tuple[str, object]] = []
    organisation_id = uuid4()
    integration_id = uuid4()
    sync_id = uuid4()

    async def register(*_args, **kwargs):
        order.append(("register", kwargs["chunk_keys"]))

    async def send(_task_name, *, kwargs, queue):
        order.append(("send", (kwargs["initial_sync_chunk_key"], queue)))
        return uuid4()

    monkeypatch.setattr(
        dispatch.InitialSyncProgressService,
        "register_hydration",
        register,
    )
    monkeypatch.setattr(dispatch, "send_deal_task_async", send)

    await dispatch.dispatch_contact_communication_hydration_async(
        organisation_id=organisation_id,
        integration_id=integration_id,
        changed_email_ids=[uuid4() for _ in range(101)],
        trigger="email_sync",
        initial_sync_id=sync_id,
        initial_sync_branch="email",
        records_synced=101,
    )

    assert order == [
        ("register", ["email:0", "email:1"]),
        ("send", ("email:0", "deal")),
        ("send", ("email:1", "deal")),
    ]


@pytest.mark.asyncio
async def test_progress_registration_failure_still_dispatches_core_hydration(monkeypatch) -> None:
    """Optional visibility cannot suppress contact/account creation after provider commit."""
    sent_payloads: list[dict] = []

    async def fail_registration(*_args, **_kwargs):
        raise RuntimeError("progress row unavailable")

    async def send(_task_name, *, kwargs, queue):
        sent_payloads.append(kwargs)
        return uuid4()

    monkeypatch.setattr(
        dispatch.InitialSyncProgressService,
        "register_hydration",
        fail_registration,
    )
    monkeypatch.setattr(dispatch, "send_deal_task_async", send)

    task_ids = await dispatch.dispatch_contact_communication_hydration_async(
        organisation_id=uuid4(),
        integration_id=uuid4(),
        changed_email_ids=[uuid4()],
        trigger="email_sync",
        initial_sync_id=uuid4(),
        initial_sync_branch="email",
        records_synced=1,
    )

    assert len(task_ids) == 1
    assert "initial_sync_id" not in sent_payloads[0]
