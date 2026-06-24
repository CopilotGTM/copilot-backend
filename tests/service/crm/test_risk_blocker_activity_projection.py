from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    DealRiskBlocker,
    RiskBlockerCategory,
    RiskBlockerSeverity,
    RiskBlockerSource,
    RiskBlockerStatus,
    RiskBlockerType,
)
from src.service.crm.risks.contracts import RiskBlockerActor, RiskBlockerWriteContext
from src.service.crm.risks.lifecycle_service import RiskBlockerLifecycleService
from src.service.crm.risks.write_service import RiskBlockerWriteService


class _FakeExecuteResult:
    def __init__(self, row: object | None = None):
        self._row = row

    def scalar_one_or_none(self) -> object | None:
        return self._row


class _FakeRiskSession:
    def __init__(self, item: DealRiskBlocker | None = None):
        self.item = item
        self.added: list[object] = []
        self.deleted: list[object] = []
        self.flushed = 0
        self.refreshed: list[object] = []

    def add(self, item: object) -> None:
        self.added.append(item)
        if isinstance(item, DealRiskBlocker):
            self.item = item

    async def execute(self, statement):  # noqa: ANN001
        return _FakeExecuteResult(self.item)

    async def flush(self) -> None:
        self.flushed += 1

    async def refresh(self, item: object) -> None:
        self.refreshed.append(item)

    async def delete(self, item: object) -> None:
        self.deleted.append(item)


class _FakeProjection:
    calls: list[tuple[str, object, str | None]] = []

    def __init__(self, session: object, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    async def upsert_risk_blocker_lifecycle(
        self,
        item: DealRiskBlocker,
        *,
        source_type: str,
    ) -> None:
        self.calls.append(("upsert", item, source_type))

    async def soft_delete_risk_blocker_projection(self, item: DealRiskBlocker) -> None:
        self.calls.append(("delete", item, None))


@pytest.fixture(autouse=True)
def _reset_projection_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeProjection.calls = []
    monkeypatch.setattr(
        "src.service.crm.risks.write_service.ActivityFeedProjectionService",
        _FakeProjection,
    )
    monkeypatch.setattr(
        "src.service.crm.risks.lifecycle_service.ActivityFeedProjectionService",
        _FakeProjection,
    )


def _context() -> RiskBlockerWriteContext:
    return RiskBlockerWriteContext(
        source="manual",
        actor=RiskBlockerActor(
            actor_type="user",
            actor_id=uuid4(),
            actor_name="Test User",
        ),
    )


def _risk_item(
    *,
    organisation_id: UUID,
    deal_id: UUID,
    item_type: RiskBlockerType = RiskBlockerType.BLOCKER,
    status: RiskBlockerStatus = RiskBlockerStatus.OPEN,
) -> DealRiskBlocker:
    return DealRiskBlocker(
        risk_blocker_id=uuid4(),
        organisation_id=organisation_id,
        deal_id=deal_id,
        item_type=item_type,
        title="Procurement approval",
        description="Procurement is not aligned yet.",
        category=RiskBlockerCategory.OTHER,
        severity=RiskBlockerSeverity.HIGH,
        status=status,
        source_type=RiskBlockerSource.MANUAL,
    )


@pytest.mark.asyncio
async def test_create_manual_projects_opened_lifecycle_event() -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    session = _FakeRiskSession()

    await RiskBlockerWriteService(session, organisation_id).create_manual(
        deal_id=deal_id,
        item_type=RiskBlockerType.RISK,
        title="Security review",
        context=_context(),
    )

    assert len(_FakeProjection.calls) == 1
    action, item, source_type = _FakeProjection.calls[0]
    assert action == "upsert"
    assert isinstance(item, DealRiskBlocker)
    assert item.deal_id == deal_id
    assert source_type == "RISK_OPENED"


@pytest.mark.asyncio
async def test_create_from_extraction_projects_opened_lifecycle_event() -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    session = _FakeRiskSession()
    extracted = SimpleNamespace(
        item_type=RiskBlockerType.BLOCKER,
        category=RiskBlockerCategory.CONTRACT,
        severity=RiskBlockerSeverity.CRITICAL,
        title="Legal approval",
        description="Legal has not approved terms.",
        quote=None,
        source_context="Discovery call",
        confidence=0.82,
        reasoning="Customer mentioned legal approval.",
        suggested_owner="Legal",
    )

    item = await RiskBlockerWriteService(session, organisation_id).create_from_extraction(
        deal_id=deal_id,
        extracted=extracted,
        context=_context(),
        source_type=RiskBlockerSource.MEETING_GOOGLE,
        source_record_id="meeting-1",
    )

    assert _FakeProjection.calls == [("upsert", item, "BLOCKER_OPENED")]


@pytest.mark.asyncio
async def test_field_update_does_not_project_non_lifecycle_activity() -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    item = _risk_item(organisation_id=organisation_id, deal_id=deal_id)

    await RiskBlockerWriteService(_FakeRiskSession(item), organisation_id).update_fields(
        item.risk_blocker_id,
        {"severity": RiskBlockerSeverity.CRITICAL},
        context=_context(),
    )

    assert _FakeProjection.calls == []


@pytest.mark.asyncio
async def test_resolve_dismiss_and_reopen_project_lifecycle_events() -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    item = _risk_item(organisation_id=organisation_id, deal_id=deal_id)
    service = RiskBlockerLifecycleService(_FakeRiskSession(item), organisation_id)

    await service.resolve(item.risk_blocker_id, "Approved by legal.", context=_context())
    await service.dismiss(item.risk_blocker_id, context=_context(), reason="No longer relevant")
    await service.reopen(item.risk_blocker_id, context=_context())

    assert [call[2] for call in _FakeProjection.calls] == [
        "BLOCKER_RESOLVED",
        "BLOCKER_DISMISSED",
        "BLOCKER_REOPENED",
    ]


@pytest.mark.asyncio
async def test_risk_lifecycle_projects_risk_specific_close_events() -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    item = _risk_item(
        organisation_id=organisation_id,
        deal_id=deal_id,
        item_type=RiskBlockerType.RISK,
    )
    service = RiskBlockerLifecycleService(_FakeRiskSession(item), organisation_id)

    await service.resolve(item.risk_blocker_id, "Approved.", context=_context())
    await service.dismiss(item.risk_blocker_id, context=_context(), reason="No longer relevant")
    await service.reopen(item.risk_blocker_id, context=_context())

    assert [call[2] for call in _FakeProjection.calls] == [
        "RISK_RESOLVED",
        "RISK_DISMISSED",
        "RISK_REOPENED",
    ]


@pytest.mark.asyncio
async def test_delete_soft_deletes_risk_blocker_projection_rows() -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    item = _risk_item(organisation_id=organisation_id, deal_id=deal_id)
    session = _FakeRiskSession(item)

    await RiskBlockerWriteService(session, organisation_id).delete(item.risk_blocker_id)

    assert _FakeProjection.calls == [("delete", item, None)]
    assert session.deleted == [item]
