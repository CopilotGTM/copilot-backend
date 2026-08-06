"""Orchestration contracts for durable event activation and run publication."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables import AutomationEventDispatch
from src.service.automation.events.contracts import AutomationEventEnvelope
from src.service.automation.triggers.activation import (
    TriggerActivationOutcome,
    TriggerActivationResult,
)
from src.workers.chat_worker.services.automation_event_activation_service import (
    AutomationEventActivationExecutionService,
)


class Session:
    """Record transaction ordering without opening a database connection."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def commit(self) -> None:
        """Record the durable commit boundary observed by the publisher."""

        self.events.append("commit")

    async def rollback(self) -> None:
        """Record cleanup before activation failure bookkeeping starts."""

        self.events.append("rollback")


class Access:
    """Provide the dispatch transitions used by activation orchestration tests."""

    def __init__(self, dispatch: AutomationEventDispatch, events: list[str]) -> None:
        self.dispatch = dispatch
        self.events = events
        self.activation_outcomes: list[dict] = []

    async def get(self, event_id):
        """Resolve only the dispatch created by this fixture."""

        return self.dispatch if event_id == self.dispatch.event_id else None

    async def mark_processed(
        self,
        event_id,
        *,
        candidate_count,
        matched_count,
        activation_outcomes,
    ) -> bool:
        """Record terminal activation before the surrounding commit."""

        self.events.append("mark_processed")
        self.dispatch.processed_at = SimpleNamespace()
        assert candidate_count >= matched_count
        assert isinstance(activation_outcomes, list)
        self.activation_outcomes = activation_outcomes
        return event_id == self.dispatch.event_id

    async def record_failure(self, event_id, *, error_code, error, terminal) -> bool:
        """Capture whether retry exhaustion made the dispatch terminal."""

        self.events.append(f"failure:{terminal}:{error_code}:{error}")
        return event_id == self.dispatch.event_id


class Activation:
    """Return or raise the trigger-matching outcome selected by each test."""

    def __init__(self, result: TriggerActivationResult | Exception) -> None:
        self.result = result

    async def activate(self, _envelope):
        """Simulate trigger evaluation without database dependencies."""

        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def service_fixture(
    result: TriggerActivationResult | Exception,
    *,
    publish_failures_before_success: int = 0,
):
    """Build an activation service whose transaction and publish order is observable."""

    organisation_id = uuid4()
    event_id = uuid4()
    subject_id = uuid4()
    envelope = AutomationEventEnvelope.model_validate(
        {
            "event_id": event_id,
            "organisation_id": organisation_id,
            "event_key": "crm.contact.created",
            "route": {"mode": "match"},
            "occurred_at": datetime(2026, 8, 5, tzinfo=UTC),
            "source": {
                "domain": "crm",
                "system": "internal",
                "operation": "create",
                "mode": "interactive",
                "source_event_id": str(event_id),
            },
            "actor": {"type": "user", "id": str(uuid4())},
            "subject": {"type": "contact", "id": str(subject_id)},
            "record": {"id": str(subject_id)},
            "correlation_id": "request-123",
            "idempotency_key": f"crm.contact.created:{event_id}",
        }
    )
    dispatch = AutomationEventDispatch(
        event_id=event_id,
        organisation_id=organisation_id,
        event_key=envelope.event_key,
        route_mode="match",
        idempotency_key=envelope.idempotency_key,
        envelope=envelope.model_dump(mode="json"),
    )
    events: list[str] = []
    session = Session(events)
    access = Access(dispatch, events)

    async def session_scope(_organisation_id) -> AsyncIterator[Session]:
        """Yield the single transaction recorder owned by this fixture."""

        yield session

    published: list[tuple[str, dict]] = []
    publish_attempts = 0

    async def publish(task_name: str, **kwargs):
        """Fail the configured attempts before recording broker acceptance."""

        nonlocal publish_attempts
        publish_attempts += 1
        events.append("publish")
        if publish_attempts <= publish_failures_before_success:
            raise RuntimeError("redis unavailable")
        published.append((task_name, kwargs))

    service = AutomationEventActivationExecutionService(
        organisation_id,
        session_scope=session_scope,  # type: ignore[arg-type]
        access_factory=lambda _session, _org: access,
        activation_factory=lambda _session, _org: Activation(result),
        publisher=publish,
        publish_retry_delays=(0.0, 0.0, 0.0),
    )
    return service, dispatch, events, published, access


