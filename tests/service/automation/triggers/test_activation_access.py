"""Regression tests for test activation; these cases lock the backend contract used by the changed feature."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from src.domain.models.organisation_tables import (
    AgentRun,
    AutomationAudienceMode,
    AutomationPrincipalType,
)
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.domain.models.organisation_tables.sync.email_models import Email
from src.domain.models.organisation_tables.sync.meeting_intelligence import Meeting
from src.service.automation.producers.meeting import MeetingEventContext
from src.service.automation.triggers.activation_access import TriggerActivationAccess


class ScalarRows:
    """Represent the Scalar Rows test double used by the surrounding regression cases."""

    def first(self) -> None:
        """Build the first fixture used by the surrounding regression cases."""
        return None

    def all(self) -> list[Any]:
        """Build the all fixture used by the surrounding regression cases."""
        return []


class Result:
    """Represent the Result test double used by the surrounding regression cases."""

    rowcount = 1

    def scalars(self) -> ScalarRows:
        """Expose the scalars operation expected by the Result test double."""
        return ScalarRows()

    def scalar_one_or_none(self) -> None:
        """Expose the scalar one or none operation expected by the Result test double."""
        return None

    def all(self) -> list[Any]:
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


@pytest.mark.asyncio
async def test_candidate_queries_use_event_array_direct_identity_and_active_status() -> None:
    """Verify the contract that candidate queries use event array direct identity and active status."""
    session = RecordingSession()
    access = TriggerActivationAccess(session, uuid4())  # type: ignore[arg-type]

    await access.list_event_candidates("crm.deal.updated")
    await access.get_direct_candidate(uuid4())

    event_sql, direct_sql = map(sql, session.statements)
    assert "automation_triggers.organisation_id" in event_sql
    assert "automation_triggers.match_event_keys" in event_sql
    assert "@>" in event_sql
    assert "automation_triggers.status" in event_sql
    assert "automation_triggers.family =" in event_sql
    assert "automation_triggers.trigger_id" in direct_sql
    assert "automation_triggers.status" in direct_sql


@pytest.mark.asyncio
async def test_readiness_and_causation_queries_are_tenant_scoped() -> None:
    """Verify the contract that readiness and causation queries are tenant scoped."""
    session = RecordingSession()
    access = TriggerActivationAccess(session, uuid4())  # type: ignore[arg-type]

    await access.list_active_agent_delivery_configs({uuid4(), uuid4()})
    await access.list_active_user_ids({uuid4()})
    await access.list_causation_trigger_ids(uuid4())

    agent_sql, user_sql, causation_sql = map(sql, session.statements)
    assert "user_agents.organisation_id" in agent_sql
    assert "user_agents.status" in agent_sql
    assert "user_agents.output_delivery_config" in agent_sql
    assert "public.users.organisation_id" in user_sql
    assert "public.users.status" in user_sql
    assert "public.users.admin_status" in user_sql
    assert "agent_runs.organisation_id" in causation_sql
    assert "with recursive" in causation_sql


@pytest.mark.asyncio
async def test_crm_principal_identity_query_is_tenant_scoped_and_active_only() -> None:
    session = RecordingSession()
    access = TriggerActivationAccess(session, uuid4())  # type: ignore[arg-type]

    assert await access.list_crm_user_ids_by_principal({uuid4()}) == {}

    statement = sql(session.statements[0])
    assert "public.users.organisation_id" in statement
    assert "public.users.status" in statement
    assert "public.users.admin_status" in statement
    assert "crm_users.organisation_id" in statement
    assert "crm_users.is_deleted is false" in statement


@pytest.mark.asyncio
async def test_run_insert_and_replay_lookup_use_trigger_event_uniqueness() -> None:
    """Verify the contract that run insert and replay lookup use trigger event uniqueness."""
    session = RecordingSession()
    organisation_id = uuid4()
    access = TriggerActivationAccess(session, organisation_id)  # type: ignore[arg-type]
    agent_id, trigger_id, event_id = uuid4(), uuid4(), uuid4()
    run = AgentRun(
        organisation_id=organisation_id,
        created_by_user_id=uuid4(),
        execution_principal_type=AutomationPrincipalType.USER,
        execution_principal_id=uuid4(),
        audience_mode=AutomationAudienceMode.PRINCIPAL,
        agent_id=agent_id,
        trigger_id=trigger_id,
        agent_revision=2,
        idempotency_key=f"trigger:{trigger_id}:event:{event_id}",
        event_id=event_id,
        event_key="crm.deal.updated",
        correlation_id=str(event_id),
        trigger_snapshot={},
        agent_snapshot={"agent_id": str(agent_id), "revision": 2},
        input_snapshot={},
    )

    assert not await access.insert_run_if_absent(run)
    await access.get_run(trigger_id=trigger_id, event_id=event_id)

    insert_sql, lookup_sql = map(sql, session.statements)
    assert "on conflict (organisation_id, trigger_id, event_id) do nothing" in insert_sql
    assert "returning agent_runs.run_id" in insert_sql
    assert "agent_runs.organisation_id" in lookup_sql
    assert "agent_runs.trigger_id" in lookup_sql
    assert "agent_runs.event_id" in lookup_sql


@pytest.mark.asyncio
async def test_schema_drift_pause_is_tenant_scoped_and_active_only() -> None:
    """Verify the contract that schema drift pause is tenant scoped and active only."""
    session = RecordingSession()
    access = TriggerActivationAccess(session, uuid4())  # type: ignore[arg-type]

    assert await access.pause_invalid_trigger(
        uuid4(),
        error_code="binding_contract_invalid",
        message="Saved trigger no longer matches the current automation catalog.",
    )

    statement = sql(session.statements[0])
    assert "update automation_triggers set" in statement
    assert "automation_triggers.organisation_id" in statement
    assert "automation_triggers.trigger_id" in statement
    assert "automation_triggers.status =" in statement


@pytest.mark.asyncio
async def test_email_subject_participants_resolve_active_tenant_users() -> None:
    """Verify the contract that email subject participants resolve active tenant users."""
    organisation_id = uuid4()
    integration = KnowledgeIntegration(
        organisation_id=organisation_id,
        created_by_user_id=uuid4(),
        integration_type=IntegrationType.OUTLOOK_EMAIL,
        name="Mailbox",
        credentials={},
    )
    email = Email(
        organisation_id=organisation_id,
        integration_id=integration.integration_id,
        folder_id=uuid4(),
        external_message_id="message-1",
        from_email="sender@example.com",
        to_emails=["owner@example.com"],
    )
    participant_ids = [integration.created_by_user_id, uuid4()]

    class ValueResult:
        """Represent the Value Result test double used by the surrounding regression cases."""

        def __init__(self, *, first=None, all_values=None):
            self.first_value = first
            self.all_values = all_values or []

        def scalars(self):
            """Expose the scalars operation expected by the Value Result test double."""
            return self

        def first(self):
            """Expose the first operation expected by the Value Result test double."""
            return self.first_value

        def all(self):
            """Expose the all operation expected by the Value Result test double."""
            return self.all_values

    class SequenceSession:
        """Represent the Sequence Session test double used by the surrounding regression cases."""

        def __init__(self):
            self.results = iter(
                [
                    ValueResult(first=email),
                    ValueResult(first=integration),
                    ValueResult(all_values=participant_ids),
                ]
            )
            self.statements = []

        async def execute(self, statement):
            """Expose the execute operation expected by the Sequence Session test double."""
            self.statements.append(statement)
            return next(self.results)

    session = SequenceSession()
    access = TriggerActivationAccess(session, organisation_id)  # type: ignore[arg-type]

    resolved = await access.list_subject_participant_user_ids("email", email.email_id)

    assert resolved == set(participant_ids)
    assert all("organisation_id" in sql(statement) for statement in session.statements)


@pytest.mark.asyncio
async def test_meeting_subject_participants_reuse_canonical_context(
    monkeypatch,
) -> None:
    """Verify the contract that meeting subject participants reuse canonical context."""
    from src.service.automation.triggers import activation_access as module

    organisation_id = uuid4()
    meeting = Meeting(organisation_id=organisation_id, calendar_event_id=uuid4())
    owner_id, attendee_id = uuid4(), uuid4()

    class MeetingResult:
        """Represent the Meeting Result test double used by the surrounding regression cases."""

        def scalars(self):
            """Expose the scalars operation expected by the Meeting Result test double."""
            return self

        def first(self):
            """Expose the first operation expected by the Meeting Result test double."""
            return meeting

    class MeetingSession:
        """Represent the Meeting Session test double used by the surrounding regression cases."""

        async def execute(self, _statement):
            """Expose the execute operation expected by the Meeting Session test double."""
            return MeetingResult()

    context_loader = AsyncMock(
        return_value=MeetingEventContext(
            owner_id=owner_id,
            attendee_ids=(attendee_id,),
        )
    )
    monkeypatch.setattr(module, "load_meeting_event_context", context_loader)
    session = MeetingSession()
    access = TriggerActivationAccess(session, organisation_id)  # type: ignore[arg-type]

    resolved = await access.list_subject_participant_user_ids(
        "meeting",
        meeting.meeting_id,
    )

    assert resolved == {owner_id, attendee_id}
    context_loader.assert_awaited_once_with(session, organisation_id, meeting)
