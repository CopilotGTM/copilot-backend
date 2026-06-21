from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import col
from src.contracts.crm.risk_blockers import RiskBlockerDetail
from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    DealRiskBlocker,
    RiskBlockerCategory,
    RiskBlockerSeverity,
    RiskBlockerSource,
    RiskBlockerStatus,
    RiskBlockerType,
)
from src.domain.models.organisation_tables.sync.timeline_models import TimelineEventType
from src.service.crm.risks.contracts import (
    RiskBlockerNotFoundError,
    RiskBlockerWriteContext,
)
from src.service.crm.risks.mapper import RiskBlockerMapper
from src.service.crm.timeline.deal_timeline_sync_service import (
    build_risk_blocker_timeline_event,
)


class ExtractedRiskBlockerProtocol(Protocol):
    item_type: RiskBlockerType
    category: RiskBlockerCategory
    severity: RiskBlockerSeverity
    title: str
    description: str
    quote: str | None
    source_context: str
    confidence: float
    reasoning: str
    suggested_owner: str | None


def _enum_value(value: Any) -> str:
    return str(value.value) if hasattr(value, "value") else str(value)


def _short(value: str) -> str:
    return value[:100] + "..." if len(value) > 100 else value


def add_risk_blocker_audit_entry(
    item: DealRiskBlocker,
    action: str,
    details: dict[str, Any],
    context: RiskBlockerWriteContext,
) -> None:
    if not item.risk_blocker_data:
        item.risk_blocker_data = {"audit_trail": [], "unique_modifiers": []}

    audit_trail = item.risk_blocker_data.setdefault("audit_trail", [])
    audit_trail.append(
        {
            "action": action,
            "timestamp": datetime.now(UTC).isoformat(),
            "actor_type": context.actor.actor_type,
            "actor_id": str(context.actor.actor_id),
            "actor_name": context.actor.actor_name,
            "details": details,
        }
    )

    modifiers = item.risk_blocker_data.setdefault("unique_modifiers", [])
    actor_id = str(context.actor.actor_id)
    if actor_id not in modifiers:
        modifiers.append(actor_id)

    flag_modified(item, "risk_blocker_data")


def build_extraction_risk_blocker(
    *,
    organisation_id: UUID,
    deal_id: UUID,
    extracted: ExtractedRiskBlockerProtocol,
    context: RiskBlockerWriteContext,
    source_type: RiskBlockerSource,
    source_record_id: str | None = None,
) -> DealRiskBlocker:
    return DealRiskBlocker(
        organisation_id=organisation_id,
        deal_id=deal_id,
        item_type=extracted.item_type,
        category=extracted.category,
        severity=extracted.severity,
        status=RiskBlockerStatus.OPEN,
        title=extracted.title,
        description=extracted.description,
        quote=extracted.quote,
        source_type=source_type,
        source_record_id=source_record_id,
        source_context=extracted.source_context,
        is_ai_generated=True,
        ai_confidence=extracted.confidence,
        ai_reasoning=extracted.reasoning,
        owner_name=extracted.suggested_owner,
        created_by_user_id=context.actor.actor_id,
        created_by_user_name=context.actor.actor_name,
        last_modified_by_user_id=context.actor.actor_id,
        last_modified_by_user_name=context.actor.actor_name,
        risk_blocker_data={
            "audit_trail": [
                {
                    "action": "opened",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "actor_type": context.actor.actor_type,
                    "actor_id": str(context.actor.actor_id),
                    "actor_name": context.actor.actor_name,
                    "details": {
                        "source": context.source,
                        "confidence": extracted.confidence,
                        "reasoning": extracted.reasoning,
                    },
                }
            ],
            "unique_modifiers": [str(context.actor.actor_id)],
        },
    )


