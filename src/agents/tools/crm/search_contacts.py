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
from src.agents.tools.crm.access import AgentCRMAccessError, resolve_agent_access_actor
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import ApprovalStatus
from src.service.crm.access.record_access_policy import DealAccessPolicy
from src.service.crm.contacts.read_service import ContactReadService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)
MAX_CONTACT_QUERY_CHARS = 200


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
    if any(
        value is not None and (not value.strip() or len(value.strip()) > MAX_CONTACT_QUERY_CHARS)
        for value in (query, company_name)
    ):
        logger.warning("search_contacts: identifying query must be 1-%d characters", MAX_CONTACT_QUERY_CHARS)
        return []

    effective_limit = max(1, min(limit, 50))

    participant_c = cast(Any, CRMDealParticipant).__table__.c
    deal_c = cast(Any, CRMDeal).__table__.c

    async with context.acquire_session() as session:
        try:
            actor = await resolve_agent_access_actor(context)
            rows = await ContactReadService(
                session,
                organisation_id,
            ).search_contacts_for_agent(
                q=query,
                company_name=company_name,
                limit=effective_limit,
            )
            contacts = [row["contact"] for row in rows]
            match_by_contact_id = {row["contact"].contact_id: row.get("match") for row in rows}
        except AgentCRMAccessError:
            logger.warning("search_contacts: authenticated CRM actor unavailable")
            return []
        except Exception as e:
            logger.exception(f"search_contacts: database error: {e}")
            return []

        if not contacts:
            logger.info("search_contacts: no contacts found for query=%s", query)
            return []

        # Deal rows are separately access-filtered; finding an org contact does
        # not grant visibility into every deal that references that person.
        contact_ids = [c.contact_id for c in contacts]
        email_to_contact_id = {
            c.email.strip().lower(): c.contact_id for c in contacts if c.email and c.email.strip()
        }
        participant_match_filters = [participant_c.related_local_id.in_(contact_ids)]
        if email_to_contact_id:
            participant_match_filters.append(
                func.lower(func.trim(participant_c.related_email)).in_(
                    list(email_to_contact_id.keys())
                )
            )

        participant_stmt = (
            select(CRMDealParticipant)
            .options(selectinload(cast(Any, CRMDealParticipant).deal))
            .join(CRMDeal, deal_c.deal_id == participant_c.deal_id)
            .where(
                participant_c.organisation_id == organisation_id,
                deal_c.organisation_id == organisation_id,
                participant_c.participant_type == CRMParticipantType.CONTACT.value,
                participant_c.approval_status == ApprovalStatus.APPROVED.value,
                participant_c.is_deleted.is_(False),
                or_(*participant_match_filters),
                DealAccessPolicy(
                    session,
                    organisation_id,
                    actor,
                ).visible_deal_predicate(deal_c),
            )
        )

        try:
            participant_result = await session.execute(participant_stmt)
            participants = participant_result.scalars().all()
        except Exception as e:
            logger.exception(f"search_contacts: error fetching stakeholders: {e}")
            participants = []

        # Group participant-backed stakeholder rows by contact_id
        contact_deals: dict[Any, list[dict[str, Any]]] = {}
        for participant in participants:
            contact_id = participant.related_local_id
            if contact_id is None and participant.related_email:
                contact_id = email_to_contact_id.get(participant.related_email.strip().lower())
            if contact_id is None:
                continue

            if contact_id not in contact_deals:
                contact_deals[contact_id] = []
            if participant.deal:
                contact_deals[contact_id].append(
                    {
                        "deal_id": str(participant.deal.deal_id),
                        "name": participant.deal.name,
                        "company_name": participant.deal.company_name,
                        "role": participant.decision_power,
                        "stance": participant.stance,
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
                    "match": match_by_contact_id.get(contact.contact_id),
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

        if any(
            value is not None
            and (not value.strip() or len(value.strip()) > MAX_CONTACT_QUERY_CHARS)
            for value in (query, company_name)
        ):
            logger.warning(
                "search_contacts: identifying query must be 1-%d characters",
                MAX_CONTACT_QUERY_CHARS,
            )
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
            description="Searching contacts",
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
