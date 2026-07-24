"""SQL-boundary tests for system-triggered CRM activation persistence."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from src.domain.models.organisation_tables import AgentRun
from src.service.automation.triggers.activation_access import TriggerActivationAccess


class ScalarRows:
    def first(self) -> None:
        return None

    def all(self) -> list[Any]:
        return []


class Result:
    def scalars(self) -> ScalarRows:
        return ScalarRows()

    def scalar_one_or_none(self) -> None:
        return None


class RecordingSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> Result:
        self.statements.append(statement)
        return Result()


def sql(statement: Any) -> str:
    return " ".join(str(statement.compile(dialect=postgresql.dialect())).lower().split())


@pytest.mark.asyncio
async def test_event_and_crm_entity_reads_repeat_organisation_boundary() -> None:
    session = RecordingSession()
    access = TriggerActivationAccess(session, uuid4())  # type: ignore[arg-type]

    await access.get_crm_event(uuid4())
    await access.get_crm_entity("deal", uuid4())
    await access.get_crm_entity("contact", uuid4())
    await access.get_crm_entity("account", uuid4())

    event_sql, deal_sql, contact_sql, account_sql = map(sql, session.statements)
    assert "crm_mutation_events.organisation_id" in event_sql
    assert "crm_mutation_events.event_id" in event_sql
    assert "crm_deals.organisation_id" in deal_sql
    assert "crm_deals.deal_id" in deal_sql
    assert "crm_contacts.organisation_id" in contact_sql
    assert "crm_contacts.contact_id" in contact_sql
    assert "crm_companies.organisation_id" in account_sql
    assert "crm_companies.company_id" in account_sql


@pytest.mark.asyncio
async def test_candidates_require_owner_event_active_trigger_and_active_agent() -> None:
    session = RecordingSession()
    access = TriggerActivationAccess(session, uuid4())  # type: ignore[arg-type]

    await access.list_crm_event_candidates(
        owner_user_id=uuid4(),
        source_domain="crm",
        event_type="crm.deal.stage_changed",
    )

    statement_sql = sql(session.statements[0])
    assert "join user_agents" in statement_sql
    assert "automation_triggers.organisation_id" in statement_sql
    assert "automation_triggers.owner_user_id" in statement_sql
    assert "automation_triggers.target_type" in statement_sql
    assert "automation_triggers.trigger_type" in statement_sql
    assert "automation_triggers.source_domain" in statement_sql
    assert "automation_triggers.event_type" in statement_sql
    assert "automation_triggers.status" in statement_sql
    assert "user_agents.organisation_id" in statement_sql
    assert "user_agents.owner_user_id" in statement_sql
    assert "user_agents.status" in statement_sql


@pytest.mark.asyncio
async def test_run_insert_is_replay_safe_on_existing_idempotency_index() -> None:
    session = RecordingSession()
    organisation_id = uuid4()
    access = TriggerActivationAccess(session, organisation_id)  # type: ignore[arg-type]
    agent_id = uuid4()
    run = AgentRun(
        organisation_id=organisation_id,
        owner_user_id=uuid4(),
        agent_id=agent_id,
        trigger_id=uuid4(),
        agent_revision=2,
        idempotency_key="trigger:abc:crm-event:def",
        source_domain="crm",
        event_type="crm.deal.stage_changed",
        source_event_id=str(uuid4()),
        trigger_snapshot={},
        agent_snapshot={"agent_id": str(agent_id), "revision": 2},
        input_snapshot={},
    )

    assert not await access.insert_run_if_absent(run)

    statement_sql = sql(session.statements[0])
    assert statement_sql.startswith("insert into agent_runs")
    assert "on conflict (organisation_id, idempotency_key)" in statement_sql
    assert "where idempotency_key is not null do nothing" in statement_sql
    assert "returning agent_runs.run_id" in statement_sql
