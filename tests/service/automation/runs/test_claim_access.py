"""SQL contract tests for atomically claiming durable agent runs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from src.service.automation.runs.access import AgentRunClaimAccess


class Result:
    def __init__(self, returned: object) -> None:
        self.returned = returned

    def scalar_one_or_none(self) -> object:
        return self.returned

    def scalars(self):
        return self

    def all(self):
        return self.returned


class RecordingSession:
    def __init__(self, returned: object = None) -> None:
        self.returned = returned
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> Result:
        self.statements.append(statement)
        return Result(self.returned)


def compile_statement(statement: Any) -> Any:
    return statement.compile(dialect=postgresql.dialect())


@pytest.mark.asyncio
async def test_claim_is_one_org_scoped_conditional_update() -> None:
    # A single conditional UPDATE is the concurrency boundary: duplicate workers
    # cannot both move the same pending or expired run into active execution.
    claimed_run = object()
    session = RecordingSession(returned=claimed_run)
    organisation_id = uuid4()
    run_id = uuid4()
    now = datetime(2026, 7, 14, 8, 30, tzinfo=UTC)
    stale_before = now - timedelta(minutes=30)
    access = AgentRunClaimAccess(session, organisation_id)  # type: ignore[arg-type]

    result = await access.claim_run(
        run_id,
        worker_id="agent-worker-3",
        claimed_at=now,
        stale_before=stale_before,
    )

    assert result is claimed_run
    assert len(session.statements) == 1
    compiled = compile_statement(session.statements[0])
    statement_sql = " ".join(str(compiled).lower().split())
    parameter_values = list(compiled.params.values())

    assert statement_sql.startswith("update agent_runs set")
    assert "agent_runs.organisation_id" in statement_sql
    assert "agent_runs.run_id" in statement_sql
    assert "agent_runs.status" in statement_sql
    assert "agent_runs.claimed_at is null" in statement_sql
    assert "agent_runs.claimed_at <" in statement_sql
    assert "attempt_count=(agent_runs.attempt_count +" in statement_sql
    assert "started_at=coalesce(agent_runs.started_at" in statement_sql
    assert "returning agent_runs.run_id" in statement_sql
    assert organisation_id in parameter_values
    assert run_id in parameter_values
    assert "pending" in parameter_values
    assert "running" in parameter_values
    assert "agent-worker-3" in parameter_values
    assert now in parameter_values
    assert stale_before in parameter_values
    assert 1 in parameter_values


@pytest.mark.asyncio
async def test_terminal_write_is_fenced_by_attempt_and_worker() -> None:
    # A stale worker must not overwrite a newer retry after its lease has been reclaimed.
    session = RecordingSession(returned=uuid4())
    organisation_id = uuid4()
    run_id = uuid4()
    completed_at = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    access = AgentRunClaimAccess(session, organisation_id)  # type: ignore[arg-type]

    updated = await access.complete_run(
        run_id,
        attempt_number=3,
        worker_id="agent-worker-3",
        status="succeeded",
        completed_at=completed_at,
        output_text="Next step",
        output_json=None,
        error_code=None,
        error_message=None,
        model_name="gpt-5-mini",
        token_usage={"total_tokens": 42},
        cost=None,
        latency_ms=1500,
    )

    assert updated is True
    compiled = compile_statement(session.statements[0])
    statement_sql = " ".join(str(compiled).lower().split())
    parameter_values = list(compiled.params.values())
    assert "agent_runs.organisation_id" in statement_sql
    assert "agent_runs.run_id" in statement_sql
    assert "agent_runs.status" in statement_sql
    assert "agent_runs.attempt_count" in statement_sql
    assert "agent_runs.claimed_by" in statement_sql
    assert organisation_id in parameter_values
    assert run_id in parameter_values
    assert 3 in parameter_values
    assert "agent-worker-3" in parameter_values
    assert "running" in parameter_values
    assert "succeeded" in parameter_values


@pytest.mark.asyncio
async def test_retry_release_is_fenced_and_returns_run_to_pending() -> None:
    session = RecordingSession(returned=uuid4())
    access = AgentRunClaimAccess(session, uuid4())  # type: ignore[arg-type]

    updated = await access.release_for_retry(
        uuid4(),
        attempt_number=2,
        worker_id="agent-worker-2",
        error_code="provider_timeout",
        error_message="Provider timed out",
    )

    assert updated is True
    compiled = compile_statement(session.statements[0])
    statement_sql = " ".join(str(compiled).lower().split())
    parameter_values = set(compiled.params.values())
    assert "claimed_at=" in statement_sql
    assert "claimed_by=" in statement_sql
    assert "pending" in parameter_values
    assert "running" in parameter_values


@pytest.mark.asyncio
async def test_dispatch_scan_returns_pending_and_expired_running_ids() -> None:
    run_ids = [uuid4(), uuid4()]
    session = RecordingSession(returned=run_ids)
    organisation_id = uuid4()
    stale_before = datetime(2026, 7, 14, 8, 30, tzinfo=UTC)
    access = AgentRunClaimAccess(session, organisation_id)  # type: ignore[arg-type]

    result = await access.list_dispatchable_run_ids(stale_before=stale_before, limit=100)

    assert result == run_ids
    compiled = compile_statement(session.statements[0])
    statement_sql = " ".join(str(compiled).lower().split())
    parameter_values = list(compiled.params.values())
    assert statement_sql.startswith("select agent_runs.run_id")
    assert "agent_runs.organisation_id" in statement_sql
    assert "agent_runs.status" in statement_sql
    assert "agent_runs.claimed_at <" in statement_sql
    assert "limit" in statement_sql
    assert organisation_id in parameter_values
    assert stale_before in parameter_values
    assert "pending" in parameter_values
    assert "running" in parameter_values
    assert 100 in parameter_values
