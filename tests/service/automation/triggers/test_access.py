"""SQL-boundary tests for owner-scoped automation trigger persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from src.service.automation.triggers.access import AutomationTriggerAccess


class ScalarRows:
    def all(self) -> list[Any]:
        return []

    def first(self) -> None:
        return None


class Result:
    def scalars(self) -> ScalarRows:
        return ScalarRows()


class RecordingSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def scalar(self, statement: Any) -> int:
        self.statements.append(statement)
        return 0

    async def execute(self, statement: Any) -> Result:
        self.statements.append(statement)
        return Result()


def sql(statement: Any) -> str:
    return " ".join(str(statement.compile(dialect=postgresql.dialect())).lower().split())


def assert_trigger_boundary(statement_sql: str) -> None:
    assert "automation_triggers.organisation_id" in statement_sql
    assert "automation_triggers.owner_user_id" in statement_sql
    assert "automation_triggers.target_type" in statement_sql
    assert "automation_triggers.target_id" in statement_sql


@pytest.mark.asyncio
async def test_list_and_get_repeat_owner_target_and_archive_boundaries() -> None:
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
