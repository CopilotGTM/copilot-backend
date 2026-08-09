"""SQL-boundary tests for private agent-definition persistence."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from src.domain.models.organisation_tables import AgentOutputMode, AgentStatus
from src.service.agents.access import AgentDefinitionAccess


class _ScalarRows:
    def __init__(self, rows: Sequence[Any]) -> None:
        self.rows = list(rows)

    def all(self) -> list[Any]:
        return self.rows

    def first(self) -> Any | None:
        return self.rows[0] if self.rows else None


class _Result:
    def __init__(self, rows: Sequence[Any] = ()) -> None:
        self.rows = rows

    def scalars(self) -> _ScalarRows:
        return _ScalarRows(self.rows)


class _RecordingSession:
    def __init__(self, *, scalar_value: int = 0, execute_rows: Sequence[Any] = ()) -> None:
        self.scalar_value = scalar_value
        self.execute_rows = execute_rows
        self.statements: list[Any] = []

    async def scalar(self, statement: Any) -> int:
        self.statements.append(statement)
        return self.scalar_value

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(self.execute_rows)


def _sql(statement: Any) -> str:
    return " ".join(str(statement.compile(dialect=postgresql.dialect())).lower().split())


def _assert_owner_boundary(sql: str) -> None:
    # A resource UUID is not authorization; both authenticated boundaries must stay in SQL.
    assert "user_agents.organisation_id" in sql
    assert "user_agents.owner_user_id" in sql


@pytest.mark.asyncio
async def test_list_agents_scopes_count_and_rows_and_hides_archived_definitions() -> None:
    session = _RecordingSession(scalar_value=0)
    access = AgentDefinitionAccess(session, uuid4(), uuid4())  # type: ignore[arg-type]

    rows, total_count = await access.list_agents(status=None, limit=20, offset=40)

    assert rows == []
    assert total_count == 0
    assert len(session.statements) == 2
    for statement in session.statements:
        sql = _sql(statement)
        _assert_owner_boundary(sql)
        assert "user_agents.status !=" in sql
    list_sql = _sql(session.statements[1])
    assert "order by user_agents.updated_at desc, user_agents.agent_id desc" in list_sql
    assert "limit" in list_sql and "offset" in list_sql


@pytest.mark.asyncio
async def test_get_agent_scopes_identifier_and_excludes_archived_definitions() -> None:
    session = _RecordingSession()
    access = AgentDefinitionAccess(session, uuid4(), uuid4())  # type: ignore[arg-type]

    assert await access.get_agent(uuid4()) is None

    sql = _sql(session.statements[0])
    _assert_owner_boundary(sql)
    assert "user_agents.agent_id" in sql
    assert "user_agents.status !=" in sql


@pytest.mark.asyncio
async def test_replace_agent_is_an_atomic_owner_scoped_revision_update() -> None:
    session = _RecordingSession()
    access = AgentDefinitionAccess(session, uuid4(), uuid4())  # type: ignore[arg-type]

    result = await access.replace_agent(
        uuid4(),
        expected_revision=7,
        values={
            "name": "Pipeline Coach",
            "description": None,
            "prompt": "Rank open deals.",
            "model_name": None,
            "model_config": {},
            "allowed_tools": [],
            "allowed_skills": [],
            "max_turns": None,
            "input_schema": None,
            "output_mode": AgentOutputMode.TEXT.value,
            "output_schema": None,
            "status": AgentStatus.ACTIVE.value,
        },
    )

    assert result is None
    sql = _sql(session.statements[0])
    assert sql.startswith("update user_agents set")
    _assert_owner_boundary(sql)
    assert "user_agents.agent_id" in sql
    assert "user_agents.revision =" in sql
    assert "user_agents.status !=" in sql
    assert "revision=(user_agents.revision +" in sql
    assert "returning" in sql
    assert "delete from" not in sql


@pytest.mark.asyncio
async def test_archive_agent_is_a_soft_delete_with_the_same_revision_boundary() -> None:
    session = _RecordingSession()
    access = AgentDefinitionAccess(session, uuid4(), uuid4())  # type: ignore[arg-type]

    assert await access.archive_agent(uuid4(), expected_revision=3) is None

    statement = session.statements[0]
    sql = _sql(statement)
    assert sql.startswith("update user_agents set")
    _assert_owner_boundary(sql)
    assert "user_agents.revision =" in sql
    assert "user_agents.status !=" in sql
    assert "delete from" not in sql
    assert (
        AgentStatus.ARCHIVED.value
        in statement.compile(dialect=postgresql.dialect()).params.values()
    )


@pytest.mark.asyncio
async def test_replace_output_delivery_uses_its_own_owner_scoped_concurrency_token() -> None:
    session = _RecordingSession()
    access = AgentDefinitionAccess(session, uuid4(), uuid4())  # type: ignore[arg-type]
    expected_updated_at = datetime(2026, 8, 9, 10, 30, tzinfo=UTC)

    result = await access.replace_output_delivery(
        uuid4(),
        expected_updated_at=expected_updated_at,
        channels=["email_notification"],
    )

    assert result is None
    sql = _sql(session.statements[0])
    assert sql.startswith("update user_agents set")
    _assert_owner_boundary(sql)
    assert "user_agents.output_delivery_updated_at =" in sql
    assert "user_agents.status !=" in sql
    assert "output_delivery_config=" in sql
    assert "revision=(user_agents.revision +" not in sql
