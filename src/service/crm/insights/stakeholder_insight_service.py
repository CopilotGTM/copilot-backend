"""Service for managing stakeholder insights via API.

This service handles CRUD operations and status transitions for stakeholder insights.
Insights are AI-generated recommendations about deal stakeholders.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.base_mixins import utcnow
from src.domain.models.organisation_tables.sync.stakeholder_insight_models import (
    DealStakeholderInsight,
    StakeholderInsightPriority,
    StakeholderInsightStatus,
    StakeholderInsightType,
)
from src.infra.utils.logger import get_logger

logger = get_logger("stakeholder_insight_service")


class StakeholderInsightServiceError(Exception):
    """Base exception for stakeholder insight service errors."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class StakeholderInsightNotFoundError(StakeholderInsightServiceError):
    """Raised when insight is not found."""

    def __init__(self, insight_id: UUID):
        super().__init__(f"Stakeholder insight {insight_id} not found", status_code=404)


class InvalidStatusTransitionError(StakeholderInsightServiceError):
    """Raised when status transition is invalid."""

    def __init__(self, current: str, target: str):
        super().__init__(
            f"Cannot transition from {current} to {target}",
            status_code=400,
        )


class StakeholderInsightService:
    """Service for managing stakeholder insights."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        user_id: UUID | None = None,
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
            logger.debug("StakeholderInsightService close exception", exc_info=True)

    # ─────────────────────────────────────────────────────────────────────────
    # Query Methods
    # ─────────────────────────────────────────────────────────────────────────

    async def list_for_deal(
        self,
        deal_id: UUID,
        limit: int = 20,
        statuses: list[StakeholderInsightStatus] | None = None,
        priority: StakeholderInsightPriority | None = None,
        insight_type: StakeholderInsightType | None = None,
        include_expired: bool = False,
    ) -> tuple[list[DealStakeholderInsight], int, dict[str, int]]:
        """List insights for a deal with filters.

        Args:
            deal_id: The deal UUID
            limit: Maximum number of insights to return
            statuses: Filter by status(es)
            priority: Filter by priority
            insight_type: Filter by insight type
            include_expired: Whether to include expired insights

        Returns:
            Tuple of (insights list, total count, summary dict)
        """
        base_query = select(DealStakeholderInsight).where(
            DealStakeholderInsight.deal_id == deal_id,
            DealStakeholderInsight.organisation_id == self.organisation_id,
        )

        # Build filtered query
        query = base_query

        if statuses:
            query = query.where(DealStakeholderInsight.status.in_(statuses))
        elif not include_expired:
            # Exclude expired/resolved/dismissed by default
            query = query.where(
                DealStakeholderInsight.status.not_in(
                    [
                        StakeholderInsightStatus.EXPIRED,
                        StakeholderInsightStatus.RESOLVED,
                        StakeholderInsightStatus.DISMISSED,
                    ]
                )
            )

        if priority:
            query = query.where(DealStakeholderInsight.priority == priority)

        if insight_type:
            query = query.where(DealStakeholderInsight.insight_type == insight_type)

        # Get total count for filtered query
        count_query = select(func.count()).select_from(query.subquery())
        total_count = await self.session.scalar(count_query) or 0

        # Get summary stats (unfiltered for deal overview)
        summary = await self._get_summary(deal_id)

        # Order by priority (CRITICAL > HIGH > MEDIUM > LOW) and then by identified_at
        priority_order = {
            StakeholderInsightPriority.CRITICAL: 0,
            StakeholderInsightPriority.HIGH: 1,
            StakeholderInsightPriority.MEDIUM: 2,
            StakeholderInsightPriority.LOW: 3,
        }

        # Apply ordering and limit
        query = query.order_by(DealStakeholderInsight.identified_at.desc()).limit(limit)

        result = await self.session.execute(query)
        insights = list(result.scalars().all())

        # Sort by priority in memory (more flexible than SQL CASE)
        insights.sort(
            key=lambda i: (priority_order.get(i.priority, 99), -i.identified_at.timestamp())
        )

        return insights, total_count, summary

    async def _get_summary(self, deal_id: UUID) -> dict[str, int]:
        """Get summary statistics for insights on a deal.

        Args:
            deal_id: The deal UUID

        Returns:
            Dictionary with counts by priority
        """
        # Count active insights by priority
        base_query = select(DealStakeholderInsight).where(
            DealStakeholderInsight.deal_id == deal_id,
            DealStakeholderInsight.organisation_id == self.organisation_id,
            DealStakeholderInsight.status.in_(
                [
                    StakeholderInsightStatus.ACTIVE,
                    StakeholderInsightStatus.ACKNOWLEDGED,
                ]
            ),
        )

        # Count by priority
        summary = {
            "critical_count": 0,
            "high_count": 0,
            "medium_count": 0,
            "low_count": 0,
        }

        for priority, key in [
            (StakeholderInsightPriority.CRITICAL, "critical_count"),
            (StakeholderInsightPriority.HIGH, "high_count"),
            (StakeholderInsightPriority.MEDIUM, "medium_count"),
            (StakeholderInsightPriority.LOW, "low_count"),
        ]:
            count_query = select(func.count()).select_from(
                base_query.where(DealStakeholderInsight.priority == priority).subquery()
            )
            summary[key] = await self.session.scalar(count_query) or 0

        return summary

    async def get_by_id(
        self,
        insight_id: UUID,
        deal_id: UUID | None = None,
    ) -> DealStakeholderInsight | None:
        """Get a single insight by ID.

        Args:
            insight_id: The insight UUID
            deal_id: Optional deal_id to verify ownership

        Returns:
            The insight or None if not found
        """
        query = select(DealStakeholderInsight).where(
            DealStakeholderInsight.insight_id == insight_id,
            DealStakeholderInsight.organisation_id == self.organisation_id,
        )

        if deal_id:
            query = query.where(DealStakeholderInsight.deal_id == deal_id)

        result = await self.session.execute(query)
        return result.scalars().first()

    # ─────────────────────────────────────────────────────────────────────────
    # Status Transition Methods
    # ─────────────────────────────────────────────────────────────────────────

    async def acknowledge(
        self,
        insight_id: UUID,
        deal_id: UUID | None = None,
    ) -> DealStakeholderInsight:
        """Mark an insight as acknowledged.

        Args:
            insight_id: The insight UUID
            deal_id: Optional deal_id to verify ownership

        Returns:
            The updated insight
        """
        insight = await self.get_by_id(insight_id, deal_id)
        if not insight:
            raise StakeholderInsightNotFoundError(insight_id)

        # Only active insights can be acknowledged
        if insight.status != StakeholderInsightStatus.ACTIVE:
            raise InvalidStatusTransitionError(insight.status.value, "ACKNOWLEDGED")

        insight.status = StakeholderInsightStatus.ACKNOWLEDGED
        insight.updated_at = utcnow()

        await self.session.flush()
        logger.info("Acknowledged insight %s", insight_id)
        return insight

    async def dismiss(
        self,
        insight_id: UUID,
        deal_id: UUID | None = None,
    ) -> DealStakeholderInsight:
        """Dismiss an insight as not relevant.

        Args:
            insight_id: The insight UUID
            deal_id: Optional deal_id to verify ownership

        Returns:
            The updated insight
        """
        insight = await self.get_by_id(insight_id, deal_id)
        if not insight:
            raise StakeholderInsightNotFoundError(insight_id)

        # Can dismiss from ACTIVE or ACKNOWLEDGED
        if insight.status not in [
            StakeholderInsightStatus.ACTIVE,
            StakeholderInsightStatus.ACKNOWLEDGED,
        ]:
            raise InvalidStatusTransitionError(insight.status.value, "DISMISSED")

        insight.status = StakeholderInsightStatus.DISMISSED
        insight.updated_at = utcnow()

        await self.session.flush()
        logger.info("Dismissed insight %s", insight_id)
        return insight

    async def resolve(
        self,
        insight_id: UUID,
        deal_id: UUID | None = None,
        resolution_notes: str | None = None,
    ) -> DealStakeholderInsight:
        """Mark an insight as resolved.

        Args:
            insight_id: The insight UUID
            deal_id: Optional deal_id to verify ownership
            resolution_notes: Optional notes about resolution

        Returns:
            The updated insight
        """
        insight = await self.get_by_id(insight_id, deal_id)
        if not insight:
            raise StakeholderInsightNotFoundError(insight_id)

        # Can resolve from ACTIVE or ACKNOWLEDGED
        if insight.status not in [
            StakeholderInsightStatus.ACTIVE,
            StakeholderInsightStatus.ACKNOWLEDGED,
        ]:
            raise InvalidStatusTransitionError(insight.status.value, "RESOLVED")

        insight.status = StakeholderInsightStatus.RESOLVED
        insight.updated_at = utcnow()

        if resolution_notes:
            insight.insight_data = {
                **insight.insight_data,
                "resolution_notes": resolution_notes,
                "resolved_at": utcnow().isoformat(),
                "resolved_by": self.user_name,
            }

        await self.session.flush()
        logger.info("Resolved insight %s", insight_id)
        return insight

    async def reopen(
        self,
        insight_id: UUID,
        deal_id: UUID | None = None,
    ) -> DealStakeholderInsight:
        """Reopen a resolved or dismissed insight.

        Args:
            insight_id: The insight UUID
            deal_id: Optional deal_id to verify ownership

        Returns:
            The updated insight
        """
        insight = await self.get_by_id(insight_id, deal_id)
        if not insight:
            raise StakeholderInsightNotFoundError(insight_id)

        # Can only reopen resolved or dismissed insights
        if insight.status not in [
            StakeholderInsightStatus.RESOLVED,
            StakeholderInsightStatus.DISMISSED,
        ]:
            raise InvalidStatusTransitionError(insight.status.value, "ACTIVE")

        insight.status = StakeholderInsightStatus.ACTIVE
        insight.updated_at = utcnow()

        await self.session.flush()
        logger.info("Reopened insight %s", insight_id)
        return insight


__all__ = [
    "StakeholderInsightService",
    "StakeholderInsightServiceError",
    "StakeholderInsightNotFoundError",
    "InvalidStatusTransitionError",
]
