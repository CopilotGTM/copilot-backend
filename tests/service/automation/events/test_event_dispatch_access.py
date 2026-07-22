"""SQL-boundary tests for automation event dispatch persistence."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from src.domain.models.organisation_tables import AutomationEventDispatch
from src.service.automation.events.access import AutomationEventDispatchAccess


class Result:
    def __init__(self, values: list[Any] | None = None) -> None:
        self.values = values or []

    def scalar_one_or_none(self) -> None:
        return None

    def scalars(self) -> Result:
        return self

    def first(self) -> None:
        return None

    def all(self) -> list[Any]:
        return self.values


class RecordingSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> Result:
        self.statements.append(statement)
        return Result()


def sql(statement: Any) -> str:
    return " ".join(str(statement.compile(dialect=postgresql.dialect())).lower().split())


@pytest.mark.asyncio
async def test_insert_is_replay_safe_on_source_identity() -> None:
    """Concurrent producer retries cannot create duplicate dispatch rows."""

    session = RecordingSession()
    organisation_id = uuid4()
    access = AutomationEventDispatchAccess(session, organisation_id)  # type: ignore[arg-type]
    dispatch = AutomationEventDispatch(
        organisation_id=organisation_id,
        source_domain="crm",
        source_event_id=str(uuid4()),
        event_type="crm.contact.created",
    )

    assert not await access.insert_if_absent(dispatch)

    statement_sql = sql(session.statements[0])
    assert statement_sql.startswith("insert into automation_event_dispatches")
    assert (
        "on conflict (organisation_id, source_domain, source_event_id, event_type)" in statement_sql
    )
    assert "do nothing" in statement_sql


@pytest.mark.asyncio
async def test_source_lookup_repeats_organisation_boundary() -> None:
    """A provider event ID alone must never cross an organization schema boundary."""

    session = RecordingSession()
    access = AutomationEventDispatchAccess(session, uuid4())  # type: ignore[arg-type]

    await access.get_by_source(
        source_domain="crm",
        source_event_id=str(uuid4()),
        event_type="crm.contact.created",
    )

    statement_sql = sql(session.statements[0])
    assert "automation_event_dispatches.organisation_id" in statement_sql
    assert "automation_event_dispatches.source_domain" in statement_sql
    assert "automation_event_dispatches.source_event_id" in statement_sql
    assert "automation_event_dispatches.event_type" in statement_sql


@pytest.mark.asyncio
async def test_terminal_updates_are_org_scoped() -> None:
    """A task delivery cannot update a dispatch from another organization."""

    session = RecordingSession()
    access = AutomationEventDispatchAccess(session, uuid4())  # type: ignore[arg-type]
    dispatch_id = uuid4()

    await access.mark_processed(dispatch_id)
    await access.record_failure(dispatch_id, error="broken", terminal=True)

    processed_sql, failure_sql = map(sql, session.statements)
    for statement_sql in (processed_sql, failure_sql):
        assert "automation_event_dispatches.organisation_id" in statement_sql
        assert "automation_event_dispatches.dispatch_id" in statement_sql
        assert "processed_at is null" in statement_sql
        assert "failed_at is null" in statement_sql
