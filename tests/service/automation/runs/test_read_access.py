"""SQL-boundary checks for principal and resolved-audience run visibility."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from src.service.automation.runs.read_access import AgentRunReadAccess


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

    async def execute(self, statement: Any) -> Result:
        """Expose the execute operation expected by the Recording Session test double."""
        self.statements.append(statement)
        return Result()


def sql(statement: Any) -> str:
    """Build the sql fixture used by the surrounding regression cases."""
    return " ".join(str(statement.compile(dialect=postgresql.dialect())).lower().split())


@pytest.mark.asyncio
async def test_list_and_detail_require_principal_or_resolved_audience_visibility() -> None:
    """Verify the contract that list and detail require principal or resolved audience visibility."""
    session = RecordingSession()
    access = AgentRunReadAccess(session, uuid4(), uuid4())  # type: ignore[arg-type]

    await access.list_runs(
        agent_id=None,
        trigger_id=None,
        status=None,
        cursor_created_at=None,
        cursor_run_id=None,
        limit=21,
    )
    await access.get_run(uuid4())

    for statement in session.statements:
        statement_sql = sql(statement)
        assert "agent_runs.organisation_id" in statement_sql
        assert "agent_runs.execution_principal_type" in statement_sql
        assert "agent_runs.execution_principal_id" in statement_sql
        assert "agent_runs.audience_mode" in statement_sql
        assert "agent_runs.audience_config @>" in statement_sql
        assert "agent_runs.created_by_user_id =" not in statement_sql
