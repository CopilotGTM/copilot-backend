"""SQL-boundary tests for owner-scoped automation trigger persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from src.service.automation.triggers.access import AutomationTriggerAccess


class ScalarRows:
    """Represent the Scalar Rows test double used by the surrounding regression cases."""

    def all(self) -> list[Any]:
        """Build the all fixture used by the surrounding regression cases."""
        return []

    def first(self) -> None:
        """Build the first fixture used by the surrounding regression cases."""
        return None


class Result:
    """Represent the Result test double used by the surrounding regression cases."""

    def scalars(self) -> ScalarRows:
        """Expose the scalars operation expected by the Result test double."""
        return ScalarRows()


class RecordingSession:
    """Represent the Recording Session test double used by the surrounding regression cases."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def scalar(self, statement: Any) -> int:
        """Expose the scalar operation expected by the Recording Session test double."""
        self.statements.append(statement)
        return 0

    async def execute(self, statement: Any) -> Result:
        """Expose the execute operation expected by the Recording Session test double."""
        self.statements.append(statement)
        return Result()


def sql(statement: Any) -> str:
    """Build the sql fixture used by the surrounding regression cases."""
    return " ".join(str(statement.compile(dialect=postgresql.dialect())).lower().split())


def assert_trigger_boundary(statement_sql: str) -> None:
    """Build the assert trigger boundary fixture used by the surrounding regression cases."""
    assert "automation_triggers.organisation_id" in statement_sql
    assert "automation_triggers.created_by_user_id" in statement_sql
    assert "automation_triggers.target_type" in statement_sql
    assert "automation_triggers.target_id" in statement_sql


@pytest.mark.asyncio
async def test_list_and_get_repeat_owner_target_and_archive_boundaries() -> None:
    """Verify the contract that list and get repeat owner target and archive boundaries."""
    session = RecordingSession()
    access = AutomationTriggerAccess(session, uuid4(), uuid4())  # type: ignore[arg-type]
    agent_id = uuid4()

    await access.list_triggers(agent_id, status=None, limit=20, offset=0)
    await access.get_trigger(agent_id, uuid4())

    for statement in session.statements:
        statement_sql = sql(statement)
        assert_trigger_boundary(statement_sql)
        assert "automation_triggers.status !=" in statement_sql


@pytest.mark.asyncio
async def test_replace_and_archive_are_conditional_updates_never_deletes() -> None:
    """Verify the contract that replace and archive are conditional updates never deletes."""
    session = RecordingSession()
    access = AutomationTriggerAccess(session, uuid4(), uuid4())  # type: ignore[arg-type]
    agent_id, trigger_id = uuid4(), uuid4()
    expected = datetime.now(UTC)

    await access.replace_trigger(
        agent_id,
        trigger_id,
        expected_updated_at=expected,
        values={"name": "Updated"},
    )
    await access.archive_trigger(agent_id, trigger_id, expected_updated_at=expected)

    for statement in session.statements:
        statement_sql = sql(statement)
        assert statement_sql.startswith("update automation_triggers set")
        assert_trigger_boundary(statement_sql)
        assert "automation_triggers.trigger_id" in statement_sql
        assert "automation_triggers.updated_at =" in statement_sql
        assert "automation_triggers.status !=" in statement_sql
        assert "returning" in statement_sql
        assert "delete from" not in statement_sql


@pytest.mark.asyncio
async def test_admin_can_manage_shared_organisation_bindings_and_targets() -> None:
    """Verify the contract that admin can manage shared organisation bindings and targets."""
    session = RecordingSession()
    access = AutomationTriggerAccess(  # type: ignore[arg-type]
        session,
        uuid4(),
        uuid4(),
        is_admin=True,
    )

    await access.get_agent(uuid4())
    await access.get_trigger(uuid4(), uuid4())

    agent_sql = sql(session.statements[0].whereclause)
    trigger_sql = sql(session.statements[1])
    assert "user_agents.owner_user_id" not in agent_sql
    assert "automation_triggers.created_by_user_id" in trigger_sql
    assert "automation_triggers.execution_principal_type" in trigger_sql
    assert " or " in trigger_sql
