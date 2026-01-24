"""Service for managing deal risks and blockers via API.

This service handles CRUD operations and status transitions for risks/blockers.
All operations maintain a full audit trail in the risk_blocker_data JSONB field.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.schemas.risk_blocker_models import (
    RiskBlockerDetail,
    RiskBlockerListItem,
    RiskBlockerSummary,
)
from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    DealRiskBlocker,
    RiskBlockerCategory,
    RiskBlockerSeverity,
    RiskBlockerSource,
    RiskBlockerStatus,
    RiskBlockerType,
)
from src.infra.utils.logger import get_logger

logger = get_logger("risk_blocker_service")


class RiskBlockerServiceError(Exception):
    """Base exception for risk/blocker service errors."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class RiskBlockerNotFoundError(RiskBlockerServiceError):
    """Raised when risk/blocker is not found."""

    def __init__(self, risk_blocker_id: UUID):
        super().__init__(f"Risk/blocker {risk_blocker_id} not found", status_code=404)


class InvalidStatusTransitionError(RiskBlockerServiceError):
    """Raised when status transition is invalid."""

    def __init__(self, current: str, target: str):
        super().__init__(
            f"Cannot transition from {current} to {target}",
            status_code=400,
        )


class RiskBlockerService:
    """Service for managing deal risks and blockers."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        user_id: UUID,
        user_name: str | None = None,
    ):
        self.session = session
        self.organisation_id = organisation_id
        self.user_id = user_id
        self.user_name = user_name

    async def close(self) -> None:
        """Dispose the underlying session when the service is released."""
        try:
            await self.session.close()
        except Exception:
            logger.debug("RiskBlockerService close exception", exc_info=True)

    # ─────────────────────────────────────────────────────────────────────────
    # Query Methods
    # ─────────────────────────────────────────────────────────────────────────

    async def list_for_deal(
        self,
        deal_id: UUID,
        limit: int = 20,
        page: int = 1,
        item_type: RiskBlockerType | None = None,
        statuses: list[RiskBlockerStatus] | None = None,
        severity: RiskBlockerSeverity | None = None,
        category: RiskBlockerCategory | None = None,
        include_resolved: bool = False,
        sort_order: str = "desc",
    ) -> tuple[list[RiskBlockerListItem], int, RiskBlockerSummary]:
        """List risks/blockers for a deal with pagination and filters."""

        base_query = select(DealRiskBlocker).where(
            col(DealRiskBlocker.deal_id) == deal_id,
            col(DealRiskBlocker.organisation_id) == self.organisation_id,
        )

        # Build filtered query
        query = base_query

        if item_type:
            query = query.where(col(DealRiskBlocker.item_type) == item_type)

        if statuses:
            query = query.where(col(DealRiskBlocker.status).in_(statuses))
        elif not include_resolved:
            # Exclude resolved/dismissed by default
            query = query.where(
                col(DealRiskBlocker.status).not_in(
                    [
                        RiskBlockerStatus.RESOLVED,
                        RiskBlockerStatus.DISMISSED,
                        RiskBlockerStatus.AI_DISMISSED,
                        RiskBlockerStatus.WONT_FIX,
                    ]
                )
            )

        if severity:
            query = query.where(col(DealRiskBlocker.severity) == severity)

        if category:
            query = query.where(col(DealRiskBlocker.category) == category)

        # Get total count for filtered query
        count_query = select(func.count()).select_from(query.subquery())
        total_count = await self.session.scalar(count_query) or 0

        # Get summary stats (unfiltered for deal overview)
        summary = await self._get_summary(deal_id)

        if page < 1:
            raise RiskBlockerServiceError("Page must be >= 1", status_code=400)

        # Apply sort
        if sort_order == "desc":
            query = query.order_by(
                col(DealRiskBlocker.created_at).desc(),
                col(DealRiskBlocker.risk_blocker_id).desc(),
            )
        else:
            query = query.order_by(
                col(DealRiskBlocker.created_at).asc(),
                col(DealRiskBlocker.risk_blocker_id).asc(),
            )

        offset = (page - 1) * limit
        query = query.offset(offset).limit(limit)

        result = await self.session.execute(query)
        items = list(result.scalars().all())

        response_items = [self._to_list_item(item) for item in items]

        return response_items, total_count, summary

    async def _get_summary(self, deal_id: UUID) -> RiskBlockerSummary:
        """Get summary statistics for a deal."""
        query = select(
            func.count().label("total"),
            func.count()
            .filter(col(DealRiskBlocker.item_type) == RiskBlockerType.RISK)
            .label("risks"),
            func.count()
            .filter(col(DealRiskBlocker.item_type) == RiskBlockerType.BLOCKER)
            .label("blockers"),
            func.count()
            .filter(col(DealRiskBlocker.status) == RiskBlockerStatus.AI_SUGGESTED)
            .label("ai_suggested"),
            func.count()
            .filter(col(DealRiskBlocker.status) == RiskBlockerStatus.OPEN)
            .label("open"),
            func.count()
            .filter(col(DealRiskBlocker.status) == RiskBlockerStatus.IN_PROGRESS)
            .label("in_progress"),
            func.count()
            .filter(col(DealRiskBlocker.status) == RiskBlockerStatus.RESOLVED)
            .label("resolved"),
            func.count()
            .filter(col(DealRiskBlocker.severity) == RiskBlockerSeverity.CRITICAL)
            .label("critical"),
            func.count()
            .filter(col(DealRiskBlocker.severity) == RiskBlockerSeverity.HIGH)
            .label("high"),
        ).where(
            col(DealRiskBlocker.deal_id) == deal_id,
            col(DealRiskBlocker.organisation_id) == self.organisation_id,
        )

        result = await self.session.execute(query)
        row = result.one()

        return RiskBlockerSummary(
            total_count=row.total or 0,
            risks_count=row.risks or 0,
            blockers_count=row.blockers or 0,
            ai_suggested_count=row.ai_suggested or 0,
            open_count=row.open or 0,
            in_progress_count=row.in_progress or 0,
            resolved_count=row.resolved or 0,
            critical_count=row.critical or 0,
            high_count=row.high or 0,
        )

    async def get_by_id(self, risk_blocker_id: UUID) -> RiskBlockerDetail | None:
        """Get a single risk/blocker by ID with full details."""
        result = await self.session.execute(
            select(DealRiskBlocker).where(
                col(DealRiskBlocker.risk_blocker_id) == risk_blocker_id,
                col(DealRiskBlocker.organisation_id) == self.organisation_id,
            )
        )
        item = result.scalar_one_or_none()

        if item:
            return self._to_detail(item)
        return None

    # ─────────────────────────────────────────────────────────────────────────
    # Create Methods
    # ─────────────────────────────────────────────────────────────────────────

    async def create(
        self,
        deal_id: UUID,
        item_type: RiskBlockerType,
        title: str,
        description: str | None = None,
        category: RiskBlockerCategory = RiskBlockerCategory.OTHER,
        severity: RiskBlockerSeverity = RiskBlockerSeverity.MEDIUM,
        owner_name: str | None = None,
        owner_email: str | None = None,
        assigned_team: str | None = None,
    ) -> RiskBlockerDetail:
        """Create a new risk/blocker manually."""

        item = DealRiskBlocker(
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            item_type=item_type,
            title=title,
            description=description,
            category=category,
            severity=severity,
            status=RiskBlockerStatus.OPEN,  # Manual items start as OPEN
            source_type=RiskBlockerSource.MANUAL,
            is_ai_generated=False,
            created_by_user_id=self.user_id,
            created_by_user_name=self.user_name,
            owner_name=owner_name,
            owner_email=owner_email,
            assigned_team=assigned_team,
        )

        # Initialize audit trail
        item.risk_blocker_data = {
            "audit_trail": [
                {
                    "action": "created",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "actor_type": "user",
                    "actor_id": str(self.user_id),
                    "actor_name": self.user_name,
                    "details": {"source": "manual"},
                }
            ],
            "unique_modifiers": [str(self.user_id)],
        }

        self.session.add(item)
        await self.session.commit()
        await self.session.refresh(item)

        logger.info("Created risk/blocker %s for deal %s", item.risk_blocker_id, deal_id)
        return self._to_detail(item)

    # ─────────────────────────────────────────────────────────────────────────
    # Update Methods
    # ─────────────────────────────────────────────────────────────────────────

    async def update(
        self,
        risk_blocker_id: UUID,
        updates: dict[str, Any],
    ) -> RiskBlockerDetail | None:
        """Update a risk/blocker with audit trail."""

        result = await self.session.execute(
            select(DealRiskBlocker).where(
                col(DealRiskBlocker.risk_blocker_id) == risk_blocker_id,
                col(DealRiskBlocker.organisation_id) == self.organisation_id,
            )
        )
        item = result.scalar_one_or_none()

        if not item:
            return None

        # Track changes for audit
        changes: dict[str, dict[str, str]] = {}
        for field, new_value in updates.items():
            if new_value is None:
                continue
            if hasattr(item, field):
                old_value = getattr(item, field)
                # Handle enum comparison
                if hasattr(old_value, "value"):
                    old_value = old_value.value
                if hasattr(new_value, "value"):
                    new_value_str = new_value.value
                else:
                    new_value_str = str(new_value) if new_value is not None else None

                if str(old_value) != str(new_value_str):
                    changes[field] = {"old": str(old_value), "new": str(new_value_str)}
                    setattr(item, field, new_value)

        if not changes:
            return self._to_detail(item)  # No changes

        # Update audit trail
        self._add_audit_entry(
            item,
            "updated",
            {"fields_changed": list(changes.keys()), "changes": changes},
        )

        # Update timestamps
        item.updated_at = datetime.now(UTC)
        item.last_modified_by_user_id = self.user_id
        item.last_modified_by_user_name = self.user_name

        await self.session.commit()
        await self.session.refresh(item)

        logger.info("Updated risk/blocker %s: %s", risk_blocker_id, list(changes.keys()))
        return self._to_detail(item)

    # ─────────────────────────────────────────────────────────────────────────
    # Status Transition Methods
    # ─────────────────────────────────────────────────────────────────────────

    async def confirm(self, risk_blocker_id: UUID) -> RiskBlockerDetail:
        """Confirm an AI-suggested item."""
        result = await self.session.execute(
            select(DealRiskBlocker).where(
                col(DealRiskBlocker.risk_blocker_id) == risk_blocker_id,
                col(DealRiskBlocker.organisation_id) == self.organisation_id,
            )
        )
        item = result.scalar_one_or_none()

        if not item:
            raise RiskBlockerNotFoundError(risk_blocker_id)

        # Handle status as either enum or string (SQLAlchemy may return string)
        current_status = item.status.value if hasattr(item.status, "value") else str(item.status)
        if current_status != RiskBlockerStatus.AI_SUGGESTED.value:
            raise InvalidStatusTransitionError(current_status, "OPEN")

        item.status = RiskBlockerStatus.OPEN
        item.user_confirmed_at = datetime.now(UTC)
        item.last_modified_by_user_id = self.user_id
        item.last_modified_by_user_name = self.user_name
        item.updated_at = datetime.now(UTC)

        self._add_audit_entry(item, "confirmed", {"previous_status": "AI_SUGGESTED"})

        await self.session.commit()
        await self.session.refresh(item)

        logger.info("Confirmed risk/blocker %s", risk_blocker_id)
        return self._to_detail(item)

    async def dismiss(self, risk_blocker_id: UUID) -> RiskBlockerDetail:
        """Dismiss a risk/blocker."""
        result = await self.session.execute(
            select(DealRiskBlocker).where(
                col(DealRiskBlocker.risk_blocker_id) == risk_blocker_id,
                col(DealRiskBlocker.organisation_id) == self.organisation_id,
            )
        )
        item = result.scalar_one_or_none()

        if not item:
            raise RiskBlockerNotFoundError(risk_blocker_id)

        # Handle status as either enum or string (SQLAlchemy may return string)
        previous_status = item.status.value if hasattr(item.status, "value") else str(item.status)
        item.status = RiskBlockerStatus.DISMISSED
        item.last_modified_by_user_id = self.user_id
        item.last_modified_by_user_name = self.user_name
        item.updated_at = datetime.now(UTC)

        self._add_audit_entry(item, "dismissed", {"previous_status": previous_status})

        await self.session.commit()
        await self.session.refresh(item)

        logger.info("Dismissed risk/blocker %s", risk_blocker_id)
        return self._to_detail(item)

    async def resolve(
        self,
        risk_blocker_id: UUID,
        resolution_notes: str,
    ) -> RiskBlockerDetail:
        """Mark a risk/blocker as resolved."""
        result = await self.session.execute(
            select(DealRiskBlocker).where(
                col(DealRiskBlocker.risk_blocker_id) == risk_blocker_id,
                col(DealRiskBlocker.organisation_id) == self.organisation_id,
            )
        )
        item = result.scalar_one_or_none()

        if not item:
            raise RiskBlockerNotFoundError(risk_blocker_id)

        # Handle status as either enum or string (SQLAlchemy may return string)
        previous_status = item.status.value if hasattr(item.status, "value") else str(item.status)
        item.status = RiskBlockerStatus.RESOLVED
        item.resolution_notes = resolution_notes
        item.resolved_at = datetime.now(UTC)
        item.resolved_by = self.user_name or str(self.user_id)
        item.last_modified_by_user_id = self.user_id
        item.last_modified_by_user_name = self.user_name
        item.updated_at = datetime.now(UTC)

        self._add_audit_entry(
            item,
            "resolved",
            {
                "previous_status": previous_status,
                "resolution_notes": resolution_notes,
            },
        )

        await self.session.commit()
        await self.session.refresh(item)

        logger.info("Resolved risk/blocker %s", risk_blocker_id)
        return self._to_detail(item)

    async def reopen(self, risk_blocker_id: UUID) -> RiskBlockerDetail:
        """Reopen a resolved risk/blocker."""
        result = await self.session.execute(
            select(DealRiskBlocker).where(
                col(DealRiskBlocker.risk_blocker_id) == risk_blocker_id,
                col(DealRiskBlocker.organisation_id) == self.organisation_id,
            )
        )
        item = result.scalar_one_or_none()

        if not item:
            raise RiskBlockerNotFoundError(risk_blocker_id)

        # Handle status as either enum or string (SQLAlchemy may return string)
        current_status = item.status.value if hasattr(item.status, "value") else str(item.status)
        if current_status not in [
            RiskBlockerStatus.RESOLVED.value,
            RiskBlockerStatus.DISMISSED.value,
        ]:
            raise InvalidStatusTransitionError(current_status, "OPEN")

        previous_status = current_status
        item.status = RiskBlockerStatus.OPEN
        item.resolved_at = None
        item.resolved_by = None
        item.resolution_notes = None
        item.last_modified_by_user_id = self.user_id
        item.last_modified_by_user_name = self.user_name
        item.updated_at = datetime.now(UTC)

        self._add_audit_entry(item, "reopened", {"previous_status": previous_status})

        await self.session.commit()
        await self.session.refresh(item)

        logger.info("Reopened risk/blocker %s", risk_blocker_id)
        return self._to_detail(item)

    async def delete(self, risk_blocker_id: UUID) -> None:
        """Delete a risk/blocker."""
        result = await self.session.execute(
            select(DealRiskBlocker).where(
                col(DealRiskBlocker.risk_blocker_id) == risk_blocker_id,
                col(DealRiskBlocker.organisation_id) == self.organisation_id,
            )
        )
        item = result.scalar_one_or_none()

        if not item:
            raise RiskBlockerNotFoundError(risk_blocker_id)

        await self.session.delete(item)
        await self.session.commit()

        logger.info("Deleted risk/blocker %s", risk_blocker_id)

    # ─────────────────────────────────────────────────────────────────────────
    # Helper Methods
    # ─────────────────────────────────────────────────────────────────────────

    def _add_audit_entry(
        self,
        item: DealRiskBlocker,
        action: str,
        details: dict,
    ) -> None:
        """Append an entry to the audit trail."""
        if not item.risk_blocker_data:
            item.risk_blocker_data = {"audit_trail": [], "unique_modifiers": []}

        entry = {
            "action": action,
            "timestamp": datetime.now(UTC).isoformat(),
            "actor_type": "user",
            "actor_id": str(self.user_id),
            "actor_name": self.user_name,
            "details": details,
        }

        item.risk_blocker_data["audit_trail"].append(entry)

        # Track unique modifiers
        user_id_str = str(self.user_id)
        if user_id_str not in item.risk_blocker_data.get("unique_modifiers", []):
            item.risk_blocker_data.setdefault("unique_modifiers", []).append(user_id_str)

    def _to_list_item(self, item: DealRiskBlocker) -> RiskBlockerListItem:
        """Convert model to list item response."""
        return RiskBlockerListItem(
            risk_blocker_id=item.risk_blocker_id,
            item_type=item.item_type,
            severity=item.severity,
            status=item.status,
            title=item.title,
            quote=item.quote,
            source_type=item.source_type,
            source_context=item.source_context,
            is_ai_generated=item.is_ai_generated,
            owner_name=item.owner_name,
        )

    def _to_detail(self, item: DealRiskBlocker) -> RiskBlockerDetail:
        """Convert model to detail response with full audit trail."""
        audit_trail = (
            item.risk_blocker_data.get("audit_trail", []) if item.risk_blocker_data else []
        )
        unique_modifiers = (
            item.risk_blocker_data.get("unique_modifiers", []) if item.risk_blocker_data else []
        )
        modification_count = len([e for e in audit_trail if e.get("action") != "created"])

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
            resolved_at=item.resolved_at,
            resolved_by=item.resolved_by,
            created_by_user_id=item.created_by_user_id,
            created_by_user_name=item.created_by_user_name,
            last_modified_by_user_id=item.last_modified_by_user_id,
            last_modified_by_user_name=item.last_modified_by_user_name,
            user_confirmed_at=item.user_confirmed_at,
            identified_at=item.identified_at,
            created_at=item.created_at,
            updated_at=item.updated_at,
            audit_trail=audit_trail,
            modification_count=modification_count,
            unique_modifier_count=len(unique_modifiers),
        )
