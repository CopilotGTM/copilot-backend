"""List Deals Tool - Browse deals with flexible filtering and sorting.

This tool provides comprehensive deal listing with various filter options
for browsing and filtering deals across the organization.

USE CASES:
- "Show me all open deals" → status="open"
- "List deals worth more than $100K" → min_amount=100000
- "What deals does john@company.com own?" → owner_email="john@company.com"
- "Show deals with Acme" → company_name="Acme"
- "Find deals in the Negotiation stage" → stage_name="Negotiation"

FILTERS AVAILABLE:
- name: Partial match on deal name
- status: "open", "closed", "won", "lost"
- stage_name: Filter by sales stage
- min_amount / max_amount: Deal value range
- owner_email: Deal owner filter
- company_name: Partial match on company name

RETURNS:
Summary information for each deal including: deal_id, name, company_name,
amount, currency, stage, status, owner, dates, etc.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access

This is an ORG-SCOPED tool. It does NOT accept organisation_id as a parameter.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import func, select
from sqlalchemy.sql import Select

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealStatus

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


@dataclass
class DealFilters:
    """Filter parameters for deal listing."""

    name: str | None = None
    status: str | None = None
    stage_name: str | None = None
    min_amount: float | None = None
    max_amount: float | None = None
    owner_email: str | None = None
    company_name: str | None = None
    sort_by: str = "source_modified_at"
    sort_order: str = "desc"
    limit: int = 20
    offset: int = 0


def _apply_filters(stmt: Select, filters: DealFilters, table_columns: Any) -> Select:
    """Apply filter conditions to the query.

    Args:
        stmt: SQLAlchemy select statement
        filters: DealFilters dataclass with filter values
        table_columns: Table columns for building conditions

    Returns:
        Modified select statement with filters applied.
    """
    c = table_columns

    if filters.name:
        stmt = stmt.where(func.lower(c.name).like(func.lower(f"%{filters.name}%")))

    if filters.status:
        stmt = _apply_status_filter(stmt, filters.status, c)

    if filters.stage_name:
        stmt = stmt.where(func.lower(c.stage_name) == func.lower(filters.stage_name))

    if filters.min_amount is not None:
        stmt = stmt.where(c.amount >= filters.min_amount)

    if filters.max_amount is not None:
        stmt = stmt.where(c.amount <= filters.max_amount)

    if filters.owner_email:
        stmt = stmt.where(func.lower(c.owner_email) == func.lower(filters.owner_email))

    if filters.company_name:
        stmt = stmt.where(func.lower(c.company_name).like(func.lower(f"%{filters.company_name}%")))

    return stmt


def _apply_status_filter(stmt: Select, status: str, c: Any) -> Select:
    """Apply status filter to query."""
    status_lower = status.lower()
    if status_lower == "open":
        return stmt.where(c.deal_status == CRMDealStatus.OPEN.value)
    elif status_lower == "closed":
        return stmt.where(c.deal_status.in_([CRMDealStatus.WON.value, CRMDealStatus.LOST.value]))
    elif status_lower == "won":
        return stmt.where(c.deal_status == CRMDealStatus.WON.value)
    elif status_lower == "lost":
        return stmt.where(c.deal_status == CRMDealStatus.LOST.value)
    return stmt


def _apply_sorting(stmt: Select, filters: DealFilters, table_columns: Any) -> Select:
    """Apply sorting to the query."""
    c = table_columns
    sort_column_map = {
        "amount": c.amount,
        "created_at": c.source_created_at,
        "close_date": c.expected_close_date,
        "updated_at": c.source_modified_at,
        "source_modified_at": c.source_modified_at,
        "name": c.name,
    }
    sort_col = sort_column_map.get(filters.sort_by, c.source_modified_at)

    if filters.sort_order.lower() == "asc":
        return stmt.order_by(sort_col.asc().nulls_last())
    return stmt.order_by(sort_col.desc().nulls_last())


def _format_deal_output(deal: CRMDeal) -> dict[str, Any]:
    """Format a single deal for output."""
    return {
        "deal_id": str(deal.deal_id),
        "name": deal.name,
        "company_name": deal.company_name,
        "amount": float(deal.amount) if deal.amount else None,
        "currency": deal.currency,
        "stage_name": deal.stage_name,
        "stage_label": deal.stage_label,
        "deal_status": deal.deal_status,
        "probability": deal.probability,
        "owner_name": deal.owner_name,
        "owner_email": deal.owner_email,
        "expected_close_date": deal.expected_close_date.isoformat()
        if deal.expected_close_date
        else None,
        "created_at": deal.source_created_at.isoformat() if deal.source_created_at else None,
        "updated_at": deal.source_modified_at.isoformat() if deal.source_modified_at else None,
        "last_activity_at": deal.last_activity_at.isoformat() if deal.last_activity_at else None,
    }


async def list_deals(
    *,
    context: AgentContext,
    filters: DealFilters,
) -> list[dict[str, Any]]:
    """List deals with flexible filtering and sorting.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access

    Args:
        context: AgentContext containing organisation metadata
        filters: DealFilters dataclass with filter/sort/pagination parameters

    Returns:
        List of deal dictionaries with summary information.
    """
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("list_deals: missing organisation_id in context")
        return []

    effective_limit = max(1, min(filters.limit, 100))
    effective_offset = max(0, filters.offset)

    c = cast(Any, CRMDeal).__table__.c

    # Base query
    stmt = select(CRMDeal).where(
        c.organisation_id == organisation_id,
        c.is_deleted.is_(False),
    )

    # Apply filters and sorting
    stmt = _apply_filters(stmt, filters, c)
    stmt = _apply_sorting(stmt, filters, c)
    stmt = stmt.offset(effective_offset).limit(effective_limit)

    async with context.acquire_session() as session:
        try:
            result = await session.execute(stmt)
            deals = result.scalars().all()
        except Exception as e:
            logger.exception(f"list_deals: database error: {e}")
            return []

        output = [_format_deal_output(deal) for deal in deals]

    logger.info("list_deals: found %d deals", len(output))
    return output


def create_list_deals(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create list_deals tool bound to a context.

    SECURITY: organisation_id comes from context (immutable).
    """

    async def list_deals_tool(
        name: str | None = None,
        status: str | None = None,
        stage_name: str | None = None,
        min_amount: float | None = None,
        max_amount: float | None = None,
        owner_email: str | None = None,
        company_name: str | None = None,
        sort_by: str = "updated_at",
        sort_order: str = "desc",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List deals with filtering and sorting.

        Use this tool to browse deals with various filters.

        Args:
            name: Filter by deal name (partial match)
            status: Filter: "open", "closed", "won", "lost"
            stage_name: Filter by stage name
            min_amount: Filter: deal amount >= value
            max_amount: Filter: deal amount <= value
            owner_email: Filter by deal owner email
            company_name: Filter by company name (partial match)
            sort_by: Sort by: "amount", "created_at", "close_date", "updated_at", "name"
            sort_order: "asc" or "desc"
            limit: Max results (default: 20, max: 100)

        Examples:
        - "Show open deals over $100K" → status="open", min_amount=100000
        - "List deals with Acme" → company_name="Acme"
        - "What deals does john@company.com own?" → owner_email="john@company.com"
        """
        logger.info(
            "[TOOL CALLED] list_deals - status=%s, min_amount=%s, company_name=%s, limit=%d",
            status,
            min_amount,
            company_name,
            limit,
        )

        if not context.organisation_id:
            logger.warning("list_deals unavailable: missing organisation_id in context")
            return []

        filters = DealFilters(
            name=name,
            status=status,
            stage_name=stage_name,
            min_amount=min_amount,
            max_amount=max_amount,
            owner_email=owner_email,
            company_name=company_name,
            sort_by=sort_by,
            sort_order=sort_order,
            limit=limit,
            offset=0,
        )

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="ListDeals",
            tool_func=list_deals,
            parameters={
                "context": context,
                "filters": filters,
            },
            description="Listing deals with filters",
            fallback_value=[],
        )

        logger.info("[TOOL RESULT] list_deals returned %d deals", len(result) if result else 0)
        return list(result) if result else []

    return list_deals_tool


__all__ = [
    "list_deals",
    "create_list_deals",
    "DealFilters",
]
