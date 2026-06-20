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

    def acquire_session(self) -> _SessionContext:
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
    item_id = uuid4()

    class _ReadService:
        def __init__(self, session: _Session, organisation_id: Any) -> None:
            self.session = session
            self.organisation_id = organisation_id

        async def list_models_for_deal(self, **kwargs: Any) -> list[_Item]:
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
    item_id = uuid4()

    class _WriteService:
        def __init__(self, session: _Session, organisation_id: Any) -> None:
            self.last_changes: dict[str, dict[str, str]] = {}

        async def update_fields(self, risk_blocker_id: Any, updates: dict[str, Any], context: Any):
            assert risk_blocker_id == item_id
            assert updates["severity"] == RiskBlockerSeverity.CRITICAL
            assert context.actor.actor_type == "system"
            self.last_changes = {"severity": {"old": "HIGH", "new": "CRITICAL"}}
            return object()

    monkeypatch.setattr(manage_risks_blockers, "RiskBlockerWriteService", _WriteService)
    context = _Context(organisation_id=uuid4(), deal_id=uuid4(), session=_Session())

    result = await manage_risks_blockers.update_risk_blocker_impl(
        context=context,
        risk_blocker_id=str(item_id),
        severity=RiskBlockerSeverity.CRITICAL.value,
    )

    assert result["status"] == "updated"
    assert result["changes"]["severity"]["new"] == "CRITICAL"
    assert context.session.committed is True


@pytest.mark.asyncio
async def test_resolve_risk_blocker_skips_closed_items(monkeypatch: pytest.MonkeyPatch) -> None:
    class _LifecycleService:
        def __init__(self, session: _Session, organisation_id: Any) -> None:
            pass

        async def get_status(self, risk_blocker_id: Any) -> RiskBlockerStatus:
            return RiskBlockerStatus.RESOLVED

    monkeypatch.setattr(manage_risks_blockers, "RiskBlockerLifecycleService", _LifecycleService)
    context = _Context(organisation_id=uuid4(), deal_id=uuid4(), session=_Session())

    result = await manage_risks_blockers.resolve_risk_blocker_impl(
        context=context,
        risk_blocker_id=str(uuid4()),
        resolution_notes="Budget approved by CFO",
    )

    assert result["status"] == "skipped"
    assert context.session.committed is False
