"""Regression tests for test risk blocker tools; these cases lock the backend contract used by the changed feature."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from src.agents.tools.crm import get_risks_blockers, manage_risks_blockers
from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    RiskBlockerCategory,
    RiskBlockerSeverity,
    RiskBlockerSource,
    RiskBlockerStatus,
    RiskBlockerType,
)


class _Session:
    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        """Expose the commit operation expected by the Session test double."""
        self.committed = True


class _SessionContext:
    def __init__(self, session: _Session) -> None:
        self.session = session

    async def __aenter__(self) -> _Session:
        return self.session

    async def __aexit__(self, *_: object) -> None:
        return None


@dataclass
class _Context:
    organisation_id: Any
    deal_id: Any
    session: _Session
    metadata: dict[str, Any] | None = None

    def acquire_session(self) -> _SessionContext:
        """Build the acquire session fixture used by the surrounding regression cases."""
        return _SessionContext(self.session)


@dataclass
class _Item:
    risk_blocker_id: Any
    item_type: RiskBlockerType = RiskBlockerType.RISK
    category: RiskBlockerCategory = RiskBlockerCategory.BUDGET
    severity: RiskBlockerSeverity = RiskBlockerSeverity.HIGH
    status: RiskBlockerStatus = RiskBlockerStatus.OPEN
    title: str = "Budget freeze"
    description: str = "Budget is frozen"
    quote: str | None = "No budget this quarter"
    source_type: RiskBlockerSource = RiskBlockerSource.MEETING_GOOGLE
    source_record_id: str | None = "meeting-1"
    is_ai_generated: bool = True
    ai_reasoning: str | None = "Buyer statement"
    created_at: datetime = datetime(2026, 6, 20, tzinfo=UTC)


@pytest.mark.asyncio
async def test_get_existing_risks_blockers_uses_read_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the contract that get existing risks blockers uses read service."""
    item_id = uuid4()

    class _ReadService:
        def __init__(self, session: _Session, organisation_id: Any) -> None:
            self.session = session
            self.organisation_id = organisation_id

        async def list_models_for_deal(self, **kwargs: Any) -> list[_Item]:
            """Expose the list models for deal operation expected by the Read Service test double."""
            assert kwargs["statuses"] == [
                RiskBlockerStatus.OPEN,
                RiskBlockerStatus.IN_PROGRESS,
                RiskBlockerStatus.RESOLVED,
                RiskBlockerStatus.WONT_FIX,
            ]
            return [_Item(risk_blocker_id=item_id)]

    monkeypatch.setattr(get_risks_blockers, "RiskBlockerReadService", _ReadService)
    context = _Context(organisation_id=uuid4(), deal_id=uuid4(), session=_Session())

    result = await get_risks_blockers.get_existing_risks_blockers(context=context)

    assert result[0]["risk_blocker_id"] == str(item_id)
    assert result[0]["status"] == RiskBlockerStatus.OPEN.value


