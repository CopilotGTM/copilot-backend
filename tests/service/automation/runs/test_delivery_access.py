"""Fenced SQL transitions for independent agent-run deliveries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from src.service.automation.runs.delivery_access import AgentRunDeliveryAccess


class Result:
    def scalar_one_or_none(self):
        return uuid4()


class RecordingSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> Result:
        self.statements.append(statement)
        return Result()


def parameters(statement: Any) -> dict[str, Any]:
    return statement.compile(dialect=postgresql.dialect()).params


@pytest.mark.asyncio
async def test_claim_is_scoped_to_exact_delivery_identity() -> None:
    delivery_id = uuid4()
    run_id = uuid4()
    delivery = SimpleNamespace(delivery_id=delivery_id, source_id=run_id)
    run = SimpleNamespace(run_id=run_id)

    class ClaimResult:
        def __init__(self, value) -> None:
            self.value = value

        def scalar_one_or_none(self):
            return self.value

        def scalars(self):
            return self

        def first(self):
            return self.value

    class ClaimSession:
        def __init__(self) -> None:
            self.statements = []
            self.results = [ClaimResult(delivery), ClaimResult(run)]

        async def execute(self, statement):
            self.statements.append(statement)
            return self.results.pop(0)

    session = ClaimSession()
    claimed = await AgentRunDeliveryAccess(  # type: ignore[arg-type]
        session,
        uuid4(),
    ).claim(
        delivery_id,
        worker_id="delivery-worker",
        claimed_at=datetime.now(UTC),
        stale_before=datetime.now(UTC) - timedelta(minutes=10),
    )

    assert claimed == (delivery, run)
    values = parameters(session.statements[0])
    assert delivery_id in values.values()
    assert run_id not in values.values()
    assert "agent_output_ready" in values.values()
    assert "agent_run" in values.values()


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["sent", "failed"])
async def test_terminal_delivery_transitions_clear_claim_metadata(transition: str) -> None:
    session = RecordingSession()
    access = AgentRunDeliveryAccess(session, uuid4())  # type: ignore[arg-type]
    delivery_id = uuid4()
    claim_token = uuid4()

    if transition == "sent":
        updated = await access.mark_sent(
            delivery_id,
            claim_token=claim_token,
            provider_resource_id="message-1",
            sent_at=datetime.now(UTC),
        )
    else:
        updated = await access.mark_terminal(
            delivery_id,
            claim_token=claim_token,
            status="FAILED",
            error_code="provider_rejected",
            error_message="Provider rejected the request",
        )

    values = parameters(session.statements[0])
    assert updated is True
    assert values["claimed_at"] is None
    assert values["claimed_by"] is None
    assert values["claim_token"] is None
    assert values["next_attempt_at"] is None


@pytest.mark.asyncio
async def test_repair_retry_is_fenced_by_tenant_run_status_and_exact_error() -> None:
    session = RecordingSession()
    organisation_id = uuid4()
    access = AgentRunDeliveryAccess(session, organisation_id)  # type: ignore[arg-type]
    run_id = uuid4()
    delivery_id = uuid4()

    updated = await access.requeue_repairable_delivery(
        run_id,
        delivery_id,
        expected_error_code="email_reauth_required",
    )

    statement = session.statements[0]
    sql = str(statement.compile(dialect=postgresql.dialect())).lower()
    values = parameters(statement)
    assert updated is not None
    assert "organisation_id" in sql and "source_id" in sql
    assert "NEEDS_ATTENTION" in values.values()
    assert "email_reauth_required" in values.values()
    assert delivery_id in values.values()
    assert values["status"] == "PENDING"
    assert values["error_code"] is None
    assert values["last_error"] is None


@pytest.mark.asyncio
async def test_recovery_candidates_include_only_health_metadata_needed_by_the_sweep() -> None:
    now = datetime.now(UTC)
    delivery_id = uuid4()
    run_id = uuid4()

    class CandidateResult:
        def all(self):
            return [
                SimpleNamespace(
                    delivery_id=delivery_id,
                    source_id=run_id,
                    status="SENDING",
                    created_at=now - timedelta(minutes=20),
                    claimed_at=now - timedelta(minutes=15),
                )
            ]

    class CandidateSession:
        def __init__(self) -> None:
            self.statement = None

        async def execute(self, statement):
            self.statement = statement
            return CandidateResult()

    session = CandidateSession()
    candidates = await AgentRunDeliveryAccess(  # type: ignore[arg-type]
        session,
        uuid4(),
    ).list_dispatchable(
        stale_before=now - timedelta(minutes=10),
        now=now,
        limit=100,
    )

    assert candidates[0].delivery_id == delivery_id
    assert candidates[0].run_id == run_id
    assert candidates[0].status == "SENDING"
    sql = str(session.statement.compile(dialect=postgresql.dialect())).lower()
    assert "created_at" in sql and "claimed_at" in sql
    assert "agent_output_ready" in parameters(session.statement).values()
    assert "trigger_snapshot" not in sql and "output_text" not in sql
