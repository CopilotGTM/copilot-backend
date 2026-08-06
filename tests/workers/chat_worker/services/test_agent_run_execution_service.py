"""Worker orchestration contracts for durable agent runs."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import src.workers.chat_worker.services.agent_run_execution_service as execution_module
from src.agents.context import AgentContext
from src.service.automation.producers.meeting import MeetingEventContext
from src.service.automation.runs.runtime_executor import AgentExecutionResult
from src.workers.chat_worker.services.agent_run_execution_service import (
    AgentRunExecutionService,
    RuntimeAuthorityError,
)
from tests.service.automation.runs.test_runtime_executor import envelope


class FakeSession:
    """Represent the Fake Session test double used by the surrounding regression cases."""

    def __init__(self, events: list[str], label: str) -> None:
        self.events = events
        self.label = label

    async def commit(self) -> None:
        """Expose the commit operation expected by the Fake Session test double."""
        self.events.append(f"{self.label}:commit")

    async def rollback(self) -> None:
        """Expose the rollback operation expected by the Fake Session test double."""
        self.events.append(f"{self.label}:rollback")


class QueryResult:
    """Represent the Query Result test double used by the surrounding regression cases."""

    def __init__(self, *, row=None, scalar=None) -> None:
        self.row = row
        self.scalar = scalar

    def one_or_none(self):
        """Expose the one or none operation expected by the Query Result test double."""
        return self.row

    def scalar_one_or_none(self):
        """Expose the scalar one or none operation expected by the Query Result test double."""
        return self.scalar

    def scalars(self):
        """Expose SQLAlchemy's scalar-result adapter for model-select tests."""
        return self

    def first(self):
        """Return the configured first scalar without enforcing row uniqueness."""
        return self.scalar


class QuerySession:
    """Represent the Query Session test double used by the surrounding regression cases."""

    def __init__(self, *results: QueryResult) -> None:
        self.results = list(results)
        self.statements: list[object] = []

    async def execute(self, statement):
        """Expose the execute operation expected by the Query Session test double."""
        self.statements.append(statement)
        return self.results.pop(0)


def query_scope(session: QuerySession):
    """Build the query scope fixture used by the surrounding regression cases."""

    async def scope():
        """Build the scope fixture used by the surrounding regression cases."""
        yield session

    return scope()