@pytest.mark.asyncio
async def test_update_risk_blocker_uses_write_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the contract that update risk blocker uses write service."""
    item_id = uuid4()

    class _WriteService:
        def __init__(self, session: _Session, organisation_id: Any) -> None:
            self.last_changes: dict[str, dict[str, str]] = {}

        async def update_fields(self, risk_blocker_id: Any, updates: dict[str, Any], context: Any):
            """Expose the update fields operation expected by the Write Service test double."""
            assert risk_blocker_id == item_id
            assert updates["severity"] == RiskBlockerSeverity.CRITICAL
            assert context.actor.actor_type == "workflow"
            assert context.inference.evidence_id == "meeting-7"
            assert context.inference.confidence == 0.93
            self.last_changes = {"severity": {"old": "HIGH", "new": "CRITICAL"}}
            return object()

    monkeypatch.setattr(manage_risks_blockers, "RiskBlockerWriteService", _WriteService)
    run_id = uuid4()
    context = _Context(
        organisation_id=uuid4(),
        deal_id=uuid4(),
        session=_Session(),
        metadata={
            "source": "agent_trigger",
            "automation_model_name": "gpt-5-mini",
            "automation_prompt_version": "user-agent:7",
            "automation_inference_run_id": str(run_id),
            "automation_caused_by_run_id": str(run_id),
            "automation_correlation_id": "chain-9",
            "automation_causation_depth": 2,
        },
    )

    result = await manage_risks_blockers.update_risk_blocker_impl(
        context=context,
        risk_blocker_id=str(item_id),
        evidence_id="meeting-7",
        confidence=0.93,
        severity=RiskBlockerSeverity.CRITICAL.value,
    )

    assert result["status"] == "updated"
    assert result["deal_id"] == str(context.deal_id)
    assert result["changes"]["severity"]["new"] == "CRITICAL"
    assert context.session.committed is True


@pytest.mark.asyncio
async def test_resolve_risk_blocker_skips_closed_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the contract that resolve risk blocker skips closed items."""

    class _LifecycleService:
        def __init__(self, session: _Session, organisation_id: Any) -> None:
            pass

        async def get_status(self, risk_blocker_id: Any) -> RiskBlockerStatus:
            """Expose the get status operation expected by the Lifecycle Service test double."""
            return RiskBlockerStatus.RESOLVED

    monkeypatch.setattr(manage_risks_blockers, "RiskBlockerLifecycleService", _LifecycleService)
    run_id = uuid4()
    context = _Context(
        organisation_id=uuid4(),
        deal_id=uuid4(),
        session=_Session(),
        metadata={
            "automation_model_name": "gpt-5-mini",
            "automation_prompt_version": "risk-blocker-extraction-v1",
            "automation_inference_run_id": str(run_id),
        },
    )

    result = await manage_risks_blockers.resolve_risk_blocker_impl(
        context=context,
        risk_blocker_id=str(uuid4()),
        evidence_id="email-3",
        confidence=0.89,
        resolution_notes="Budget approved by CFO",
    )

    assert result["status"] == "skipped"
    assert context.session.committed is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected_status"), [("dismiss", "dismissed"), ("resolve", "resolved")]
)
async def test_risk_blocker_lifecycle_results_include_deal_id(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    expected_status: str,
) -> None:
    """Verify the contract that risk blocker lifecycle results include deal id."""
    item_id = uuid4()

    class _LifecycleService:
        def __init__(self, session: _Session, organisation_id: Any) -> None:
            pass

        async def get_status(self, risk_blocker_id: Any) -> RiskBlockerStatus:
            """Expose the get status operation expected by the Lifecycle Service test double."""
            return RiskBlockerStatus.OPEN

        async def dismiss(self, risk_blocker_id: Any, **kwargs: Any) -> None:
            """Expose the dismiss operation expected by the Lifecycle Service test double."""
            return None

        async def resolve(self, risk_blocker_id: Any, *args: Any, **kwargs: Any) -> None:
            """Expose the resolve operation expected by the Lifecycle Service test double."""
            return None

    monkeypatch.setattr(manage_risks_blockers, "RiskBlockerLifecycleService", _LifecycleService)
    run_id = uuid4()
    context = _Context(
        organisation_id=uuid4(),
        deal_id=uuid4(),
        session=_Session(),
        metadata={
            "automation_model_name": "gpt-5-mini",
            "automation_prompt_version": "risk-blocker-extraction-v1",
            "automation_inference_run_id": str(run_id),
        },
    )

    if action == "dismiss":
        result = await manage_risks_blockers.dismiss_risk_blocker_impl(
            context=context,
            risk_blocker_id=str(item_id),
            evidence_id="meeting-2",
            confidence=0.9,
            reason="No longer relevant",
        )
    else:
        result = await manage_risks_blockers.resolve_risk_blocker_impl(
            context=context,
            risk_blocker_id=str(item_id),
            evidence_id="meeting-2",
            confidence=0.9,
            resolution_notes="Issue resolved",
        )

    assert result["status"] == expected_status
    assert result["deal_id"] == str(context.deal_id)


@pytest.mark.asyncio
async def test_ai_mutation_rejects_missing_durable_provenance_before_opening_session() -> None:
    """Verify the contract that ai mutation rejects missing durable provenance before opening session."""
    context = _Context(
        organisation_id=uuid4(),
        deal_id=uuid4(),
        session=_Session(),
        metadata={},
    )

    result = await manage_risks_blockers.update_risk_blocker_impl(
        context=context,
        risk_blocker_id=str(uuid4()),
        evidence_id="meeting-9",
        confidence=0.8,
        severity=RiskBlockerSeverity.HIGH.value,
    )

    assert result["status"] == "error"
    assert "provenance" in result["message"].lower()
    assert context.session.committed is False
