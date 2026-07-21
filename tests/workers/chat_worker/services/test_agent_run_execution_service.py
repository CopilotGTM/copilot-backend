"""Worker orchestration contracts for durable agent runs."""

from __future__ import annotations

import pytest
from src.agents.context import AgentContext
from src.service.automation.runs.runtime_executor import AgentExecutionResult
from src.workers.chat_worker.services.agent_run_execution_service import (
    AgentRunExecutionService,
)
from tests.service.automation.runs.test_runtime_executor import envelope


class FakeSession:
    def __init__(self, events: list[str], label: str) -> None:
        self.events = events
        self.label = label

    async def commit(self) -> None:
        self.events.append(f"{self.label}:commit")

    async def rollback(self) -> None:
        self.events.append(f"{self.label}:rollback")


class SessionScopes:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.count = 0

    def __call__(self, _organisation_id):
        self.count += 1
        session = FakeSession(self.events, f"session{self.count}")

        async def scope():
            yield session

        return scope()


class FakeClaim:
    def __init__(self, item, events: list[str]) -> None:
        self.item = item
        self.events = events

    async def claim_run(self, _run_id, *, worker_id):
        self.events.append(f"claim:{worker_id}")
        return self.item


class FakeLifecycle:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def mark_succeeded(self, _run_id, **_kwargs):
        self.events.append("complete")
        return True

    async def mark_failed(self, _run_id, **_kwargs):
        self.events.append("failed")
        return True

    async def release_for_retry(self, _run_id, **_kwargs):
        self.events.append("release")
        return True


class FakeExecutor:
    def __init__(self, events: list[str], failure: Exception | None = None) -> None:
        self.events = events
        self.failure = failure

    async def execute(self, _envelope, *, context):
        self.events.append("execute")
        if self.failure:
            raise self.failure
        return AgentExecutionResult(
            output_text="Call buyer",
            output_json=None,
            model_name="gpt-5-mini",
            token_usage={"total_tokens": 10},
            cost=None,
            latency_ms=50,
        )


def build_service(events: list[str], *, failure: Exception | None = None):
    item = envelope()
    scopes = SessionScopes(events)
    lifecycle = FakeLifecycle(events)

    async def context_loader(_envelope):
        return AgentContext(
            organisation_id=item.organisation_id,
            deal_id=item.deal_id,
            user_id=item.owner_user_id,
        )

    service = AgentRunExecutionService(
        item.organisation_id,
        session_scope=scopes,
        claim_factory=lambda _session, _org: FakeClaim(item, events),
        lifecycle_factory=lambda _session, _org: lifecycle,
        executor=FakeExecutor(events, failure),
        context_loader=context_loader,
    )
    return service, item, lifecycle


@pytest.mark.asyncio
async def test_worker_commits_claim_before_slow_execution_then_fenced_result() -> None:
    events: list[str] = []
    service, item, _lifecycle = build_service(events)

    result = await service.execute(item.run_id, worker_id="worker-1", retry_allowed=True)

    assert result["status"] == "succeeded"
    assert events == [
        "claim:worker-1",
        "session1:commit",
        "execute",
        "complete",
        "session2:commit",
    ]


@pytest.mark.asyncio
async def test_retryable_failure_releases_claim_before_celery_retry() -> None:
    events: list[str] = []
    service, item, _lifecycle = build_service(events, failure=RuntimeError("provider down"))

    result = await service.execute(item.run_id, worker_id="worker-2", retry_allowed=True)

    assert result["status"] == "retry"
    assert events[-2:] == ["release", "session2:commit"]


@pytest.mark.asyncio
async def test_invalid_runtime_output_is_terminal_validation_failure() -> None:
    events: list[str] = []
    service, item, _lifecycle = build_service(events, failure=ValueError("bad JSON"))

    result = await service.execute(item.run_id, worker_id="worker-3", retry_allowed=True)

    assert result["status"] == "validation_failed"
    assert events[-2:] == ["failed", "session2:commit"]
