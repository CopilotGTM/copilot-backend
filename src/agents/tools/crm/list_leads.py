"""List Leads agent tool."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.agents.tools.base import execute_with_capabilities
from src.agents.tools.crm.access import AgentCRMAccessError, resolve_agent_access_actor
from src.domain.models.organisation_tables.sync.crm.leads import CRMLeadSourceType, CRMLeadStatus
from src.service.crm.leads.contracts import LeadServiceError
from src.service.crm.leads.read_service import LeadReadService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


@dataclass
class LeadFilters:
    q: str | None = None
    status: CRMLeadStatus | None = None
    owner_id: UUID | None = None
    source_type: CRMLeadSourceType | None = None
    limit: int = 20


def _parse_status(value: CRMLeadStatus | str | None) -> CRMLeadStatus | None:
    if value is None or isinstance(value, CRMLeadStatus):
        return value
    cleaned = value.strip().lower()
    if not cleaned:
        return None
    try:
        return CRMLeadStatus(cleaned)
    except ValueError:
        logger.warning("list_leads: invalid status ignored: %s", value)
        return None


def _parse_source_type(value: CRMLeadSourceType | str | None) -> CRMLeadSourceType | None:
    if value is None or isinstance(value, CRMLeadSourceType):
        return value
    cleaned = value.strip().lower()
    if not cleaned:
        return None
    try:
        return CRMLeadSourceType(cleaned)
    except ValueError:
        logger.warning("list_leads: invalid source_type ignored: %s", value)
        return None


def _parse_uuid(value: UUID | str | None) -> UUID | None:
    if value is None or isinstance(value, UUID):
        return value
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        return UUID(cleaned)
    except ValueError:
        logger.warning("list_leads: invalid owner_id ignored: %s", value)
        return None


def _format_lead_output(lead: Any) -> dict[str, Any]:
    return {
        "lead_id": str(lead.lead_id),
        "status": lead.status.value if hasattr(lead.status, "value") else lead.status,
        "source_type": lead.source_type.value
        if hasattr(lead.source_type, "value")
        else lead.source_type,
        "source_channel": lead.source_channel,
        "name": lead.full_name,
        "email": lead.email,
        "phone": lead.phone,
        "job_title": lead.job_title,
        "company_name": lead.company_name,
        "company_domain": lead.company_domain,
        "message": lead.message,
        "interest": lead.interest,
        "owner_id": str(lead.owner_id) if lead.owner_id else None,
        "owner_name": lead.owner_name,
        "owner_email": lead.owner_email,
        "created_at": lead.created_at.isoformat() if lead.created_at else None,
        "captured_at": lead.captured_at.isoformat() if lead.captured_at else None,
        "updated_at": lead.updated_at.isoformat() if lead.updated_at else None,
    }


async def list_leads(
    *,
    context: AgentContext,
    filters: LeadFilters,
) -> list[dict[str, Any]]:
    """List leads with org-scoped filtering."""
    organisation_id = context.organisation_id
    if not organisation_id:
        logger.warning("list_leads: missing organisation_id in context")
        return []

    async with context.acquire_session() as org_session:
        try:
            access_actor = await resolve_agent_access_actor(context)
        except AgentCRMAccessError:
            logger.warning("list_leads: authenticated CRM actor unavailable")
            return []
        service = LeadReadService(
            org_session,
            organisation_id,
            actor_user_id=context.user_id,
            master_session=None,
            access_actor=access_actor,
        )
        try:
            leads, _total_count = await service.list_leads(
                page=1,
                limit=max(1, min(filters.limit, 100)),
                q=filters.q,
                status=filters.status,
                owner_id=filters.owner_id,
                source_type=filters.source_type,
            )
        except LeadServiceError as exc:
            logger.warning("list_leads: service error: %s", exc.message)
            return []
        except Exception:
            logger.exception("list_leads: unexpected failure")
            return []

    return [_format_lead_output(lead) for lead in leads]


def create_list_leads(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create the list_leads tool bound to context."""

    async def list_leads_tool(
        q: str | None = None,
        status: str | None = None,
        owner_id: str | None = None,
        source_type: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List CRM leads with optional search, status, owner, and source filters."""
        filters = LeadFilters(
            q=q.strip() if isinstance(q, str) and q.strip() else None,
            status=_parse_status(status),
            owner_id=_parse_uuid(owner_id),
            source_type=_parse_source_type(source_type),
            limit=limit,
        )
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="ListLeads",
            tool_func=list_leads,
            parameters={"context": context, "filters": filters},
            description="Searching leads",
            fallback_value=[],
        )
        return list(result) if result else []

    return list_leads_tool


__all__ = ["LeadFilters", "list_leads", "create_list_leads"]
