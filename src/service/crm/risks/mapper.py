from __future__ import annotations

from src.api.schemas.risk_blocker_models import RiskBlockerDetail, RiskBlockerListItem
from src.domain.models.organisation_tables.sync.risk_blocker_models import DealRiskBlocker


class RiskBlockerMapper:
    """Maps risk/blocker rows to API DTOs."""

    def to_list_item(self, item: DealRiskBlocker) -> RiskBlockerListItem:
        return RiskBlockerListItem(
            risk_blocker_id=item.risk_blocker_id,
            item_type=item.item_type,
            severity=item.severity,
            status=item.status,
            title=item.title,
            quote=item.quote,
            source_type=item.source_type,
            source_record_id=item.source_record_id,
            source_context=item.source_context,
            is_ai_generated=item.is_ai_generated,
            owner_name=item.owner_name,
        )

    def to_detail(self, item: DealRiskBlocker) -> RiskBlockerDetail:
        audit_trail = (
            item.risk_blocker_data.get("audit_trail", []) if item.risk_blocker_data else []
        )
        unique_modifiers = (
            item.risk_blocker_data.get("unique_modifiers", []) if item.risk_blocker_data else []
        )
        modification_count = len(
            [entry for entry in audit_trail if entry.get("action") not in ("opened", "created")]
        )

        return RiskBlockerDetail(
            risk_blocker_id=item.risk_blocker_id,
            deal_id=item.deal_id,
            item_type=item.item_type,
            category=item.category,
            severity=item.severity,
            status=item.status,
            title=item.title,
            description=item.description,
            quote=item.quote,
            source_type=item.source_type,
            source_record_id=item.source_record_id,
            source_context=item.source_context,
            is_ai_generated=item.is_ai_generated,
            ai_confidence=item.ai_confidence,
            ai_reasoning=item.ai_reasoning,
            extraction_run_id=item.extraction_run_id,
            owner_name=item.owner_name,
            owner_email=item.owner_email,
            assigned_team=item.assigned_team,
            resolution_notes=item.resolution_notes,
            closed_at=item.closed_at,
            closed_by_user_id=item.closed_by_user_id,
            closed_by_user_name=item.closed_by_user_name,
            created_by_user_id=item.created_by_user_id,
            created_by_user_name=item.created_by_user_name,
            last_modified_by_user_id=item.last_modified_by_user_id,
            last_modified_by_user_name=item.last_modified_by_user_name,
            identified_at=item.identified_at,
            created_at=item.created_at,
            updated_at=item.updated_at,
            audit_trail=audit_trail,
            modification_count=modification_count,
            unique_modifier_count=len(unique_modifiers),
        )
