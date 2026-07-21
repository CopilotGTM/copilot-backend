"""Celery registration and thin-task contracts for agent execution."""

from __future__ import annotations

from uuid import uuid4

import pytest
from src.workers.chat_worker.config import ChatWorkerConfig


def test_agent_run_tasks_have_explicit_routes_and_recovery_schedule() -> None:
    config = ChatWorkerConfig()

    assert config.task_routes["chat.execute_agent_run"]["queue"] == "chat_messages"
    assert config.task_routes["chat.recover_agent_runs"]["queue"] == "chat_processing"
    assert config.beat_schedule["recover-agent-runs"]["task"] == "chat.recover_agent_runs"


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
