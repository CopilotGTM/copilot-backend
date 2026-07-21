"""Fenced completion contracts for durable agent runs."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables import AgentRunStatus
from src.service.automation.runs.lifecycle_service import AgentRunLifecycleService


class FakeAccess:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls: list[tuple[str, UUID, dict[str, Any]]] = []

    async def complete_run(self, run_id: UUID, **kwargs: Any) -> bool:
        self.calls.append(("complete", run_id, kwargs))
        return self.result

    async def release_for_retry(self, run_id: UUID, **kwargs: Any) -> bool:
        self.calls.append(("retry", run_id, kwargs))
        return self.result


@pytest.mark.asyncio
async def test_success_persists_result_metadata_under_claim_fence() -> None:
    access = FakeAccess()
    run_id = uuid4()
    completed_at = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    service = AgentRunLifecycleService(object(), uuid4(), access=access)  # type: ignore[arg-type]

    updated = await service.mark_succeeded(
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

    assert updated is True
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
        )
    ]


@pytest.mark.asyncio
async def test_validation_failure_is_terminal_and_sanitized() -> None:
    access = FakeAccess()
    run_id = uuid4()
    service = AgentRunLifecycleService(object(), uuid4(), access=access)  # type: ignore[arg-type]

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


@pytest.mark.asyncio
async def test_retry_release_preserves_attempt_fence() -> None:
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
