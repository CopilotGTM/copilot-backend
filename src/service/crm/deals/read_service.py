"""CRM service layer for accessing and managing deal data."""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, TypedDict, cast
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMDealStage,
    CRMDealStageHistory,
    CRMDealStatus,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.infra.utils.logger import get_logger
from src.service.crm.deals.contracts import DealReadServiceError

logger = get_logger("deal_read_service")


class DealOwnerFilterRow(TypedDict):
    owner_id: UUID
    name: str | None
    email: str | None


class DealReadService:
    """Service for accessing CRM deals with filtering, pagination, and statistics."""

    @staticmethod
    def _deal_status_to_db_value(status: CRMDealStatus | str) -> str:
        """Normalize a status input to the string stored in the DB.

        Pylance cannot narrow a union based on hasattr checks (e.g. "value" on str),
        so use an explicit isinstance check against the enum type.
        """

        if isinstance(status, CRMDealStatus):
            return status.value
        return status

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
            logger.debug("DealReadService close exception", exc_info=True)

    async def list_deals(
        self,
        limit: int = 40,
        page: int = 1,
        statuses: Iterable[CRMDealStatus | str] | None = None,
        pipeline_ids: Iterable[UUID] | None = None,
        owner_ids: Iterable[UUID] | None = None,
        min_amount: float | None = None,
        max_amount: float | None = None,
        min_close_date: datetime | None = None,
        max_close_date: datetime | None = None,
        q: str | None = None,
    ) -> tuple[list[CRMDeal], int]:
        """List deals with pagination and filters.

        Args:
            limit: Maximum number of deals to return (default 40, max in router)
            page: Page number (1-indexed)
            statuses: Optional list of deal statuses to filter (OPEN, WON, LOST, CLOSED)
            pipeline_ids: Optional list of pipeline UUIDs to filter
            owner_ids: Optional list of internal owner IDs to filter
            min_amount: Optional minimum deal amount
            max_amount: Optional maximum deal amount
            min_close_date: Optional minimum close date
            max_close_date: Optional maximum close date
            q: Optional search query for deal name (case-insensitive)

        Returns:
            Tuple of (deals list, total_count)

        Raises:
            DealReadServiceError: If database error occurs
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
            status_values = [self._deal_status_to_db_value(s) for s in statuses]
            stmt = stmt.where(c.deal_status.in_(status_values))

        if pipeline_ids:
            stmt = stmt.where(c.pipeline_id.in_(list(pipeline_ids)))

        if owner_ids:
            stmt = stmt.where(c.owner_id.in_(list(owner_ids)))

        if min_amount is not None:
            stmt = stmt.where(c.amount >= min_amount)

        if max_amount is not None:
            stmt = stmt.where(c.amount <= max_amount)

        if min_close_date:
            stmt = stmt.where(c.close_date >= min_close_date)

        if max_close_date:
            stmt = stmt.where(c.close_date <= max_close_date)

        if q:
            # Case-insensitive search on deal name and company name
            like_expr = f"%{q.lower()}%"
            stmt = stmt.where(or_(c.name.ilike(like_expr), c.company_name.ilike(like_expr)))

        if page < 1:
            raise DealReadServiceError("Page must be >= 1", status_code=400)

        offset = (page - 1) * limit

        count_stmt = select(func.count(c.deal_id)).where(
            c.organisation_id == self.organisation_id,
            c.is_deleted.is_(False),
        )

        if statuses:
            status_values = [self._deal_status_to_db_value(s) for s in statuses]
            count_stmt = count_stmt.where(c.deal_status.in_(status_values))

        if pipeline_ids:
            count_stmt = count_stmt.where(c.pipeline_id.in_(list(pipeline_ids)))

        if owner_ids:
            count_stmt = count_stmt.where(c.owner_id.in_(list(owner_ids)))

        if min_amount is not None:
            count_stmt = count_stmt.where(c.amount >= min_amount)

        if max_amount is not None:
            count_stmt = count_stmt.where(c.amount <= max_amount)

        if min_close_date:
            count_stmt = count_stmt.where(c.close_date >= min_close_date)

        if max_close_date:
            count_stmt = count_stmt.where(c.close_date <= max_close_date)

        if q:
            like_expr = f"%{q.lower()}%"
            count_stmt = count_stmt.where(
                or_(c.name.ilike(like_expr), c.company_name.ilike(like_expr))
            )

        count_res = await self.org_session.execute(count_stmt)
        total_count = count_res.scalar() or 0

        # Order by created_at DESC, deal_id DESC for consistent pagination
        stmt = (
            stmt.order_by(
                c.created_at.desc(),
                c.deal_id.desc(),
            )
            .offset(offset)
            .limit(limit)
        )

        # Execute query
        res = await self.org_session.execute(stmt)
        rows: list[CRMDeal] = list(res.scalars().all())

        return rows, total_count

    async def get_all_deals(
        self,
        max_results: int = 500,
        statuses: Iterable[CRMDealStatus | str] | None = None,
        pipeline_ids: Iterable[UUID] | None = None,
        owner_ids: Iterable[UUID] | None = None,
        min_amount: float | None = None,
        max_amount: float | None = None,
        min_close_date: datetime | None = None,
        max_close_date: datetime | None = None,
        q: str | None = None,
    ) -> list[CRMDeal]:
        """Retrieve all deals without pagination (capped at max_results).

        Useful for small datasets where client-side filtering is preferred.
        All filters are applied server-side before returning results.

        Args:
            max_results: Maximum number of deals to return (default 500, safety cap)
            statuses: Optional list of deal statuses to filter (OPEN, WON, LOST, CLOSED)
            pipeline_ids: Optional list of pipeline UUIDs to filter
            owner_ids: Optional list of internal owner IDs to filter
            min_amount: Optional minimum deal amount
            max_amount: Optional maximum deal amount
            min_close_date: Optional minimum close date
            max_close_date: Optional maximum close date
            q: Optional search query for deal name (case-insensitive)

        Returns:
            List of all matching deals (up to max_results)

        Raises:
            DealReadServiceError: If database error occurs
        """
        try:
            c = cast(Any, CRMDeal).__table__.c
            stmt = select(CRMDeal).where(
                c.organisation_id == self.organisation_id,
                c.is_deleted.is_(False),
            )

            # Apply filters (same logic as list_deals)
            if statuses:
                status_values = [self._deal_status_to_db_value(s) for s in statuses]
                stmt = stmt.where(c.deal_status.in_(status_values))

            if pipeline_ids:
                stmt = stmt.where(c.pipeline_id.in_(list(pipeline_ids)))

            if owner_ids:
                stmt = stmt.where(c.owner_id.in_(list(owner_ids)))

            if min_amount is not None:
                stmt = stmt.where(c.amount >= min_amount)

            if max_amount is not None:
                stmt = stmt.where(c.amount <= max_amount)

            if min_close_date:
                stmt = stmt.where(c.close_date >= min_close_date)

            if max_close_date:
                stmt = stmt.where(c.close_date <= max_close_date)

            if q:
                like_expr = f"%{q.lower()}%"
                stmt = stmt.where(c.name.ilike(like_expr))

            # Order by created_at DESC for consistent results
            stmt = stmt.order_by(
                c.created_at.desc(),
                c.deal_id.desc(),
            ).limit(max_results)

            res = await self.org_session.execute(stmt)
            return list(res.scalars().all())

        except Exception as e:
            logger.error("Failed to get all deals: %s", e, exc_info=True)
            raise DealReadServiceError("Failed to retrieve deals") from e

    async def get_deal_by_id(self, deal_id: UUID) -> CRMDeal | None:
        """Retrieve a single deal by ID.

        Args:
            deal_id: UUID of the deal to retrieve

        Returns:
            CRMDeal instance or None if not found or access denied

        Raises:
            DealReadServiceError: If database error occurs
        """
        try:
            c = cast(Any, CRMDeal).__table__.c
            stmt = (
                select(CRMDeal)
                .options(
                    joinedload(CRMDeal.participants),  # type: ignore[arg-type]
                    joinedload(CRMDeal.stage).joinedload(CRMDealStage.pipeline),  # type: ignore[arg-type]
                )
                .where(
                    c.deal_id == deal_id,
                    c.organisation_id == self.organisation_id,
                    c.is_deleted.is_(False),
                )
            )
            res = await self.org_session.execute(stmt)
            return res.unique().scalar_one_or_none()
        except Exception as e:
            logger.error("Failed to get deal %s: %s", deal_id, e, exc_info=True)
            raise DealReadServiceError("Failed to retrieve deal") from e

    async def get_deal_stage_history(self, deal_id: UUID) -> list[dict[str, Any]]:
        """Retrieve stage transition history for a deal with stage details.

        Args:
            deal_id: UUID of the deal

        Returns:
            List of stage history items with computed duration_days and stage labels

        Raises:
            DealReadServiceError: If database error occurs
        """
        try:
            # Join stage history with stage table to get stage names/labels
            h_c = cast(Any, CRMDealStageHistory).__table__.c
            s_c = cast(Any, CRMDealStage).__table__.c

            stmt = (
                select(
                    h_c.history_id,
                    h_c.stage_id,
                    h_c.from_stage_id,
                    h_c.entered_at,
                    h_c.exited_at,
                    h_c.amount_at_stage,
                    h_c.probability_at_stage,
                    h_c.source,
                    s_c.name.label("stage_name"),
                    s_c.label.label("stage_label"),
                )
                .outerjoin(
                    cast(Any, CRMDealStage).__table__,
                    and_(
                        h_c.stage_id == s_c.stage_id,
                        s_c.organisation_id == self.organisation_id,
                    ),
                )
                .where(
                    h_c.organisation_id == self.organisation_id,
                    h_c.deal_id == deal_id,
                    h_c.is_deleted.is_(False),
                )
                .order_by(h_c.entered_at.asc())
            )

            res = await self.org_session.execute(stmt)
            rows = res.all()

            history_items = []
            for row in rows:
                # Calculate duration in days
                duration_days = None
                if row.entered_at:
                    end_time = row.exited_at or datetime.now(UTC)
                    start_time = (
                        row.entered_at
                        if row.entered_at.tzinfo
                        else row.entered_at.replace(tzinfo=UTC)
                    )
                    end_time = end_time if end_time.tzinfo else end_time.replace(tzinfo=UTC)
                    duration_days = (end_time - start_time).days

                item = {
                    "history_id": row.history_id,
                    "stage_id": row.stage_id,
                    "stage_name": row.stage_name,
                    "stage_label": row.stage_label,
                    "from_stage_id": row.from_stage_id,
                    "entered_at": row.entered_at,
                    "exited_at": row.exited_at,
                    "duration_days": duration_days,
                    "amount_at_stage": float(row.amount_at_stage) if row.amount_at_stage else None,
                    "probability_at_stage": row.probability_at_stage,
                    "source": row.source,
                }
                history_items.append(item)

            return history_items
        except Exception as e:
            logger.error("Failed to get stage history for deal %s: %s", deal_id, e, exc_info=True)
            raise DealReadServiceError("Failed to retrieve stage history") from e

    async def get_pipeline_stages(self, pipeline_id: UUID) -> list[CRMDealStage]:
        """Retrieve all stages for a given pipeline, ordered by display order.

        Args:
            pipeline_id: UUID of the pipeline

        Returns:
            List of CRMDealStage objects
        """
        try:
            # Use cast(Any, ...) to avoid Mypy errors with SQLModel fields in expressions
            s_model = cast(Any, CRMDealStage)
            stmt = (
                select(CRMDealStage)
                .where(
                    s_model.pipeline_id == pipeline_id,
                    s_model.organisation_id == self.organisation_id,
                )
                .order_by(s_model.display_order.asc().nulls_last())
            )
            res = await self.org_session.execute(stmt)
            return list(res.scalars().all())
        except Exception as e:
            logger.error("Failed to get pipeline stages for %s: %s", pipeline_id, e, exc_info=True)
            return []

    async def get_company_by_id(self, company_id: UUID) -> CRMCompany | None:
        """Retrieve a company by its internal ID.

        Args:
            company_id: The internal ID of the company

        Returns:
            CRMCompany object or None if not found
        """
        try:
            # Use cast(Any, ...) to avoid Mypy errors with SQLModel fields in expressions
            c_model = cast(Any, CRMCompany)
            stmt = select(CRMCompany).where(
                c_model.company_id == company_id,
                c_model.organisation_id == self.organisation_id,
            )
            res = await self.org_session.execute(stmt)
            return res.scalar_one_or_none()
        except Exception as e:
            logger.error("Failed to get company by id %s: %s", company_id, e, exc_info=True)
            return None

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
            DealReadServiceError: If database error occurs
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
            raise DealReadServiceError("Failed to calculate statistics") from e

    async def get_deal_team_members_batch(
        self, deal_ids: list[UUID]
    ) -> dict[UUID, list[dict[str, Any]]]:
        """Retrieve USER-type participants (team members) for multiple deals, excluding owners.

        This returns collaborators, split_owners, and other user participants
        associated with the given deals. Owners are excluded since they are
        returned separately in owner_details. Results are grouped by deal_id.

        Args:
            deal_ids: List of deal UUIDs to fetch team members for

        Returns:
            Dictionary mapping deal_id to list of team members.
            Each team member dict contains:
                - user_id: Internal user ID when linked
                - name: Display name (if known)
                - email: Email address (if known)
                - role: Relationship role (collaborator, split_owner, etc.)

        Raises:
            DealReadServiceError: If database error occurs
        """
        if not deal_ids:
            return {}

        try:
            c = cast(Any, CRMDealParticipant).__table__.c
            stmt = (
                select(
                    c.participant_id,
                    c.deal_id,
                    c.related_local_id,
                    c.related_name,
                    c.related_email,
                    c.relationship_role,
                )
                .where(
                    c.organisation_id == self.organisation_id,
                    c.deal_id.in_(deal_ids),
                    c.participant_type == CRMParticipantType.USER.value,
                    # NULL means a normal team member; SQL `!=` would otherwise drop it.
                    func.coalesce(c.relationship_role, "") != "owner",
                    c.is_deleted.is_(False),
                )
                .order_by(
                    c.deal_id,
                    c.relationship_role,
                )
            )

            result = await self.org_session.execute(stmt)
            rows = result.all()

            # Group by deal_id
            team_by_deal: dict[UUID, list[dict[str, Any]]] = {deal_id: [] for deal_id in deal_ids}

            for row in rows:
                deal_id = row.deal_id
                if deal_id in team_by_deal:
                    team_by_deal[deal_id].append(
                        {
                            "participant_id": row.participant_id,
                            "user_id": row.related_local_id,
                            "name": row.related_name,
                            "email": row.related_email,
                            "role": row.relationship_role or "member",
                        }
                    )

            return team_by_deal

        except Exception as e:
            logger.error("Failed to get deal team members: %s", e, exc_info=True)
            raise DealReadServiceError("Failed to retrieve deal team members") from e

    async def list_deal_owners(
        self,
        q: str | None = None,
        limit: int = 200,
        owner_ids: Iterable[UUID] | None = None,
    ) -> list[DealOwnerFilterRow]:
        """Return distinct deal owners for filter dropdowns.

        Args:
            q: Optional search term for owner name/email/external ID.
            limit: Maximum number of owners to return.
            owner_ids: Optional owner IDs to hydrate even when they do not match q.

        Returns:
            List of distinct owners with owner_id, name, and email.
        """
        try:
            c = cast(Any, CRMDeal).__table__.c
            selected_owner_ids = list(dict.fromkeys(owner_ids or []))

            base_stmt = (
                select(
                    c.owner_id.label("owner_id"),
                    func.max(c.owner_name).label("name"),
                    func.max(c.owner_email).label("email"),
                )
                .where(
                    c.organisation_id == self.organisation_id,
                    c.is_deleted.is_(False),
                    c.owner_id.is_not(None),
                )
                .group_by(c.owner_id)
            )

            owners_by_id: dict[UUID, DealOwnerFilterRow] = {}
            if selected_owner_ids:
                selected_stmt = base_stmt.where(c.owner_id.in_(selected_owner_ids)).order_by(
                    func.max(c.owner_name).asc().nulls_last(),
                    c.owner_id.asc(),
                )
                selected_result = await self.org_session.execute(selected_stmt)
                for row in selected_result.all():
                    owners_by_id[cast(UUID, row.owner_id)] = {
                        "owner_id": cast(UUID, row.owner_id),
                        "name": cast(str | None, row.name),
                        "email": cast(str | None, row.email),
                    }

            stmt = base_stmt
            if q:
                like_expr = f"%{q.lower()}%"
                conditions = [
                    c.owner_name.ilike(like_expr),
                    c.owner_email.ilike(like_expr),
                ]
                with contextlib.suppress(TypeError, ValueError):
                    conditions.append(c.owner_id == UUID(q))
                stmt = stmt.where(or_(*conditions))

            if selected_owner_ids:
                stmt = stmt.where(c.owner_id.not_in(selected_owner_ids))

            stmt = stmt.order_by(
                func.max(c.owner_name).asc().nulls_last(),
                c.owner_id.asc(),
            ).limit(limit)

            result = await self.org_session.execute(stmt)
            rows = result.all()

            for row in rows:
                owners_by_id.setdefault(
                    cast(UUID, row.owner_id),
                    {
                        "owner_id": cast(UUID, row.owner_id),
                        "name": cast(str | None, row.name),
                        "email": cast(str | None, row.email),
                    },
                )
            return list(owners_by_id.values())
        except Exception as exc:
            logger.error("Failed to list deal owners: %s", exc, exc_info=True)
            raise DealReadServiceError("Failed to retrieve deal owners") from exc

    async def resolve_current_user_deal_owners(
        self,
        *,
        user_id: UUID,
        user_email: str | None,
    ) -> list[DealOwnerFilterRow]:
        """Return deal-owner rows that map to the current app user."""
        try:
            deal_columns = cast(Any, CRMDeal).__table__.c
            crm_user_columns = cast(Any, CRMUser).__table__.c

            crm_user_conditions = [crm_user_columns.user_id == user_id]
            owner_conditions = [deal_columns.owner_id == user_id]
            if user_email:
                email_lower = user_email.lower()
                crm_user_conditions.append(func.lower(crm_user_columns.email) == email_lower)
                owner_conditions.append(func.lower(deal_columns.owner_email) == email_lower)

            crm_user_id_stmt = select(crm_user_columns.user_id).where(
                crm_user_columns.organisation_id == self.organisation_id,
                crm_user_columns.is_deleted.is_(False),
                or_(*crm_user_conditions),
            )
            owner_conditions.append(deal_columns.owner_id.in_(crm_user_id_stmt))

            stmt = (
                select(
                    deal_columns.owner_id.label("owner_id"),
                    func.max(deal_columns.owner_name).label("name"),
                    func.max(deal_columns.owner_email).label("email"),
                )
                .where(
                    deal_columns.organisation_id == self.organisation_id,
                    deal_columns.is_deleted.is_(False),
                    deal_columns.owner_id.is_not(None),
                    or_(*owner_conditions),
                )
                .group_by(deal_columns.owner_id)
                .order_by(func.max(deal_columns.owner_name).asc().nulls_last())
            )

            result = await self.org_session.execute(stmt)
            return [
                {
                    "owner_id": cast(UUID, row.owner_id),
                    "name": cast(str | None, row.name),
                    "email": cast(str | None, row.email),
                }
                for row in result.all()
            ]
        except Exception as exc:
            logger.error("Failed to resolve current user deal owners: %s", exc, exc_info=True)
            raise DealReadServiceError("Failed to resolve current user deal owners") from exc


def extract_deal_health(deal_summary_data: dict | None) -> str | None:
    """Extract the AI-assessed deal health from deal summary data.

    Args:
        deal_summary_data: The deal_summary_data JSONB field from CRMDeal

    Returns:
        The deal health status string, or None if not available
    """
    if not deal_summary_data:
        return None

    data = deal_summary_data.get("data") if isinstance(deal_summary_data, dict) else None
    if not isinstance(data, dict):
        return None

    return data.get("deal_health")


def extract_deal_health_reason(deal_summary_data: dict | None) -> str | None:
    """Extract the rationale for the AI-assessed deal health from deal summary data.

    Args:
        deal_summary_data: The deal_summary_data JSONB field from CRMDeal

    Returns:
        The deal health reason string, or None if not available
    """
    if not deal_summary_data:
        return None

    data = deal_summary_data.get("data") if isinstance(deal_summary_data, dict) else None
    if not isinstance(data, dict):
        return None

    return data.get("deal_health_reason")
