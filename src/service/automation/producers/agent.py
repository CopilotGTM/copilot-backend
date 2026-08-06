"""Canonical lifecycle events from fenced terminal agent-run rows."""

from __future__ import annotations

from datetime import UTC
from enum import Enum
from uuid import NAMESPACE_URL, UUID, uuid5

from src.domain.models.organisation_tables import AgentRun, AgentRunStatus
from src.service.automation.events.contracts import AutomationEventEnvelope

_OUTCOME_BY_STATUS = {
    AgentRunStatus.SUCCEEDED.value: "completed",
    AgentRunStatus.FAILED.value: "failed",
    AgentRunStatus.DELIVERY_FAILED.value: "failed",
    AgentRunStatus.VALIDATION_FAILED.value: "failed",
    AgentRunStatus.CANCELLED.value: "cancelled",
}


class AgentRunEventAdapter:
    """Map one successfully fenced terminal transition to a private envelope."""

    def __init__(self, organisation_id: UUID) -> None:
        self.organisation_id = organisation_id

    def from_run(self, run: AgentRun) -> AutomationEventEnvelope | None:
        """Build the canonical event or contract from run."""
        if run.organisation_id != self.organisation_id:
            raise ValueError("Agent run organisation does not match producer boundary")
        status = _value(run.status)
        outcome = _OUTCOME_BY_STATUS.get(status)
        if outcome is None:
            raise ValueError("Agent run must be terminal before producing an event")
        if run.completed_at is None:
            raise ValueError("Terminal agent run is missing completed_at")
        if run.completed_at.tzinfo is None or run.completed_at.utcoffset() is None:
            raise ValueError("Terminal agent run completed_at must include timezone")

        causation_depth = run.causation_depth + 1
        if causation_depth > 3:
            return None
        event_key = f"agent.run.{outcome}"
        event_id = uuid5(
            NAMESPACE_URL,
            f"copilotgtm:{self.organisation_id}:{event_key}:{run.run_id}",
        )
        return AutomationEventEnvelope.model_validate(
            {
                "event_id": event_id,
                "organisation_id": self.organisation_id,
                "event_key": event_key,
                "route": {"mode": "match"},
                "occurred_at": run.completed_at.astimezone(UTC),
                "source": {
                    "domain": "agent",
                    "system": "copilotgtm",
                    "operation": "signal",
                    "mode": "system",
                    "source_event_id": str(run.run_id),
                },
                "actor": {"type": "agent", "id": str(run.agent_id)},
                "subject": {"type": "agent_run", "id": str(run.run_id)},
                "record": {
                    "id": str(run.run_id),
                    "agent_id": str(run.agent_id),
                    "trigger_id": str(run.trigger_id),
                    "status": status,
                    "event_key": run.event_key,
                    "created_by_user_id": str(run.created_by_user_id),
                },
                "correlation_id": run.correlation_id,
                "caused_by_run_id": run.run_id,
                "causation_depth": causation_depth,
                "idempotency_key": f"{event_key}:{run.run_id}",
            }
        )


def _value(value: object) -> str:
    return str(value.value) if isinstance(value, Enum) else str(value)


__all__ = ["AgentRunEventAdapter"]
