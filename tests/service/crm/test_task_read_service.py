"""Focused query-contract tests for canonical CRM task reads."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from src.service.crm.tasks.read_service import TaskReadService


class _EmptyResult:
    """Represent an empty SQLAlchemy result without requiring a database."""

    def all(self) -> list:
        """Return no task rows so the test only inspects the generated query."""
        return []


class _FakeSession:
    """Capture count and row queries emitted by TaskReadService."""

    def __init__(self) -> None:
        self.scalar_statements: list[object] = []
        self.executed_statements: list[object] = []

    async def scalar(self, statement):
        """Capture the count query and report an empty result set."""
        self.scalar_statements.append(statement)
        return 0

    async def execute(self, statement):
        """Capture the row query and return no rows."""
        self.executed_statements.append(statement)
        return _EmptyResult()


@pytest.mark.asyncio
async def test_today_filter_keeps_timed_and_local_date_semantics_separate() -> None:
    """All-day dates compare locally while timed tasks use UTC day bounds."""
    session = _FakeSession()
    service = TaskReadService(session, uuid4())

    await service.list_tasks(
        scope="assigned_to_me",
        current_user_id=uuid4(),
        due_from=datetime(2026, 7, 27, 18, 30, tzinfo=UTC),
        due_to=datetime(2026, 7, 28, 18, 29, 59, 999999, tzinfo=UTC),
        due_local_date=date(2026, 7, 28),
    )

    statement_sql = str(session.executed_statements[0])
    assert "crm_tasks.due_at IS NOT NULL" in statement_sql
    assert "crm_tasks.due_at >=" in statement_sql
    assert "crm_tasks.due_at <=" in statement_sql
    assert "crm_tasks.due_at IS NULL" in statement_sql
    assert "crm_tasks.due_date =" in statement_sql
