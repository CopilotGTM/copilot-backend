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


@pytest.mark.asyncio
async def test_untracked_broker_failure_preserves_original_dispatch_error(monkeypatch) -> None:
    """Failed registration means no chunk exists for the broker failure to terminalize."""
    failed_chunks: list[str] = []

    async def fail_registration(*_args, **_kwargs):
        raise RuntimeError("progress row unavailable")

    async def fail_send(*_args, **_kwargs):
        raise ConnectionError("broker unavailable")

    async def fail_chunk(_org_id, _sync_id, _branch, chunk_key, **_kwargs):
        failed_chunks.append(chunk_key)

    monkeypatch.setattr(
        dispatch.InitialSyncProgressService,
        "register_hydration",
        fail_registration,
    )
    monkeypatch.setattr(dispatch, "send_deal_task_async", fail_send)
    monkeypatch.setattr(
        dispatch.InitialSyncProgressService,
        "fail_hydration_chunk",
        fail_chunk,
    )

    with pytest.raises(ConnectionError, match="broker unavailable"):
        await dispatch.dispatch_contact_communication_hydration_async(
            organisation_id=uuid4(),
            integration_id=uuid4(),
            changed_email_ids=[uuid4()],
            trigger="email_sync",
            initial_sync_id=uuid4(),
            initial_sync_branch="email",
            records_synced=1,
        )

    assert failed_chunks == []


@pytest.mark.asyncio
async def test_tracked_chunk_failure_cannot_suppress_later_dispatches(monkeypatch) -> None:
    """Optional failure visibility must not stop independent replay-safe chunks."""
    attempted_chunks: list[str] = []

    async def register(*_args, **_kwargs):
        return None

    async def send(_task_name, *, kwargs, queue):
        attempted_chunks.append(kwargs["initial_sync_chunk_key"])
        if len(attempted_chunks) == 1:
            raise ConnectionError("broker rejected one chunk")
        assert queue == "deal"
        return uuid4()

    async def fail_chunk(*_args, **_kwargs):
        raise RuntimeError("progress store unavailable")

    monkeypatch.setattr(
        dispatch.InitialSyncProgressService,
        "register_hydration",
        register,
    )
    monkeypatch.setattr(dispatch, "send_deal_task_async", send)
    monkeypatch.setattr(
        dispatch.InitialSyncProgressService,
        "fail_hydration_chunk",
        fail_chunk,
    )

    task_ids = await dispatch.dispatch_contact_communication_hydration_async(
        organisation_id=uuid4(),
        integration_id=uuid4(),
        changed_email_ids=[uuid4() for _ in range(201)],
        trigger="email_sync",
        initial_sync_id=uuid4(),
        initial_sync_branch="email",
        records_synced=201,
    )

    assert attempted_chunks == ["email:0", "email:1", "email:2"]
    assert len(task_ids) == 2
