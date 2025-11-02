"""CRM service layer for accessing and managing deal data."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealStatus
from src.infra.utils.cursor import decode_cursor, encode_cursor
from src.infra.utils.logger import get_logger

logger = get_logger("deals_access_service")


class DealsAccessServiceError(Exception):
    """Custom exception for deals access service errors."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class DealsAccessService:
    """Service for accessing CRM deals with filtering, pagination, and statistics."""

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        """Initialize the service with an org-scoped session.

        Args:
            org_session: Async database session scoped to the organisation
            organisation_id: UUID of the organisation to scope queries
        """
        self.org_session = org_session
        self.organisation_id = organisation_id

    @property
    def session(self) -> AsyncSession:
        """Return the org session."""
        return self.org_session

    async def close(self):
        """Clean up any resources (placeholder for future async cleanup)."""
        try:
            # Future: close any connections or resources
            await self.org_session.close()
        except Exception:
            logger.debug("DealsAccessService close exception", exc_info=True)

    async def list_deals(
        self,
        limit: int = 20,
        cursor: str | None = None,
        statuses: Iterable[CRMDealStatus | str] | None = None,
        pipeline_ids: Iterable[UUID] | None = None,
        owner_external_ids: Iterable[str] | None = None,
        q: str | None = None,
    ) -> tuple[list[CRMDeal], str | None]:
        """List deals with pagination and filters.

        Args:
            limit: Maximum number of deals to return (default 20, max in router)
            cursor: Opaque cursor for pagination (encodes created_at + deal_id)
            statuses: Optional list of deal statuses to filter (OPEN, WON, LOST, CLOSED)
            pipeline_ids: Optional list of pipeline UUIDs to filter
            owner_external_ids: Optional list of owner external IDs to filter
            q: Optional search query for deal name (case-insensitive)

        Returns:
            Tuple of (deals list, next_cursor)
            - next_cursor is None if no more results

        Raises:
            DealsAccessServiceError: If cursor is invalid or database error occurs
        """
        # Base query: organisation scope + exclude soft-deleted
        # Use Core column attributes to keep static typing happy with SQLAlchemy operators
        c = cast(Any, CRMDeal).__table__.c
        stmt = select(CRMDeal).where(
            c.organisation_id == self.organisation_id,
            c.is_deleted.is_(False),
        )

        # Apply filters
        if statuses:
            status_values = [s.value if hasattr(s, "value") else str(s) for s in statuses]
            stmt = stmt.where(c.deal_status.in_(status_values))

        if pipeline_ids:
            stmt = stmt.where(c.pipeline_id.in_(list(pipeline_ids)))

        if owner_external_ids:
            stmt = stmt.where(c.owner_external_id.in_(list(owner_external_ids)))

        if q:
            # Case-insensitive search on deal name
            like_expr = f"%{q.lower()}%"
            stmt = stmt.where(c.name.ilike(like_expr))

        # Cursor-based pagination (keyset pagination on created_at, deal_id)
        if cursor:
            try:
                ts, deal_id = decode_cursor(cursor)
            except Exception as e:
                raise DealsAccessServiceError("Invalid cursor", status_code=400) from e

            stmt = stmt.where(
                or_(
                    c.created_at < ts,
                    and_(
                        c.created_at == ts,
                        c.deal_id < deal_id,
                    ),
                )
            )

        # Order by created_at DESC, deal_id DESC for consistent pagination
        stmt = stmt.order_by(
            c.created_at.desc(),
            c.deal_id.desc(),
        ).limit(limit + 1)

        # Execute query
        res = await self.org_session.execute(stmt)
        rows: list[CRMDeal] = list(res.scalars().all())

        # Check if there are more results
        has_more = len(rows) == limit + 1
        items = rows[:limit]

        # Generate next cursor if there are more results
        next_cursor: str | None = None
        if has_more and items:
            last = items[-1]
            next_cursor = encode_cursor(last.created_at, last.deal_id)

        return items, next_cursor

    async def get_deal_by_id(self, deal_id: UUID) -> CRMDeal | None:
        """Retrieve a single deal by ID.

        Args:
            deal_id: UUID of the deal to retrieve

        Returns:
            CRMDeal instance or None if not found or access denied

        Raises:
            DealsAccessServiceError: If database error occurs
        """
        try:
            c = cast(Any, CRMDeal).__table__.c
            stmt = select(CRMDeal).where(
                c.deal_id == deal_id,
                c.organisation_id == self.organisation_id,
                c.is_deleted.is_(False),
            )
            res = await self.org_session.execute(stmt)
            return res.scalar_one_or_none()
        except Exception as e:
            logger.error("Failed to get deal %s: %s", deal_id, e, exc_info=True)
            raise DealsAccessServiceError("Failed to retrieve deal") from e

    async def get_deals_stats(self) -> dict:
        """Calculate aggregate statistics for deals.

        Returns:
            Dictionary with keys:
                - total: int
                - by_status: dict[str, int]
                - by_pipeline: dict[str, int]
                - total_amount: float | None
                - average_amount: float | None

        Raises:
            DealsAccessServiceError: If database error occurs
        """
        try:
            # Total count
            c = cast(Any, CRMDeal).__table__.c
            total_stmt = select(func.count(c.deal_id)).where(
                c.organisation_id == self.organisation_id,
                c.is_deleted.is_(False),
            )
            total_res = await self.org_session.execute(total_stmt)
            total = total_res.scalar() or 0

            # By status
            status_stmt = (
                select(c.deal_status, func.count(c.deal_id))
                .where(
                    c.organisation_id == self.organisation_id,
                    c.is_deleted.is_(False),
                )
                .group_by(c.deal_status)
            )
            status_res = await self.org_session.execute(status_stmt)
            by_status = {row[0]: row[1] for row in status_res.all()}

            # By pipeline (use pipeline_name as key)
            pipeline_stmt = (
                select(c.pipeline_name, func.count(c.deal_id))
                .where(
                    c.organisation_id == self.organisation_id,
                    c.is_deleted.is_(False),
                    c.pipeline_name.is_not(None),
                )
                .group_by(c.pipeline_name)
            )
            pipeline_res = await self.org_session.execute(pipeline_stmt)
            by_pipeline = {row[0]: row[1] for row in pipeline_res.all()}

            # Sum and average amount
            amount_stmt = select(
                func.sum(c.amount),
                func.avg(c.amount),
            ).where(
                c.organisation_id == self.organisation_id,
                c.is_deleted.is_(False),
                c.amount.is_not(None),
            )
            amount_res = await self.org_session.execute(amount_stmt)
            amount_row = amount_res.one_or_none()

            total_amount = float(amount_row[0]) if amount_row and amount_row[0] else None
            average_amount = float(amount_row[1]) if amount_row and amount_row[1] else None

            return {
                "total": total,
                "by_status": by_status,
                "by_pipeline": by_pipeline,
                "total_amount": total_amount,
                "average_amount": average_amount,
            }
        except Exception as e:
            logger.error("Failed to calculate deals stats: %s", e, exc_info=True)
            raise DealsAccessServiceError("Failed to calculate statistics") from e
