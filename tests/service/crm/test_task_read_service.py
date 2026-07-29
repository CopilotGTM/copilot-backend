"""Focused query-contract tests for canonical CRM task reads."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from src.domain.models.master_tables.user_models import UserAppRole
from src.service.crm.access.record_access_policy import CRMAccessActor
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


def test_personal_assignment_visibility_does_not_require_deal_relationship() -> None:
    """An assigned task remains visible even when its deal is outside the user's relationship set."""
    actor = CRMAccessActor(uuid4(), UserAppRole.MEMBER, "seller@example.com")
    service = TaskReadService(_FakeSession(), uuid4(), access_actor=actor)

    visibility_sql = str(service._visible_task_condition())

    # Assignment is its own read grant inside the organisation. Restricting the
    # personal branch to standalone tasks would silently omit legitimate work.
    assert "crm_tasks.deal_id IS NULL" not in visibility_sql


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


@pytest.mark.asyncio
async def test_overdue_filter_keeps_due_today_all_day_tasks_current() -> None:
    """The overdue query must compare date-only tasks after, not during, their due date."""
    session = _FakeSession()
    service = TaskReadService(session, uuid4())

    await service.list_tasks(
        scope="assigned_to_me",
        current_user_id=uuid4(),
        is_overdue=True,
        user_timezone="Asia/Kolkata",
    )

    statement_sql = str(session.executed_statements[0])
    assert "crm_tasks.due_at IS NOT NULL" in statement_sql
    assert "crm_tasks.due_at <" in statement_sql
    assert "crm_tasks.due_at IS NULL" in statement_sql
    assert "crm_tasks.due_date <" in statement_sql
    assert "crm_tasks.status NOT IN" in statement_sql
