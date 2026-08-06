"""Risk/blocker write contracts shared by CRM services, AI extraction, and automation signals.

These dataclasses carry the organisation, actor, and inference provenance needed
to audit durable writes and reject incomplete AI-triggered transitions before
they reach storage.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

RiskBlockerActorType = Literal["user", "system", "workflow"]


@dataclass(frozen=True)
class RiskBlockerActor:
    """Actor metadata for audit trail and provenance fields."""

    actor_type: RiskBlockerActorType
    actor_id: UUID
    actor_name: str | None


@dataclass(frozen=True)
class RiskBlockerInference:
    """Durable identity for one AI-created lifecycle transition."""

    transition_id: UUID
    evidence_id: str
    confidence: float
    model_name: str
    prompt_version: str
    inference_run_id: UUID
    correlation_id: str
    caused_by_run_id: UUID | None = None
    causation_depth: int = 0

    def __post_init__(self) -> None:
        for field_name in ("evidence_id", "model_name", "prompt_version", "correlation_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
            object.__setattr__(self, field_name, value.strip())
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if not isinstance(self.causation_depth, int) or isinstance(self.causation_depth, bool):
            raise ValueError("causation_depth must be an integer")
        if not 0 <= self.causation_depth <= 4:
            raise ValueError("causation_depth must be between 0 and 4")
        if (self.caused_by_run_id is None) != (self.causation_depth == 0):
            raise ValueError("causation lineage requires both run identity and child depth")


@dataclass(frozen=True)
class RiskBlockerWriteContext:
    """Caller metadata for risk/blocker writes and future mutation events."""

    source: str
    actor: RiskBlockerActor
    inference: RiskBlockerInference | None = None


class RiskBlockerServiceError(Exception):
    """HTTP-shaped risk/blocker error for routers, agents, and workers."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class RiskBlockerNotFoundError(RiskBlockerServiceError):
    """Raised when a risk/blocker is not found in the organisation boundary."""

    def __init__(self, risk_blocker_id: UUID):
        super().__init__(f"Risk/blocker {risk_blocker_id} not found", status_code=404)


class InvalidStatusTransitionError(RiskBlockerServiceError):
    """Raised when lifecycle transition is not allowed."""

    def __init__(self, current: str, target: str):
        super().__init__(f"Cannot transition from {current} to {target}", status_code=400)
