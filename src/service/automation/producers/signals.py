"""Canonical events for persisted AI risk and blocker transitions."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    DealRiskBlocker,
    RiskBlockerType,
)
from src.service.automation.events.contracts import (
    AutomationEventActor,
    AutomationEventEnvelope,
    AutomationSourceMode,
)

SignalTransition = Literal["detected", "changed", "resolved"]


class AutomationProducerError(ValueError):
    """Raised when a durable row cannot prove a canonical producer fact."""


class AISignalEventAdapter:
    """Map one provenance-complete risk/blocker transition to an envelope."""

    def __init__(self, organisation_id: UUID) -> None:
        self.organisation_id = organisation_id

    def from_risk_blocker(
        self,
        item: DealRiskBlocker,
        *,
        transition: SignalTransition,
        actor: AutomationEventActor | None = None,
        source_mode: AutomationSourceMode = AutomationSourceMode.SYSTEM,
    ) -> AutomationEventEnvelope | None:
        """Build a content-free event, or stop a chain beyond its depth ceiling."""

        if item.organisation_id != self.organisation_id:
            raise AutomationProducerError("AI signal organisation does not match producer boundary")
        if transition not in {"detected", "changed", "resolved"}:
            raise AutomationProducerError("AI signal transition is invalid")

        inference = _inference_metadata(item)
        transition_id = _required_uuid(inference, "transition_id", "transition identity")
        inference_run_id = _required_uuid(
            inference,
            "inference_run_id",
            "inference run identity",
        )
        evidence_id = _required_text(inference, "evidence_id", "evidence id")
        model_name = _required_text(inference, "model_name", "model name")
        prompt_version = _required_text(
            inference,
            "prompt_version",
            "prompt version",
        )
        confidence = _confidence(item, inference)
        caused_by_run_id = _optional_uuid(
            inference,
            "caused_by_run_id",
            "caused by run identity",
        )
        causation_depth = inference.get("causation_depth", 0)
        if (
            not isinstance(causation_depth, int)
            or isinstance(causation_depth, bool)
            or causation_depth < 0
        ):
            raise AutomationProducerError("causation depth must be a non-negative integer")
        if causation_depth > 3:
            return None
        if (caused_by_run_id is None) != (causation_depth == 0):
            raise AutomationProducerError("causation lineage is incomplete")

        correlation_id = inference.get("correlation_id") or str(inference_run_id)
        if not isinstance(correlation_id, str) or not correlation_id.strip():
            raise AutomationProducerError("correlation identity is missing")

        signal_type = "risk" if _value(item.item_type) == RiskBlockerType.RISK.value else "blocker"
        event_key = f"ai.deal_{signal_type}.{transition}"
        occurred_at = _occurred_at(item, transition)
        event_id = uuid5(
            NAMESPACE_URL,
            f"copilotgtm:{self.organisation_id}:{event_key}:{transition_id}",
        )
        return AutomationEventEnvelope.model_validate(
            {
                "event_id": event_id,
                "organisation_id": self.organisation_id,
                "event_key": event_key,
                "route": {"mode": "match"},
                "occurred_at": occurred_at,
                "source": {
                    "domain": "ai",
                    "system": model_name,
                    "operation": "signal",
                    "mode": source_mode,
                    "source_event_id": str(transition_id),
                },
                "actor": actor
                or {
                    "type": "agent" if caused_by_run_id else "system",
                    "id": str(caused_by_run_id or inference_run_id),
                },
                "subject": {"type": "deal", "id": str(item.deal_id)},
                "record": {
                    "id": str(item.deal_id),
                    "signal_id": str(item.risk_blocker_id),
                    "signal_type": signal_type,
                    "status": _value(item.status),
                    "severity": _value(item.severity),
                    "confidence": format(confidence, "f"),
                    "deal_id": str(item.deal_id),
                    "contact_id": None,
                },
                "references": {
                    "evidence_id": evidence_id,
                    "model_name": model_name,
                    "prompt_version": prompt_version,
                    "inference_run_id": str(inference_run_id),
                },
                "correlation_id": correlation_id.strip(),
                "caused_by_run_id": caused_by_run_id,
                "causation_depth": causation_depth,
                "idempotency_key": f"{event_key}:{transition_id}",
            }
        )


def _inference_metadata(item: DealRiskBlocker) -> dict[str, Any]:
    value = (item.risk_blocker_data or {}).get("automation_signal")
    if not isinstance(value, dict):
        raise AutomationProducerError("durable inference identity is missing")
    return value


def prepare_manual_signal_transition(item: DealRiskBlocker) -> bool:
    """Assign a fresh identity when a user changes an AI-originated signal."""
    data = dict(item.risk_blocker_data or {})
    signal = data.get("automation_signal")
    if not isinstance(signal, dict):
        return False
    transition_id = uuid4()
    signal = dict(signal)
    signal.update(
        {
            "transition_id": str(transition_id),
            "correlation_id": str(transition_id),
            "caused_by_run_id": None,
            "causation_depth": 0,
        }
    )
    data["automation_signal"] = signal
    item.risk_blocker_data = data
    return True


def _required_text(values: dict[str, Any], key: str, label: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AutomationProducerError(f"{label} is missing")
    return value.strip()


def _required_uuid(values: dict[str, Any], key: str, label: str) -> UUID:
    value = _required_text(values, key, label)
    try:
        return UUID(value)
    except ValueError as exc:
        raise AutomationProducerError(f"{label} is invalid") from exc


def _optional_uuid(values: dict[str, Any], key: str, label: str) -> UUID | None:
    value = values.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise AutomationProducerError(f"{label} is invalid")
    try:
        return UUID(value)
    except ValueError as exc:
        raise AutomationProducerError(f"{label} is invalid") from exc


def _confidence(item: DealRiskBlocker, inference: dict[str, Any]) -> Decimal:
    value = inference.get("confidence")
    try:
        confidence = Decimal(str(value))
        stored = Decimal(str(item.ai_confidence))
    except (InvalidOperation, TypeError) as exc:
        raise AutomationProducerError("confidence is missing") from exc
    if not confidence.is_finite() or not Decimal("0") <= confidence <= Decimal("1"):
        raise AutomationProducerError("confidence is outside 0 to 1")
    if confidence != stored:
        raise AutomationProducerError("confidence does not match durable signal state")
    return confidence


def _occurred_at(item: DealRiskBlocker, transition: SignalTransition) -> datetime:
    value = (
        item.identified_at
        if transition == "detected"
        else item.closed_at
        if transition == "resolved" and item.closed_at is not None
        else item.updated_at
    )
    if value.tzinfo is None or value.utcoffset() is None:
        raise AutomationProducerError("AI signal occurrence time must include timezone")
    return value.astimezone(UTC)


def _value(value: object) -> str:
    return str(value.value) if isinstance(value, Enum) else str(value)


__all__ = [
    "AISignalEventAdapter",
    "AutomationProducerError",
    "SignalTransition",
    "prepare_manual_signal_transition",
]
