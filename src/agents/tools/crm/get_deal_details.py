"""Get Deal Details Tool - Comprehensive information for a single deal.

This tool retrieves full details for a specific deal including all metadata,
stakeholders, and active risks/blockers.

USE CASE:
After using search_deals to find a deal_id, use this tool to get complete
deal information for answering user questions.

RETURNS:
- Basic info: name, description, company, amount, currency
- Status and stage information
- Pipeline information
- Owner and primary contact details
- All dates (created, updated, close dates, etc.)
- Deal summary (AI-generated if available)
- Stakeholders (optional, default: included)
- Active risks/blockers (optional, default: included)

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access

This is an ORG-SCOPED tool. deal_id is a parameter, but organisation_id
comes from context to ensure users can only access their own deals.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select

from src.agents.tools.base import execute_with_capabilities
from src.agents.tools.crm.access import AgentCRMAccessError, require_agent_deal_access
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMParticipantType,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def get_deal_details(
    deal_id: str,
    *,
    context: AgentContext,
    include_stakeholders: bool = True,
    include_risks: bool = True,
) -> dict[str, Any] | None:
    """Get comprehensive details for a single deal.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access

    Args:
        deal_id: The deal UUID as a string
        context: AgentContext containing organisation metadata
        include_stakeholders: Include stakeholder summary (default: True)
        include_risks: Include active risks/blockers (default: True)

    Returns:
        Deal dictionary with full details, or None if not found.
    """
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("get_deal_details: missing organisation_id in context")
        return None

    try:
        parsed_deal_id = UUID(deal_id)
    except ValueError:
        logger.warning("get_deal_details: invalid deal_id format: %s", deal_id)
        return None

    c = cast(Any, CRMDeal).__table__.c

    stmt = select(CRMDeal).where(
        c.organisation_id == organisation_id,
        c.deal_id == parsed_deal_id,
        c.is_deleted.is_(False),
    )

    async with context.acquire_session() as session:
        try:
            await require_agent_deal_access(context, session, parsed_deal_id)
            result = await session.execute(stmt)
            deal = result.scalar_one_or_none()
        except AgentCRMAccessError:
            logger.info("get_deal_details: deal not visible to agent actor")
            return None
        except Exception as e:
            logger.exception(f"get_deal_details: database error: {e}")
            return None

        if not deal:
            logger.info("get_deal_details: deal not found: %s", deal_id)
            return None

        company_industry = None
        company_website = None
        company_linkedin_url = None
        company_country = None
        if deal.company_id:
            company_c = cast(Any, CRMCompany).__table__.c
            company_stmt = select(CRMCompany).where(
                company_c.organisation_id == organisation_id,
                company_c.company_id == deal.company_id,
                company_c.is_deleted.is_(False),
            )
            try:
                company_result = await session.execute(company_stmt)
                company = company_result.scalar_one_or_none()
            except Exception as e:
                logger.warning("get_deal_details: error fetching company profile: %s", e)
                company = None
            if company:
                company_industry = getattr(company, "industry", None)
                company_website = getattr(company, "website", None)
                company_linkedin_url = getattr(company, "linkedin_url", None)
                company_country = getattr(company, "country", None)

        output: dict[str, Any] = {
            "deal_id": str(deal.deal_id),
            "name": deal.name,
            "description": deal.description,
            "company_id": str(deal.company_id) if deal.company_id else None,
            "company_name": deal.company_name,
            "company_domain": deal.company_domain,
            "company_industry": company_industry,
            "company_website": company_website,
            "company_linkedin_url": company_linkedin_url,
            "company_country": company_country,
            "amount": float(deal.amount) if deal.amount is not None else None,
            "predicted_amount": float(deal.predicted_amount)
            if deal.predicted_amount is not None
            else None,
            "currency": deal.currency,
            "deal_status": deal.deal_status,
            "forecast_category": deal.forecast_category,
            "stage": {
                "stage_id": str(deal.stage_id) if deal.stage_id else None,
                "name": deal.stage_name,
                "label": deal.stage_label,
                "probability": deal.probability,
                "entered_at": deal.stage_entered_at.isoformat() if deal.stage_entered_at else None,
                "updated_at": deal.stage_updated_at.isoformat() if deal.stage_updated_at else None,
            },
            "pipeline": {
                "pipeline_id": str(deal.pipeline_id) if deal.pipeline_id else None,
                "name": deal.pipeline_name,
                "label": deal.pipeline_label,
            },
            "owner": {
                "owner_user_id": str(deal.owner_id) if deal.owner_id else None,
                "name": deal.owner_name,
                "email": deal.owner_email,
            },
            "primary_contact": {
                "primary_contact_id": (
                    str(deal.primary_contact_id) if deal.primary_contact_id else None
                ),
                "name": deal.primary_contact_name,
                "email": deal.primary_contact_email,
            },
            "dates": {
                "created_at": deal.source_created_at.isoformat()
                if deal.source_created_at
                else None,
                "updated_at": deal.source_modified_at.isoformat()
                if deal.source_modified_at
                else None,
                "expected_close_date": deal.expected_close_date.isoformat()
                if deal.expected_close_date
                else None,
                "close_date": deal.close_date.isoformat() if deal.close_date else None,
                "closed_won_at": deal.closed_won_at.isoformat() if deal.closed_won_at else None,
                "closed_lost_at": deal.closed_lost_at.isoformat() if deal.closed_lost_at else None,
                "last_activity_at": deal.last_activity_at.isoformat()
                if deal.last_activity_at
                else None,
            },
            "closed_reason": deal.closed_reason,
            "deal_type": deal.deal_type,
            "lead_source": deal.lead_source,
            "priority": deal.priority,
            "crm_url": deal.crm_url,
            "metrics": {
                "associated_contact_count": deal.associated_contact_count,
                "associated_company_count": deal.associated_company_count,
                "note_count": deal.note_count,
            },
            "deal_summary": deal.deal_summary_data,
        }

        # Fetch stakeholders if requested
        if include_stakeholders:
            participant_c = cast(Any, CRMDealParticipant).__table__.c
            stakeholder_stmt = (
                select(CRMDealParticipant)
                .where(
                    participant_c.organisation_id == organisation_id,
                    participant_c.deal_id == parsed_deal_id,
                    participant_c.participant_type == CRMParticipantType.CONTACT.value,
                    participant_c.is_deleted.is_(False),
                )
                .limit(10)
            )
            try:
                stakeholder_result = await session.execute(stakeholder_stmt)
                participants = stakeholder_result.scalars().all()
                contact_ids = {
                    participant.related_local_id
                    for participant in participants
                    if participant.related_local_id
                }
                contacts_by_id: dict[UUID, CRMContact] = {}
                if contact_ids:
                    contact_c = cast(Any, CRMContact).__table__.c
                    contact_stmt = select(CRMContact).where(
                        contact_c.organisation_id == organisation_id,
                        contact_c.contact_id.in_(contact_ids),
                        contact_c.is_deleted.is_(False),
                    )
                    contact_result = await session.execute(contact_stmt)
                    contacts_by_id = {
                        contact.contact_id: contact for contact in contact_result.scalars().all()
                    }
                output["stakeholders"] = [
                    _deal_detail_stakeholder_summary(
                        participant,
                        contacts_by_id.get(participant.related_local_id),
                    )
                    for participant in participants
                ]
            except Exception as e:
                logger.warning("get_deal_details: error fetching stakeholders: %s", e)
                output["stakeholders"] = []

        # Fetch risks/blockers if requested
        if include_risks:
            from src.domain.models.organisation_tables.sync.risk_blocker_models import (
                DealRiskBlocker,
                RiskBlockerStatus,
            )

            risk_c = cast(Any, DealRiskBlocker).__table__.c
            risk_stmt = (
                select(DealRiskBlocker)
                .where(
                    risk_c.organisation_id == organisation_id,
                    risk_c.deal_id == parsed_deal_id,
                    risk_c.status != RiskBlockerStatus.DISMISSED.value,
                )
                .order_by(risk_c.created_at.desc())
                .limit(10)
            )
            try:
                risk_result = await session.execute(risk_stmt)
                risks = risk_result.scalars().all()
                output["risks_blockers"] = [
                    {
                        "risk_blocker_id": str(r.risk_blocker_id),
                        "item_type": r.item_type.value
                        if hasattr(r.item_type, "value")
                        else r.item_type,
                        "title": r.title,
                        "severity": r.severity.value
                        if hasattr(r.severity, "value")
                        else r.severity,
                        "status": r.status.value if hasattr(r.status, "value") else r.status,
                    }
                    for r in risks
                ]
            except Exception as e:
                logger.warning("get_deal_details: error fetching risks: %s", e)
                output["risks_blockers"] = []

    logger.info("get_deal_details: found deal %s", deal_id)
    return output


def _deal_detail_stakeholder_summary(
    participant: CRMDealParticipant,
    contact: CRMContact | None,
) -> dict[str, Any]:
    """Render stakeholder summary fields from contact-owned profile data."""
    return {
        "participant_id": str(participant.participant_id),
        "name": contact.full_name if contact and contact.full_name else participant.related_name,
        "email": contact.email if contact and contact.email else participant.related_email,
        "job_title": contact.job_title
        if contact and contact.job_title
        else getattr(participant, "job_title", None),
        "decision_power": participant.decision_power,
        "stance": participant.stance,
    }


def create_get_deal_details(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create get_deal_details tool bound to a context.

    SECURITY: organisation_id comes from context; deal_id falls back to the
    immutable deal scope and is still checked by the shared access policy.
    """

    async def get_deal_details_tool(
        deal_id: str | None = None,
        include_stakeholders: bool = True,
        include_risks: bool = True,
    ) -> dict[str, Any] | None:
        """Get comprehensive details for a deal.

        Use this tool after search_deals to get full deal information.

        Args:
            deal_id: Visible deal UUID. If omitted, uses the current deal context.
            include_stakeholders: Include stakeholder summary (default: True)
            include_risks: Include active risks/blockers (default: True)

        Returns full deal info including:
        - Basic info: name, company, amount, status, stage
        - Owner and primary contact
        - All dates (created, updated, close dates)
        - Deal summary (AI-generated if available)
        - Stakeholders (if include_stakeholders=True)
        - Risks/blockers (if include_risks=True)
        """
        effective_deal_id = deal_id or (str(context.deal_id) if context.deal_id else None)
        logger.info("[TOOL CALLED] get_deal_details - deal_id='%s'", effective_deal_id)

        if not context.organisation_id or not effective_deal_id:
            logger.warning("get_deal_details unavailable: missing organisation or deal context")
            return None

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="GetDealDetails",
            tool_func=get_deal_details,
            parameters={
                "deal_id": effective_deal_id,
                "context": context,
                "include_stakeholders": include_stakeholders,
                "include_risks": include_risks,
            },
            description="Looking up deal details",
            fallback_value=None,
        )

        if result:
            logger.info(
                "[TOOL RESULT] get_deal_details found deal: %s", result.get("name", "Unknown")
            )
        else:
            logger.info("[TOOL RESULT] get_deal_details returned None (not found)")

        return dict(result) if result else None

    return get_deal_details_tool


__all__ = [
    "get_deal_details",
    "create_get_deal_details",
]
