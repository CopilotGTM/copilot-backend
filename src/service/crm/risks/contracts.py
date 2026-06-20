from __future__ import annotations

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
class RiskBlockerWriteContext:
    """Caller metadata for risk/blocker writes and future mutation events."""

    source: str
    actor: RiskBlockerActor


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