class RiskBlockerWriteService:
    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self._mapper = RiskBlockerMapper()
        self.last_changes: dict[str, dict[str, str]] = {}

    async def create_manual(
        self,
        deal_id: UUID,
        item_type: RiskBlockerType,
        title: str,
        context: RiskBlockerWriteContext,
        description: str | None = None,
        category: RiskBlockerCategory = RiskBlockerCategory.OTHER,
        severity: RiskBlockerSeverity = RiskBlockerSeverity.MEDIUM,
        owner_name: str | None = None,
        owner_email: str | None = None,
        assigned_team: str | None = None,
    ) -> RiskBlockerDetail:
        item = DealRiskBlocker(
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            item_type=item_type,
            title=title,
            description=description,
            category=category,
            severity=severity,
            status=RiskBlockerStatus.OPEN,
            source_type=RiskBlockerSource.MANUAL,
            is_ai_generated=False,
            created_by_user_id=context.actor.actor_id,
            created_by_user_name=context.actor.actor_name,
            owner_name=owner_name,
            owner_email=owner_email,
            assigned_team=assigned_team,
        )
        item.risk_blocker_data = {
            "audit_trail": [
                {
                    "action": "opened",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "actor_type": context.actor.actor_type,
                    "actor_id": str(context.actor.actor_id),
                    "actor_name": context.actor.actor_name,
                    "details": {"source": context.source},
                }
            ],
            "unique_modifiers": [str(context.actor.actor_id)],
        }

        self.session.add(item)
        self.session.add(self._build_opened_timeline_event(item))
        await self.session.flush()
        await self.session.refresh(item)
        return self._mapper.to_detail(item)

    async def create_from_extraction(
        self,
        deal_id: UUID,
        extracted: ExtractedRiskBlockerProtocol,
        context: RiskBlockerWriteContext,
        source_type: RiskBlockerSource,
        source_record_id: str | None = None,
    ) -> DealRiskBlocker:
        item = build_extraction_risk_blocker(
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            extracted=extracted,
            context=context,
            source_type=source_type,
            source_record_id=source_record_id,
        )
        self.session.add(item)
        self.session.add(self._build_opened_timeline_event(item))
        await self.session.flush()
        return item

    async def update_fields(
        self,
        risk_blocker_id: UUID,
        updates: dict[str, Any],
        context: RiskBlockerWriteContext,
    ) -> RiskBlockerDetail | None:
        item = await self._load_item(risk_blocker_id)
        self.last_changes = {}
        if not item:
            return None

        changes: dict[str, dict[str, str]] = {}
        data = item.risk_blocker_data or {}
        for field, new_value in updates.items():
            if new_value is None or field == "status":
                continue
            if field == "recommended_next_step":
                old_next_step = str(data.get("recommended_next_step") or "")
                if old_next_step != new_value:
                    changes[field] = {"old": _short(old_next_step), "new": _short(str(new_value))}
                    data["recommended_next_step"] = new_value
                continue
            if not hasattr(item, field):
                continue
            old_value = getattr(item, field)
            new_value_str = _enum_value(new_value) if new_value is not None else None
            if str(_enum_value(old_value)) != str(new_value_str):
                changes[field] = {"old": str(_enum_value(old_value)), "new": str(new_value_str)}
                setattr(item, field, new_value)

        if not changes:
            return self._mapper.to_detail(item)

        self.last_changes = changes
        item.risk_blocker_data = data
        add_risk_blocker_audit_entry(
            item,
            "updated",
            {"fields_changed": list(changes.keys()), "changes": changes},
            context,
        )
        now = datetime.now(UTC)
        item.updated_at = now
        item.last_modified_by_user_id = context.actor.actor_id
        item.last_modified_by_user_name = context.actor.actor_name
        await self.session.flush()
        await self.session.refresh(item)
        return self._mapper.to_detail(item)

    async def delete(self, risk_blocker_id: UUID) -> None:
        item = await self._load_item(risk_blocker_id)
        if not item:
            raise RiskBlockerNotFoundError(risk_blocker_id)
        await self.session.delete(item)
        await self.session.flush()

    async def _load_item(self, risk_blocker_id: UUID) -> DealRiskBlocker | None:
        result = await self.session.execute(
            select(DealRiskBlocker).where(
                col(DealRiskBlocker.risk_blocker_id) == risk_blocker_id,
                col(DealRiskBlocker.organisation_id) == self.organisation_id,
            )
        )
        return result.scalar_one_or_none()

    def _build_opened_timeline_event(self, item: DealRiskBlocker):
        event_type = (
            TimelineEventType.RISK_FLAGGED
            if item.item_type == RiskBlockerType.RISK
            else TimelineEventType.BLOCKER_ADDED
        )
        return build_risk_blocker_timeline_event(
            organisation_id=self.organisation_id,
            deal_id=item.deal_id,
            risk_blocker=item,
            event_type=event_type,
        )