class SessionScopes:
    """Represent the Session Scopes test double used by the surrounding regression cases."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.count = 0

    def __call__(self, _organisation_id):
        self.count += 1
        session = FakeSession(self.events, f"session{self.count}")

        async def scope():
            """Build the scope fixture used by the surrounding regression cases."""
            yield session

        return scope()


class FakeClaim:
    """Represent the Fake Claim test double used by the surrounding regression cases."""

    def __init__(self, item, events: list[str]) -> None:
        self.item = item
        self.events = events

    async def claim_run(self, _run_id, *, worker_id):
        """Expose the claim run operation expected by the Fake Claim test double."""
        self.events.append(f"claim:{worker_id}")
        return self.item


class FakeLifecycle:
    """Represent the Fake Lifecycle test double used by the surrounding regression cases."""

    def __init__(self, events: list[str], terminal_failure: Exception | None = None) -> None:
        self.events = events
        self.failure_kwargs: dict = {}
        self.terminal_failure = terminal_failure

    async def mark_succeeded(self, _run_id, **_kwargs):
        """Expose the mark succeeded operation expected by the Fake Lifecycle test double."""
        self.events.append("complete")
        if self.terminal_failure is not None:
            raise self.terminal_failure
        return True

    async def mark_failed(self, _run_id, **kwargs):
        """Expose the mark failed operation expected by the Fake Lifecycle test double."""
        self.events.append("failed")
        self.failure_kwargs = kwargs
        if self.terminal_failure is not None:
            raise self.terminal_failure
        return True

    async def release_for_retry(self, _run_id, **_kwargs):
        """Expose the release for retry operation expected by the Fake Lifecycle test double."""
        self.events.append("release")
        return True


class FakeExecutor:
    """Represent the Fake Executor test double used by the surrounding regression cases."""

    def __init__(self, events: list[str], failure: Exception | None = None) -> None:
        self.events = events
        self.failure = failure

    async def execute(self, _envelope, *, context):
        """Expose the execute operation expected by the Fake Executor test double."""
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


def build_service(
    events: list[str],
    *,
    failure: Exception | None = None,
    terminal_failure: Exception | None = None,
):
    """Build the build service fixture used by the surrounding regression cases."""
    item = envelope()
    scopes = SessionScopes(events)
    lifecycle = FakeLifecycle(events, terminal_failure=terminal_failure)

    async def context_loader(_envelope):
        """Build the context loader fixture used by the surrounding regression cases."""
        return AgentContext(
            organisation_id=item.organisation_id,
            deal_id=item.deal_id,
            subject_type=item.subject_type,
            subject_id=item.subject_id,
            user_id=item.principal_user_id,
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
    """Verify the contract that worker commits claim before slow execution then fenced result."""
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
async def test_terminal_finalization_failure_releases_the_fresh_claim_for_retry() -> None:
    """A failed terminal append must not strand the run behind the lease window."""
    events: list[str] = []
    service, item, _lifecycle = build_service(
        events,
        terminal_failure=RuntimeError("event append failed"),
    )

    result = await service.execute(item.run_id, worker_id="worker-terminal", retry_allowed=True)

    assert result["status"] == "retry"
    assert events == [
        "claim:worker-terminal",
        "session1:commit",
        "execute",
        "complete",
        "session2:rollback",
        "release",
        "session2:commit",
    ]


@pytest.mark.asyncio
async def test_validation_finalization_failure_releases_the_fresh_claim_for_retry() -> None:
    """The shared terminal append guard must also cover failed run outcomes."""
    events: list[str] = []
    service, item, _lifecycle = build_service(
        events,
        failure=ValueError("invalid output"),
        terminal_failure=RuntimeError("event append failed"),
    )

    result = await service.execute(item.run_id, worker_id="worker-terminal", retry_allowed=True)

    assert result["status"] == "retry"
    assert events == [
        "claim:worker-terminal",
        "session1:commit",
        "execute",
        "failed",
        "session2:rollback",
        "release",
        "session2:commit",
    ]


@pytest.mark.asyncio
async def test_retryable_failure_releases_claim_before_celery_retry() -> None:
    """Verify the contract that retryable failure releases claim before celery retry."""
    events: list[str] = []
    service, item, _lifecycle = build_service(events, failure=RuntimeError("provider down"))

    result = await service.execute(item.run_id, worker_id="worker-2", retry_allowed=True)

    assert result["status"] == "retry"
    assert events[-2:] == ["release", "session2:commit"]


@pytest.mark.asyncio
async def test_invalid_runtime_output_is_terminal_validation_failure() -> None:
    """Verify the contract that invalid runtime output is terminal validation failure."""
    events: list[str] = []
    service, item, lifecycle = build_service(events, failure=ValueError("bad JSON"))

    result = await service.execute(item.run_id, worker_id="worker-3", retry_allowed=True)

    assert result["status"] == "validation_failed"
    assert events[-2:] == ["failed", "session2:commit"]
    assert lifecycle.failure_kwargs["error_code"] == "invalid_agent_output"


@pytest.mark.asyncio
async def test_revoked_runtime_authority_is_not_reported_as_bad_agent_output() -> None:
    """Verify the contract that revoked runtime authority is not reported as bad agent output."""
    events: list[str] = []
    service, item, lifecycle = build_service(
        events,
        failure=RuntimeAuthorityError("principal is suspended"),
    )

    result = await service.execute(item.run_id, worker_id="worker-4", retry_allowed=True)

    assert result["status"] == "validation_failed"
    assert lifecycle.failure_kwargs["error_code"] == "runtime_authority_invalid"


@pytest.mark.asyncio
async def test_default_context_loader_revalidates_live_authority(monkeypatch) -> None:
    """Verify the contract that default context loader revalidates live authority."""
    item = envelope()
    master = QuerySession(
        QueryResult(row=(None, "Acme")),
        QueryResult(scalar="owner@example.com"),
    )
    tenant = QuerySession(
        QueryResult(scalar=item.agent_id),
        QueryResult(scalar=item.deal_id),
    )
    monkeypatch.setattr(
        execution_module,
        "acquire_master_async_session",
        lambda: query_scope(master),
    )
    service = AgentRunExecutionService(
        item.organisation_id,
        session_scope=lambda _organisation_id: query_scope(tenant),
    )

    context = await service._load_context(item)

    assert context.org_name == "Acme"
    assert context.user_id == item.principal_user_id
    assert context.user_email == "owner@example.com"
    assert context.deal_id == item.deal_id
    assert context.metadata == {
        "source": "agent_trigger",
        "run_id": str(item.run_id),
        "trigger_id": str(item.trigger_id),
        "automation_agent_id": str(item.agent_id),
        "automation_agent_revision": item.agent_revision,
        "automation_inference_run_id": str(item.run_id),
        "automation_evidence_id": str(item.event_id),
        "automation_caused_by_run_id": str(item.run_id),
        "automation_correlation_id": item.correlation_id,
        "automation_causation_depth": item.causation_depth + 1,
    }
    assert context.automation_causation() == {
        "caused_by_run_id": item.run_id,
        "causation_depth": item.causation_depth + 1,
        "correlation_id": item.correlation_id,
    }
    assert len(master.statements) == 2
    assert len(tenant.statements) == 2


@pytest.mark.asyncio
async def test_default_context_loader_rejects_revoked_principal(monkeypatch) -> None:
    """Verify the contract that default context loader rejects revoked principal."""
    item = envelope()
    master = QuerySession(
        QueryResult(row=("Copilot", "Acme")),
        QueryResult(scalar=None),
    )
    monkeypatch.setattr(
        execution_module,
        "acquire_master_async_session",
        lambda: query_scope(master),
    )
    service = AgentRunExecutionService(item.organisation_id)

    with pytest.raises(RuntimeAuthorityError, match="principal"):
        await service._load_context(item)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("subject_type", "table_name"),
    [
        ("email", "emails"),
        ("agent_run", "agent_runs"),
    ],
)
async def test_enabled_subjects_have_live_runtime_authority_policy(
    subject_type: str,
    table_name: str,
) -> None:
    """Verify the contract that enabled subjects have live runtime authority policy."""
    subject_id = uuid4()
    item = replace(envelope(), subject_type=subject_type, subject_id=str(subject_id))
    session = QuerySession(QueryResult(scalar=subject_id))

    await AgentRunExecutionService._ensure_subject_authority(session, item)

    assert table_name in str(session.statements[0]).lower()


@pytest.mark.asyncio
async def test_personal_meeting_authority_uses_the_event_owner(monkeypatch) -> None:
    """Runtime must authorize the organiser owner even when another mailbox synced the event."""
    item = envelope()
    meeting = SimpleNamespace(
        meeting_id=UUID(item.subject_id),
        organisation_id=item.organisation_id,
        calendar_event_id=uuid4(),
    )
    item = replace(item, subject_type="meeting", subject_id=str(meeting.meeting_id))
    session = QuerySession(QueryResult(scalar=meeting))
    load_context = AsyncMock(return_value=MeetingEventContext(owner_id=item.principal_user_id))
    monkeypatch.setattr(execution_module, "load_meeting_event_context", load_context, raising=False)

    await AgentRunExecutionService._ensure_subject_authority(session, item)

    load_context.assert_awaited_once_with(session, item.organisation_id, meeting)
    assert "meetings" in str(session.statements[0]).lower()


@pytest.mark.asyncio
async def test_personal_meeting_authority_rejects_a_different_event_owner(monkeypatch) -> None:
    """A mailbox integration owner must not override an active internal organiser owner."""
    item = envelope()
    meeting = SimpleNamespace(
        meeting_id=UUID(item.subject_id),
        organisation_id=item.organisation_id,
        calendar_event_id=uuid4(),
    )
    item = replace(item, subject_type="meeting", subject_id=str(meeting.meeting_id))
    session = QuerySession(QueryResult(scalar=meeting))
    monkeypatch.setattr(
        execution_module,
        "load_meeting_event_context",
        AsyncMock(return_value=MeetingEventContext(owner_id=uuid4())),
        raising=False,
    )

    with pytest.raises(RuntimeAuthorityError, match="principal"):
        await AgentRunExecutionService._ensure_subject_authority(session, item)


@pytest.mark.asyncio
async def test_personal_meeting_authority_fails_closed_when_event_context_is_gone(
    monkeypatch,
) -> None:
    """A deleted calendar event is revoked authority, not malformed model output."""
    item = envelope()
    meeting = SimpleNamespace(
        meeting_id=UUID(item.subject_id),
        organisation_id=item.organisation_id,
        calendar_event_id=uuid4(),
    )
    item = replace(item, subject_type="meeting", subject_id=str(meeting.meeting_id))
    session = QuerySession(QueryResult(scalar=meeting))
    monkeypatch.setattr(
        execution_module,
        "load_meeting_event_context",
        AsyncMock(side_effect=ValueError("Meeting calendar event is missing")),
        raising=False,
    )

    with pytest.raises(RuntimeAuthorityError, match="principal"):
        await AgentRunExecutionService._ensure_subject_authority(session, item)
