"""Privacy and provenance contracts for persisted AI deal signals."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    DealRiskBlocker,
    RiskBlockerCategory,
    RiskBlockerSeverity,
    RiskBlockerSource,
    RiskBlockerStatus,
    RiskBlockerType,
)
from src.service.automation.producers.signals import (
    AISignalEventAdapter,
    AutomationProducerError,
)
from src.service.automation.triggers.registry import AUTOMATION_REGISTRY
from src.service.crm.risks.contracts import (
    RiskBlockerActor,
    RiskBlockerInference,
    RiskBlockerWriteContext,
)
from src.service.crm.risks.lifecycle_service import RiskBlockerLifecycleService
from src.service.crm.risks.write_service import RiskBlockerWriteService


class _Result:
    def __init__(self, item: object | None) -> None:
        self.item = item

    def scalar_one_or_none(self) -> object | None:
        """Expose the scalar one or none operation expected by the Result test double."""
        return self.item


class _Session:
    def __init__(self, item: DealRiskBlocker | None = None) -> None:
        self.item = item
        self.flushed = 0

    def add(self, item: object) -> None:
        """Expose the add operation expected by the Session test double."""
        if isinstance(item, DealRiskBlocker):
            self.item = item

    async def execute(self, _statement: object) -> _Result:
        """Expose the execute operation expected by the Session test double."""
        return _Result(self.item)

    async def flush(self) -> None:
        """Expose the flush operation expected by the Session test double."""
        self.flushed += 1

    async def refresh(self, _item: object) -> None:
        """Expose the refresh operation expected by the Session test double."""
        return None


class _Projection:
    def __init__(self, _session: object, _organisation_id: object) -> None:
        pass

    async def upsert_risk_blocker_lifecycle(self, *_args: object, **_kwargs: object) -> None:
        """Build the upsert risk blocker lifecycle fixture used by the surrounding regression cases."""
        return None


class _Enqueuer:
    def __init__(self, session: _Session) -> None:
        self.session = session
        self.envelopes: list[Any] = []

    async def enqueue(self, envelope: object) -> object:
        """Build the enqueue fixture used by the surrounding regression cases."""
        assert self.session.flushed > 0
        self.envelopes.append(envelope)
        return object()


@pytest.fixture
def projection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build the projection fixture used by the surrounding regression cases."""
    monkeypatch.setattr(
        "src.service.crm.risks.write_service.ActivityFeedProjectionService",
        _Projection,
    )
    monkeypatch.setattr(
        "src.service.crm.risks.lifecycle_service.ActivityFeedProjectionService",
        _Projection,
    )


def inference(*, caused_by_run_id=None, depth: int = 0) -> RiskBlockerInference:
    """Build the inference fixture used by the surrounding regression cases."""
    inference_run_id = uuid4()
    return RiskBlockerInference(
        transition_id=uuid4(),
        evidence_id="meeting-evidence-7",
        confidence=0.91,
        model_name="gpt-5-mini",
        prompt_version="risk-blocker-extraction-v1",
        inference_run_id=inference_run_id,
        correlation_id="chain-7",
        caused_by_run_id=caused_by_run_id,
        causation_depth=depth,
    )


def write_context(inference_value: RiskBlockerInference | None) -> RiskBlockerWriteContext:
    """Build the write context fixture used by the surrounding regression cases."""
    return RiskBlockerWriteContext(
        source="ai_extraction",
        actor=RiskBlockerActor(
            actor_type="system",
            actor_id=uuid4(),
            actor_name="AI extraction",
        ),
        inference=inference_value,
    )


def manual_context() -> RiskBlockerWriteContext:
    """Build a user context for a manual mutation of an AI-originated row."""
    return RiskBlockerWriteContext(
        source="manual",
        actor=RiskBlockerActor(
            actor_type="user",
            actor_id=uuid4(),
            actor_name="Manual User",
        ),
    )


