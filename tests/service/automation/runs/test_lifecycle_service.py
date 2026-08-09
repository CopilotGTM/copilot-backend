"""Fenced completion contracts for durable agent runs."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables import AgentRun, AgentRunStatus
from src.service.automation.runs.lifecycle_service import AgentRunLifecycleService


class FakeAccess:
    """Represent the Fake Access test double used by the surrounding regression cases."""

    def __init__(self, run: AgentRun | None = None, result: bool = True) -> None:
        self.run = run
        self.result = result
        self.delivery_id = uuid4()
        self.calls: list[tuple[str, UUID, dict[str, Any]]] = []

    async def complete_run(self, run_id: UUID, **kwargs: Any) -> AgentRun | None:
        """Expose the complete run operation expected by the Fake Access test double."""
        self.calls.append(("complete", run_id, kwargs))
        if not self.result:
            return None
        assert self.run is not None
        self.run.status = kwargs["status"]
        self.run.completed_at = kwargs["completed_at"]
        return self.run

    async def release_for_retry(self, run_id: UUID, **kwargs: Any) -> bool:
        """Expose the release for retry operation expected by the Fake Access test double."""
        self.calls.append(("retry", run_id, kwargs))
        return self.result

    async def record_trigger_outcome(self, trigger_id: UUID, **kwargs: Any) -> None:
        """Expose the record trigger outcome operation expected by the Fake Access test double."""
        self.calls.append(("trigger_outcome", trigger_id, kwargs))

    async def ensure_deliveries(self, run: AgentRun) -> tuple[UUID, ...]:
        self.calls.append(("ensure_deliveries", run.run_id, {"delivery_ids": [self.delivery_id]}))
        return (self.delivery_id,)


class FakeEvents:
    """Represent the Fake Events test double used by the surrounding regression cases."""

    def __init__(self, access: FakeAccess) -> None:
        self.access = access
        self.envelopes = []

    async def enqueue(self, envelope):  # noqa: ANN001
        """Expose the enqueue operation expected by the Fake Events test double."""
        assert self.access.calls[0][0] == "complete"
        self.envelopes.append(envelope)
        return object()


def agent_run(organisation_id: UUID) -> AgentRun:
    """Build the agent run fixture used by the surrounding regression cases."""
    return AgentRun(
        organisation_id=organisation_id,
        created_by_user_id=uuid4(),
        execution_principal_type="user",
        execution_principal_id=uuid4(),
        audience_mode="principal",
        audience_config={},
        agent_id=uuid4(),
        trigger_id=uuid4(),
        agent_revision=2,
        status=AgentRunStatus.RUNNING,
        idempotency_key=f"run:{uuid4()}",
        event_id=uuid4(),
        event_key="crm.deal.updated",
        correlation_id="chain-22",
        causation_depth=0,
        trigger_snapshot={},
        agent_snapshot={},
        input_snapshot={},
    )


@pytest.mark.asyncio
async def test_success_persists_result_metadata_under_claim_fence() -> None:
    """Verify the contract that success persists result metadata under claim fence."""
    organisation_id = uuid4()
    run = agent_run(organisation_id)
    access = FakeAccess(run)
    events = FakeEvents(access)
    run_id = uuid4()
    run.run_id = run_id
    completed_at = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    service = AgentRunLifecycleService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        access=access,
        automation_events=events,
    )

    completed = await service.mark_succeeded(
        run_id,
        attempt_number=2,
        worker_id="agent-worker-2",
        completed_at=completed_at,
        output_text="Next step",
        output_json={"next_step": "Call buyer"},
        model_name="gpt-5-mini",
        token_usage={"total_tokens": 42},
        cost=Decimal("0.01"),
        latency_ms=1200,
    )

    assert completed.updated is True
    assert completed.delivery_ids == ()
    assert access.calls == [
        (
            "complete",
            run_id,
            {
                "attempt_number": 2,
                "worker_id": "agent-worker-2",
                "status": AgentRunStatus.SUCCEEDED.value,
                "completed_at": completed_at,
                "output_text": "Next step",
                "output_json": {"next_step": "Call buyer"},
                "error_code": None,
                "error_message": None,
                "model_name": "gpt-5-mini",
                "token_usage": {"total_tokens": 42},
                "cost": Decimal("0.01"),
                "latency_ms": 1200,
            },
        ),
        (
            "trigger_outcome",
            run.trigger_id,
            {
                "succeeded": True,
                "completed_at": completed_at,
                "error_code": None,
                "error_message": None,
            },
        ),
    ]
    assert [event.event_key for event in events.envelopes] == ["agent.run.completed"]


@pytest.mark.asyncio
async def test_success_creates_platform_email_notification_from_the_pinned_run_policy() -> None:
    organisation_id = uuid4()
    run = agent_run(organisation_id)
    run.output_delivery_config = {"channels": ["email_notification"]}
    access = FakeAccess(run)
    service = AgentRunLifecycleService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        access=access,
        automation_events=FakeEvents(access),
    )

    completed = await service.mark_succeeded(
        run.run_id,
        attempt_number=1,
        worker_id="agent-worker-1",
        output_text="Call the buyer",
        output_json=None,
        model_name="gpt-5",
        token_usage=None,
        cost=None,
        latency_ms=10,
    )

    assert completed.updated is True
    assert completed.delivery_ids == (access.delivery_id,)
    assert [call[0] for call in access.calls] == [
        "complete",
        "ensure_deliveries",
        "trigger_outcome",
    ]


@pytest.mark.asyncio
async def test_validation_failure_is_terminal_and_sanitized() -> None:
    """Verify the contract that validation failure is terminal and sanitized."""
    organisation_id = uuid4()
    run = agent_run(organisation_id)
    access = FakeAccess(run)
    events = FakeEvents(access)
    run_id = uuid4()
    run.run_id = run_id
    service = AgentRunLifecycleService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        access=access,
        automation_events=events,
    )

    await service.mark_failed(
        run_id,
        attempt_number=1,
        worker_id="agent-worker-1",
        error_code="invalid_snapshot",
        error_message="Pinned input is invalid",
        validation_failure=True,
        completed_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
        latency_ms=10,
    )

    assert access.calls[0][2]["status"] == AgentRunStatus.VALIDATION_FAILED.value
    assert access.calls[0][2]["error_code"] == "invalid_snapshot"
    assert access.calls[1][0] == "trigger_outcome"
    assert access.calls[1][2]["succeeded"] is False
    assert [event.event_key for event in events.envelopes] == ["agent.run.failed"]


@pytest.mark.asyncio
async def test_failed_run_does_not_create_email_delivery() -> None:
    organisation_id = uuid4()
    run = agent_run(organisation_id)
    run.output_delivery_config = {"channels": ["email_notification"]}
    access = FakeAccess(run)
    service = AgentRunLifecycleService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        access=access,
        automation_events=FakeEvents(access),
    )

    await service.mark_failed(
        run.run_id,
        attempt_number=1,
        worker_id="agent-worker-1",
        error_code="agent_execution_failed",
        error_message="Agent execution failed",
        validation_failure=False,
        latency_ms=10,
    )

    assert "ensure_deliveries" not in [call[0] for call in access.calls]


@pytest.mark.asyncio
async def test_retry_release_preserves_attempt_fence() -> None:
    """Verify the contract that retry release preserves attempt fence."""
    access = FakeAccess()
    run_id = uuid4()
    service = AgentRunLifecycleService(object(), uuid4(), access=access)  # type: ignore[arg-type]

    await service.release_for_retry(
        run_id,
        attempt_number=4,
        worker_id="agent-worker-4",
        error_code="provider_timeout",
        error_message="Provider timed out",
    )

    assert access.calls == [
        (
            "retry",
            run_id,
            {
                "attempt_number": 4,
                "worker_id": "agent-worker-4",
                "error_code": "provider_timeout",
                "error_message": "Provider timed out",
            },
        )
    ]


@pytest.mark.asyncio
async def test_lost_completion_fence_emits_no_terminal_event() -> None:
    """Verify the contract that lost completion fence emits no terminal event."""
    organisation_id = uuid4()
    access = FakeAccess(agent_run(organisation_id), result=False)
    events = FakeEvents(access)
    service = AgentRunLifecycleService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        access=access,
        automation_events=events,
    )

    updated = await service.mark_failed(
        uuid4(),
        attempt_number=1,
        worker_id="stale-worker",
        error_code="agent_execution_failed",
        error_message="sanitized",
        validation_failure=False,
        latency_ms=10,
        completed_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
    )

    assert updated is False
    assert events.envelopes == []