@pytest.mark.asyncio
async def test_activation_commits_runs_before_broker_publication() -> None:
    """A worker can recover a committed run if Redis publication fails afterward."""

    run_id = uuid4()
    result = TriggerActivationResult(
        event_id=uuid4(),
        candidate_count=1,
        matched_count=1,
        created_run_ids=(run_id,),
        outcomes=(TriggerActivationOutcome(uuid4(), "created", run_id=run_id),),
    )
    service, dispatch, events, published, access = service_fixture(result)

    outcome = await service.activate(dispatch.event_id, retry_allowed=True)

    assert outcome == {
        "status": "processed",
        "created_runs": 1,
        "published_runs": 1,
        "publish_failures": 0,
    }
    assert events == ["mark_processed", "commit", "publish"]
    assert published[0][0] == "chat.execute_agent_run"
    assert published[0][1]["kwargs"]["run_id"] == str(run_id)
    assert access.activation_outcomes == [
        {
            "trigger_id": str(result.outcomes[0].trigger_id),
            "matched": True,
            "status": "created",
            "reason": None,
            "run_id": str(run_id),
            "correlation_id": "request-123",
            "latency_ms": access.activation_outcomes[0]["latency_ms"],
        }
    ]
    assert access.activation_outcomes[0]["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_agent_run_publication_retries_before_reporting_failure() -> None:
    """A transient broker failure must not wait for future polling to execute the run."""

    run_id = uuid4()
    service, dispatch, events, published, _access = service_fixture(
        TriggerActivationResult(
            event_id=uuid4(),
            candidate_count=1,
            matched_count=1,
            created_run_ids=(run_id,),
        ),
        publish_failures_before_success=2,
    )

    outcome = await service.activate(dispatch.event_id, retry_allowed=True)

    assert outcome["published_runs"] == 1
    assert outcome["publish_failures"] == 0
    assert events == ["mark_processed", "commit", "publish", "publish", "publish"]
    assert published[0][1]["kwargs"]["run_id"] == str(run_id)


@pytest.mark.asyncio
async def test_zero_matches_is_processed_and_not_recovered_forever() -> None:
    """An event with no configured trigger is a terminal successful activation."""

    service, dispatch, events, published, _access = service_fixture(
        TriggerActivationResult(event_id=uuid4(), candidate_count=0, matched_count=0)
    )

    outcome = await service.activate(dispatch.event_id, retry_allowed=True)

    assert outcome["status"] == "processed"
    assert outcome["created_runs"] == 0
    assert events == ["mark_processed", "commit"]
    assert published == []


@pytest.mark.asyncio
async def test_activation_failure_is_recorded_in_a_fresh_transaction() -> None:
    """Retry state survives even when the activation transaction itself fails."""

    service, dispatch, events, _published, _access = service_fixture(RuntimeError("broken source"))

    outcome = await service.activate(dispatch.event_id, retry_allowed=True)

    assert outcome == {"status": "retry", "event_id": str(dispatch.event_id)}
    assert events == [
        "rollback",
        "failure:False:activation_failed:Automation event activation failed",
        "commit",
    ]


@pytest.mark.asyncio
async def test_processed_duplicate_delivery_is_a_noop() -> None:
    """Recovery and broker redelivery cannot republish a completed dispatch."""

    service, dispatch, events, published, _access = service_fixture(
        TriggerActivationResult(event_id=uuid4(), candidate_count=0, matched_count=0)
    )
    dispatch.processed_at = SimpleNamespace()

    outcome = await service.activate(dispatch.event_id, retry_allowed=True)

    assert outcome == {"status": "already_processed", "event_id": str(dispatch.event_id)}
    assert events == []
    assert published == []