def risk_blocker(organisation_id, *, item_type=RiskBlockerType.RISK) -> DealRiskBlocker:
    """Build the risk blocker fixture used by the surrounding regression cases."""
    transition_id = uuid4()
    inference_run_id = uuid4()
    return DealRiskBlocker(
        organisation_id=organisation_id,
        deal_id=uuid4(),
        item_type=item_type,
        category=RiskBlockerCategory.BUDGET,
        severity=RiskBlockerSeverity.HIGH,
        status=RiskBlockerStatus.OPEN,
        title="Private budget concern",
        description="Private source-derived explanation",
        quote="Private buyer quote",
        source_type=RiskBlockerSource.EMAIL_OUTLOOK,
        source_record_id="email-evidence-42",
        source_context="Private email subject",
        is_ai_generated=True,
        ai_confidence=0.87,
        ai_reasoning="Private chain of evidence",
        extraction_run_id=inference_run_id,
        identified_at=datetime(2026, 8, 5, 8, tzinfo=UTC),
        created_at=datetime(2026, 8, 5, 8, tzinfo=UTC),
        updated_at=datetime(2026, 8, 5, 8, tzinfo=UTC),
        risk_blocker_data={
            "automation_signal": {
                "transition_id": str(transition_id),
                "evidence_id": "email-evidence-42",
                "model_name": "gpt-5-mini",
                "prompt_version": "risk-blocker-extraction-v1",
                "inference_run_id": str(inference_run_id),
                "correlation_id": str(inference_run_id),
                "caused_by_run_id": None,
                "causation_depth": 0,
                "confidence": 0.87,
            }
        },
    )


