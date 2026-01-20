"""Search Contacts Tool - Search for contacts across all deals.

This tool searches for contacts by name, email, or company across the organization.
Returns contact info along with their associated deals.

USE CASES:
- "Find John from Acme" → query="John", company_name="Acme"
- "Search for jane@example.com" → query="jane@example.com"
- "Who are the contacts at TechCorp?" → company_name="TechCorp"

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access

This is an ORG-SCOPED tool. organisation_id comes from context.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    DealStakeholder,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def search_contacts(
    *,
    context: AgentContext,
    query: str | None = None,
    company_name: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search for contacts across all deals.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access

    Args:
        context: AgentContext containing organisation metadata
        query: Search term - matches name, email (case-insensitive, partial match)
        company_name: Filter by company name (partial match)
        limit: Maximum number of contacts to return (default: 20)

    Returns:
        List of contact dictionaries with deal associations.
    """
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("search_contacts: missing organisation_id in context")
        return []

    if not query and not company_name:
        logger.warning("search_contacts: at least query or company_name required")
        return []

    effective_limit = max(1, min(limit, 50))

    # Table columns for type-safe queries
    contact_c = cast(Any, CRMContact).__table__.c
    stakeholder_c = cast(Any, DealStakeholder).__table__.c

    # Build contact query
    stmt = select(CRMContact).where(
        contact_c.organisation_id == organisation_id,
        contact_c.is_deleted.is_(False),
    )

    # Apply search filter
    if query:
        search_term = f"%{query.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(contact_c.full_name).like(search_term),
                func.lower(contact_c.first_name).like(search_term),
                func.lower(contact_c.last_name).like(search_term),
                func.lower(contact_c.email).like(search_term),
            )
        )

    # Apply company filter
    if company_name:
        company_term = f"%{company_name.lower()}%"
        stmt = stmt.where(func.lower(contact_c.company_name).like(company_term))

    # Order by last activity (most recent first)
    stmt = stmt.order_by(contact_c.last_activity_at.desc().nullslast())
    stmt = stmt.limit(effective_limit)

    async with context.acquire_session() as session:
        try:
            result = await session.execute(stmt)
            contacts = result.scalars().all()
        except Exception as e:
            logger.exception(f"search_contacts: database error: {e}")
            return []

        if not contacts:
            logger.info("search_contacts: no contacts found for query=%s", query)
            return []

        # Get deal associations for these contacts
        contact_ids = [c.contact_id for c in contacts]

        stakeholder_stmt = (
            select(DealStakeholder)
            .options(selectinload(cast(Any, DealStakeholder).deal))
            .where(
                stakeholder_c.organisation_id == organisation_id,
                stakeholder_c.contact_id.in_(contact_ids),
                stakeholder_c.approval_status == ApprovalStatus.APPROVED,
                stakeholder_c.is_deleted.is_(False),
            )
        )

        try:
            stakeholder_result = await session.execute(stakeholder_stmt)
            stakeholders = stakeholder_result.scalars().all()
        except Exception as e:
            logger.exception(f"search_contacts: error fetching stakeholders: {e}")
            stakeholders = []

        # Group stakeholders by contact_id
        contact_deals: dict[Any, list[dict[str, Any]]] = {}
        for s in stakeholders:
            if s.contact_id not in contact_deals:
                contact_deals[s.contact_id] = []
            if s.deal:
                contact_deals[s.contact_id].append(
                    {
                        "deal_id": str(s.deal.deal_id),
                        "name": s.deal.name,
                        "company_name": s.deal.company_name,
                        "role": s.decision_power.value if s.decision_power else None,
                        "stance": s.stance.value if s.stance else None,
                    }
                )

        # Build output
        output: list[dict[str, Any]] = []
        for contact in contacts:
            deals = contact_deals.get(contact.contact_id, [])
            output.append(
                {
                    "contact_id": str(contact.contact_id),
                    "email": contact.email,
                    "name": contact.full_name
                    or f"{contact.first_name or ''} {contact.last_name or ''}".strip(),
                    "first_name": contact.first_name,
                    "last_name": contact.last_name,
                    "job_title": contact.job_title,
                    "company_name": contact.company_name,
                    "phone": contact.phone,
                    "deals": deals,
                    "deal_count": len(deals),
                    "last_contacted_at": (
                        contact.last_contacted_at.isoformat() if contact.last_contacted_at else None
                    ),
                    "last_activity_at": (
                        contact.last_activity_at.isoformat() if contact.last_activity_at else None
                    ),
                }
            )

    logger.info(
        "search_contacts: found %d contacts for query=%s, company=%s",
        len(output),
        query,
        company_name,
    )
    return output


def create_search_contacts(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create search_contacts tool bound to a context.

    SECURITY: organisation_id comes from context (immutable).
    """

    async def search_contacts_tool(
        query: str | None = None,
        company_name: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search for contacts across all deals.

        Use this tool to find contacts by name, email, or company.
        Returns contact info along with their associated deals.

        Args:
            query: Search by name or email (case-insensitive, partial match)
            company_name: Filter by company name (partial match)
            limit: Maximum results (default: 20, max: 50)

        At least one of query or company_name must be provided.

        Returns list of contacts with:
        - contact_id, email, name, job_title, company_name, phone
        - deals: List of associated deals with role and stance
        - deal_count: Number of associated deals
        - last_contacted_at, last_activity_at
        """
        logger.info("[TOOL CALLED] search_contacts - query=%s, company=%s", query, company_name)

        if not context.organisation_id:
            logger.warning("search_contacts unavailable: missing organisation_id")
            return []

        if not query and not company_name:
            logger.warning("search_contacts: at least query or company_name required")
            return []

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="SearchContacts",
            tool_func=search_contacts,
            parameters={
                "context": context,
                "query": query,
                "company_name": company_name,
                "limit": limit,
            },
            description=f"Searching contacts: query={query}, company={company_name}",
            fallback_value=[],
        )

        logger.info(
            "[TOOL RESULT] search_contacts returned %d contacts",
            len(result) if result else 0,
        )
        return list(result) if result else []

    return search_contacts_tool


__all__ = [
    "search_contacts",
    "create_search_contacts",
]
