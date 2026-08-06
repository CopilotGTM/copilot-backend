"""Regression tests for test event dispatch; these cases lock the backend contract used by the changed feature."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from src.domain.models.organisation_tables import AutomationEventDispatch
from src.service.automation.events.access import AutomationEventDispatchAccess


class Result:
    """Represent the Result test double used by the surrounding regression cases."""

    def scalar_one_or_none(self) -> None:
        """Expose the scalar one or none operation expected by the Result test double."""
        return None

    def scalars(self) -> Result:
        """Expose the scalars operation expected by the Result test double."""
        return self

    def first(self) -> None:
        """Expose the first operation expected by the Result test double."""
        return None

    def all(self) -> list[Any]:
        """Expose the all operation expected by the Result test double."""
        return []


class RecordingSession:
    """Represent the Recording Session test double used by the surrounding regression cases."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> Result:
        """Expose the execute operation expected by the Recording Session test double."""
        self.statements.append(statement)
        return Result()


def sql(statement: Any) -> str:
    """Build the sql fixture used by the surrounding regression cases."""
    return " ".join(str(statement.compile(dialect=postgresql.dialect())).lower().split())


def dispatch() -> AutomationEventDispatch:
    """Build the dispatch fixture used by the surrounding regression cases."""
    return AutomationEventDispatch(
        organisation_id=uuid4(),
        event_key="crm.contact.created",
        route_mode="match",
        idempotency_key="crm.contact.created:event-1",
        envelope={},
    )


@pytest.mark.asyncio
async def test_insert_is_replay_safe_on_tenant_idempotency_identity() -> None:
    """Verify the contract that insert is replay safe on tenant idempotency identity."""
    row = dispatch()
    session = RecordingSession()
    access = AutomationEventDispatchAccess(session, row.organisation_id)  # type: ignore[arg-type]

    assert not await access.insert_if_absent(row)

    statement_sql = sql(session.statements[0])
    assert statement_sql.startswith("insert into automation_event_dispatches")
    assert "on conflict (organisation_id, idempotency_key) do nothing" in statement_sql
    assert "returning automation_event_dispatches.event_id" in statement_sql


@pytest.mark.asyncio
async def test_reads_and_terminal_updates_repeat_the_organisation_boundary() -> None:
    """Verify the contract that reads and terminal updates repeat the organisation boundary."""
    session = RecordingSession()
    access = AutomationEventDispatchAccess(session, uuid4())  # type: ignore[arg-type]
    event_id = uuid4()

    await access.get_by_idempotency_key("event-key")
    await access.get(event_id)
    await access.mark_processed(
        event_id,
        candidate_count=2,
        matched_count=1,
        activation_outcomes=[{"status": "created"}],
    )
    await access.record_failure(
        event_id,
        error_code="invalid_envelope",
        error="broken",
        terminal=True,
    )

    lookup_sql, get_sql, processed_sql, failure_sql = map(sql, session.statements)
    assert "automation_event_dispatches.organisation_id" in lookup_sql
    assert "automation_event_dispatches.idempotency_key" in lookup_sql
    for statement_sql in (get_sql, processed_sql, failure_sql):
        assert "automation_event_dispatches.organisation_id" in statement_sql
        assert "automation_event_dispatches.event_id" in statement_sql
    assert "candidate_count" in processed_sql
    assert "matched_count" in processed_sql
    assert "activation_outcomes" in processed_sql
    assert "last_error_code" in failure_sql


@pytest.mark.asyncio
async def test_pending_recovery_is_tenant_scoped_bounded_and_ignores_terminal_rows() -> None:
    """Verify the contract that pending recovery is tenant scoped bounded and ignores terminal rows."""
    session = RecordingSession()
    access = AutomationEventDispatchAccess(session, uuid4())  # type: ignore[arg-type]
    cutoff = datetime(2026, 8, 5, 10, tzinfo=UTC)

    await access.list_pending_before(cutoff, limit=100)

    statement_sql = sql(session.statements[0])
    assert "automation_event_dispatches.organisation_id" in statement_sql
    assert "automation_event_dispatches.created_at <=" in statement_sql
    assert "automation_event_dispatches.processed_at is null" in statement_sql
    assert "automation_event_dispatches.failed_at is null" in statement_sql
    assert "order by automation_event_dispatches.created_at" in statement_sql
    assert "limit" in statement_sql