def test_detected_signal_is_private_complete_and_replay_stable() -> None:
    """Verify the contract that detected signal is private complete and replay stable."""
    organisation_id = uuid4()
    item = risk_blocker(organisation_id)
    adapter = AISignalEventAdapter(organisation_id)

    first = adapter.from_risk_blocker(item, transition="detected")
    replay = adapter.from_risk_blocker(item, transition="detected")

    assert first is not None
    assert replay is not None
    assert first.event_key == "ai.deal_risk.detected"
    AUTOMATION_REGISTRY.event(first.event_key).validate_envelope(first)
    assert first.event_id == replay.event_id
    assert first.idempotency_key == replay.idempotency_key
    assert first.subject.id == str(item.deal_id)
    assert first.record == {
        "id": str(item.deal_id),
        "signal_id": str(item.risk_blocker_id),
        "signal_type": "risk",
        "status": "OPEN",
        "severity": "HIGH",
        "confidence": "0.87",
        "deal_id": str(item.deal_id),
        "contact_id": None,
    }
    assert first.references == {
        "evidence_id": "email-evidence-42",
        "model_name": "gpt-5-mini",
        "prompt_version": "risk-blocker-extraction-v1",
        "inference_run_id": str(item.extraction_run_id),
    }
    serialized = first.model_dump_json()
    for forbidden in (
        item.title,
        item.description,
        item.quote,
        item.source_context,
        item.ai_reasoning,
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize("missing", ["evidence_id", "model_name", "prompt_version"])
def test_signal_rejects_incomplete_durable_inference_identity(missing: str) -> None:
    """Verify the contract that signal rejects incomplete durable inference identity."""
    organisation_id = uuid4()
    item = risk_blocker(organisation_id)
    item.risk_blocker_data["automation_signal"][missing] = None

    with pytest.raises(AutomationProducerError, match=missing.replace("_", " ")):
        AISignalEventAdapter(organisation_id).from_risk_blocker(
            item,
            transition="detected",
        )


def test_changed_and_resolved_signals_keep_transition_and_run_causation() -> None:
    """Verify the contract that changed and resolved signals keep transition and run causation."""
    organisation_id = uuid4()
    parent_run_id = uuid4()
    item = risk_blocker(organisation_id, item_type=RiskBlockerType.BLOCKER)
    metadata = item.risk_blocker_data["automation_signal"]
    metadata.update(
        {
            "caused_by_run_id": str(parent_run_id),
            "correlation_id": "chain-42",
            "causation_depth": 2,
        }
    )
    adapter = AISignalEventAdapter(organisation_id)

    changed = adapter.from_risk_blocker(item, transition="changed")
    metadata["transition_id"] = str(uuid4())
    item.status = RiskBlockerStatus.RESOLVED
    item.closed_at = datetime(2026, 8, 5, 9, tzinfo=UTC)
    item.updated_at = item.closed_at
    resolved = adapter.from_risk_blocker(item, transition="resolved")

    assert changed is not None
    assert resolved is not None
    assert changed.event_key == "ai.deal_blocker.changed"
    assert resolved.event_key == "ai.deal_blocker.resolved"
    assert changed.event_id != resolved.event_id
    assert resolved.caused_by_run_id == parent_run_id
    assert resolved.correlation_id == "chain-42"
    assert resolved.causation_depth == 2


def test_signal_at_depth_above_chain_limit_is_not_emitted() -> None:
    """Verify the contract that signal at depth above chain limit is not emitted."""
    organisation_id = uuid4()
    item = risk_blocker(organisation_id)
    item.risk_blocker_data["automation_signal"]["causation_depth"] = 4

    assert (
        AISignalEventAdapter(organisation_id).from_risk_blocker(
            item,
            transition="changed",
        )
        is None
    )


@pytest.mark.asyncio
async def test_detected_signal_is_persisted_and_enqueued_before_caller_commit(
    projection: None,
) -> None:
    """Verify the contract that detected signal is persisted and enqueued before caller commit."""
    organisation_id = uuid4()
    session = _Session()
    enqueuer = _Enqueuer(session)
    extracted = SimpleNamespace(
        item_type=RiskBlockerType.RISK,
        category=RiskBlockerCategory.BUDGET,
        severity=RiskBlockerSeverity.HIGH,
        title="Budget freeze",
        description="Private description",
        quote="Private quote",
        source_context="Private source context",
        confidence=0.91,
        reasoning="Private reasoning",
        suggested_owner=None,
    )
    inference_value = inference()

    item = await RiskBlockerWriteService(
        session,  # type: ignore[arg-type]
        organisation_id,
        automation_events=enqueuer,
    ).create_from_extraction(
        deal_id=uuid4(),
        extracted=extracted,
        context=write_context(inference_value),
        source_type=RiskBlockerSource.MEETING_GOOGLE,
        source_record_id=inference_value.evidence_id,
    )

    assert item.extraction_run_id == inference_value.inference_run_id
    assert item.risk_blocker_data["automation_signal"]["transition_id"] == str(
        inference_value.transition_id
    )
    assert [event.event_key for event in enqueuer.envelopes] == ["ai.deal_risk.detected"]


@pytest.mark.asyncio
async def test_unchanged_ai_update_emits_nothing(projection: None) -> None:
    """Verify the contract that unchanged ai update emits nothing."""
    organisation_id = uuid4()
    item = risk_blocker(organisation_id)
    session = _Session(item)
    enqueuer = _Enqueuer(session)

    service = RiskBlockerWriteService(
        session,  # type: ignore[arg-type]
        organisation_id,
        automation_events=enqueuer,
    )
    await service.update_fields(
        item.risk_blocker_id,
        {"severity": item.severity},
        context=write_context(inference()),
    )

    assert service.last_changes == {}
    assert enqueuer.envelopes == []


@pytest.mark.asyncio
async def test_resolved_signal_is_enqueued_after_lifecycle_flush(projection: None) -> None:
    """Verify the contract that resolved signal is enqueued after lifecycle flush."""
    organisation_id = uuid4()
    item = risk_blocker(organisation_id, item_type=RiskBlockerType.BLOCKER)
    item.status = RiskBlockerStatus.OPEN
    session = _Session(item)
    enqueuer = _Enqueuer(session)
    parent_run_id = uuid4()

    await RiskBlockerLifecycleService(
        session,  # type: ignore[arg-type]
        organisation_id,
        automation_events=enqueuer,
    ).resolve(
        item.risk_blocker_id,
        "Private resolution notes",
        context=write_context(inference(caused_by_run_id=parent_run_id, depth=2)),
    )

    assert [event.event_key for event in enqueuer.envelopes] == ["ai.deal_blocker.resolved"]
    assert enqueuer.envelopes[0].caused_by_run_id == parent_run_id


@pytest.mark.asyncio
async def test_manual_resolution_of_ai_signal_emits_a_user_transition(projection: None) -> None:
    """Manual resolution of a durable AI signal must remain automation-visible."""
    organisation_id = uuid4()
    item = risk_blocker(organisation_id)
    session = _Session(item)
    enqueuer = _Enqueuer(session)
    context = manual_context()

    await RiskBlockerLifecycleService(
        session,  # type: ignore[arg-type]
        organisation_id,
        automation_events=enqueuer,
    ).resolve(item.risk_blocker_id, "Resolved by seller", context=context)

    assert len(enqueuer.envelopes) == 1
    envelope = enqueuer.envelopes[0]
    assert envelope.event_key == "ai.deal_risk.resolved"
    assert envelope.source.mode == "interactive"
    assert envelope.actor.type == "user"
    assert envelope.actor.id == str(context.actor.actor_id)
    assert (
        envelope.source.source_event_id
        == item.risk_blocker_data["automation_signal"]["transition_id"]
    )


@pytest.mark.asyncio
async def test_manual_update_of_ai_signal_emits_changed_transition(projection: None) -> None:
    """Manual field changes on a durable AI signal must emit a changed event."""
    organisation_id = uuid4()
    item = risk_blocker(organisation_id)
    session = _Session(item)
    enqueuer = _Enqueuer(session)

    await RiskBlockerWriteService(
        session,  # type: ignore[arg-type]
        organisation_id,
        automation_events=enqueuer,
    ).update_fields(
        item.risk_blocker_id,
        {"description": "Updated by seller"},
        context=manual_context(),
    )

    assert [event.event_key for event in enqueuer.envelopes] == ["ai.deal_risk.changed"]
