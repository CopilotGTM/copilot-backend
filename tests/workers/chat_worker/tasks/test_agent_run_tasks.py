"""Celery registration and thin-task contracts for agent execution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.workers.chat_worker.config import ChatWorkerConfig


def test_agent_run_tasks_have_explicit_routes_and_recovery_schedule() -> None:
    config = ChatWorkerConfig()

    assert config.task_routes["chat.execute_agent_run"]["queue"] == "chat_messages"
    assert config.task_routes["chat.recover_agent_runs"]["queue"] == "chat_maintenance"
    assert config.task_routes["chat.deliver_agent_run"]["queue"] == "chat_messages"
    assert config.task_routes["chat.recover_agent_run_deliveries"]["queue"] == "chat_maintenance"
    assert config.beat_schedule["recover-agent-runs"]["task"] == "chat.recover_agent_runs"
    assert config.beat_schedule["recover-agent-run-deliveries"]["task"] == (
        "chat.recover_agent_run_deliveries"
    )


def test_execute_task_delegates_only_org_and_run_identity(monkeypatch) -> None:
    from src.workers.chat_worker.tasks import agent_run_tasks

    calls = []

    class Service:
        def __init__(self, organisation_id) -> None:
            calls.append(("init", organisation_id))

        async def execute(self, run_id, *, worker_id, retry_allowed):
            calls.append(("execute", run_id, worker_id, retry_allowed))
            return {"status": "succeeded", "run_id": str(run_id)}

    monkeypatch.setattr(agent_run_tasks, "AgentRunExecutionService", Service)
    monkeypatch.setattr(agent_run_tasks, "run_async", lambda awaitable: _resolve(awaitable))
    organisation_id = uuid4()
    run_id = uuid4()

    result = agent_run_tasks.execute_agent_run_task.run(
        organisation_id=str(organisation_id),
        run_id=str(run_id),
    )

    assert result == {"status": "succeeded", "run_id": str(run_id)}
    assert calls[0] == ("init", organisation_id)
    assert calls[1][0:2] == ("execute", run_id)


def test_delivery_task_delegates_only_org_and_delivery_identity(monkeypatch) -> None:
    from src.workers.chat_worker.tasks import agent_run_tasks

    calls = []

    class Service:
        def __init__(self, organisation_id) -> None:
            calls.append(("init", organisation_id))

        async def execute(self, delivery_id, *, worker_id, retry_allowed):
            calls.append(("execute", delivery_id, worker_id, retry_allowed))
            return {"status": "sent", "delivery_id": str(delivery_id)}

    monkeypatch.setattr(agent_run_tasks, "AgentRunDeliveryExecutionService", Service)
    monkeypatch.setattr(agent_run_tasks, "run_async", lambda awaitable: _resolve(awaitable))
    organisation_id = uuid4()
    delivery_id = uuid4()

    result = agent_run_tasks.deliver_agent_run_task.run(
        organisation_id=str(organisation_id),
        delivery_id=str(delivery_id),
    )

    assert result == {"status": "sent", "delivery_id": str(delivery_id)}
    assert calls[0] == ("init", organisation_id)
    assert calls[1][0:2] == ("execute", delivery_id)


def test_delivery_retry_delay_is_bounded_exponential_with_jitter(monkeypatch) -> None:
    from src.workers.chat_worker.services import agent_run_delivery_service
    from src.workers.chat_worker.tasks import agent_run_tasks

    delay = getattr(agent_run_tasks, "_delivery_retry_delay_seconds", None)
    assert callable(delay), "delivery retry delay helper is missing"
    monkeypatch.setattr(
        agent_run_delivery_service.secrets,
        "randbelow",
        lambda upper: upper - 1,
    )

    assert [delay(attempt) for attempt in (1, 2, 3, 4, 20)] == [37, 75, 150, 300, 300]


def _resolve(awaitable):
    import asyncio

    return asyncio.run(awaitable)


@pytest.mark.asyncio
async def test_recovery_publishes_committed_run_identity(monkeypatch) -> None:
    from src.workers.chat_worker.tasks import agent_run_tasks

    organisation_id = uuid4()
    run_id = uuid4()
    published = []

    class Rows:
        def scalars(self):
            return self

        def all(self):
            return [organisation_id]

    class MasterSession:
        async def execute(self, _statement):
            return Rows()

    async def master_scope():
        yield MasterSession()

    async def org_scope(_organisation_id):
        yield object()

    class Access:
        def __init__(self, _session, _organisation_id) -> None:
            return None

        async def list_dispatchable_run_ids(self, **_kwargs):
            return [run_id]

    async def publish(task_name, **kwargs):
        published.append((task_name, kwargs))
        return "task-id"

    monkeypatch.setattr(agent_run_tasks, "acquire_master_async_session", master_scope)
    monkeypatch.setattr(agent_run_tasks, "acquire_org_async_session", org_scope)
    monkeypatch.setattr(agent_run_tasks, "AgentRunClaimAccess", Access)
    monkeypatch.setattr(agent_run_tasks, "send_chat_task_async", publish)

    result = await agent_run_tasks._recover_agent_runs()

    assert result["dispatched"] == 1
    assert published == [
        (
            "chat.execute_agent_run",
            {
                "kwargs": {
                    "organisation_id": str(organisation_id),
                    "run_id": str(run_id),
                },
                "queue": "chat_messages",
                "routing_key": "chat_messages",
            },
        )
    ]


@pytest.mark.asyncio
async def test_delivery_recovery_publishes_committed_delivery_identity(monkeypatch) -> None:
    from src.workers.chat_worker.tasks import agent_run_tasks

    organisation_id = uuid4()
    run_id = uuid4()
    skipped_run_id = uuid4()
    delivery_id = uuid4()
    skipped_delivery_id = uuid4()
    published = []
    metrics = []
    ages = []

    class Rows:
        def scalars(self):
            return self

        def all(self):
            return [organisation_id]

    class MasterSession:
        async def execute(self, _statement):
            return Rows()

    async def master_scope():
        yield MasterSession()

    async def org_scope(_organisation_id):
        yield object()

    class Access:
        def __init__(self, _session, _organisation_id) -> None:
            return None

        async def list_dispatchable(self, **_kwargs):
            return [
                SimpleNamespace(
                    delivery_id=delivery_id,
                    run_id=run_id,
                    status="sending",
                    created_at=datetime.now(UTC) - timedelta(minutes=30),
                    claimed_at=datetime.now(UTC) - timedelta(minutes=15),
                ),
                SimpleNamespace(
                    delivery_id=skipped_delivery_id,
                    run_id=skipped_run_id,
                    status="pending",
                    created_at=datetime.now(UTC) - timedelta(minutes=5),
                    claimed_at=None,
                ),
            ]

    async def publish(task_name, **kwargs):
        published.append((task_name, kwargs))
        if kwargs["kwargs"]["delivery_id"] == str(skipped_delivery_id):
            raise RuntimeError("broker unavailable")
        return "task-id"

    monkeypatch.setattr(agent_run_tasks, "acquire_master_async_session", master_scope)
    monkeypatch.setattr(agent_run_tasks, "acquire_org_async_session", org_scope)
    monkeypatch.setattr(agent_run_tasks, "AgentRunDeliveryAccess", Access)
    monkeypatch.setattr(agent_run_tasks, "send_chat_task_async", publish)
    monkeypatch.setattr(agent_run_tasks.logger, "exception", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        agent_run_tasks,
        "record_agent_run_delivery_recovery",
        lambda outcome, **values: metrics.append((outcome, values)),
        raising=False,
    )
    monkeypatch.setattr(
        agent_run_tasks,
        "record_agent_run_delivery_recovery_age",
        lambda status, age_seconds: ages.append((status, age_seconds)),
        raising=False,
    )

    result = await agent_run_tasks._recover_agent_run_deliveries()

    assert result["dispatched"] == 1
    assert result["failed"] == 1
    assert result["skipped"] == 1
    assert result["reclaimed"] == 1
    assert metrics == [
        ("scanned", {"rows": 2}),
        ("reclaimed", {}),
        ("dispatched", {}),
        ("skipped", {}),
    ]
    assert [status for status, _age in ages] == ["sending", "pending"]
    assert ages[0][1] >= 15 * 60
    assert ages[1][1] >= 5 * 60
    assert published[0][0] == "chat.deliver_agent_run"
    assert published[0][1]["kwargs"] == {
        "organisation_id": str(organisation_id),
        "delivery_id": str(delivery_id),
    }
