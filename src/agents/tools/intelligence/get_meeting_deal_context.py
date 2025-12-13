"""Get deal context for a meeting.

Given a calendar event id, return the associated deal metadata (if any).
This enables tool-first agents to start from an event_id and discover the
relevant deal context without the handler pre-loading it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select

from src.agents.tools.base import db_session_lock, execute_with_capabilities
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarEventDealAssociation,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def get_meeting_deal_context(
    event_id: str,
    *,
    context: AgentContext,
) -> dict[str, Any] | None:
    """Return deal context for a meeting (best-effort).

    Args:
        event_id: Calendar event UUID as string
        context: AgentContext containing session and organisation metadata

    Returns:
        Dict with deal_id/deal_name/company_name/deal_stage if found, else None.
    """
    session = context.session
    organisation_id = context.organisation_id

    if not session or not organisation_id:
        logger.warning("get_meeting_deal_context: missing session or organisation_id")
        return None

    try:
        parsed_event_id = UUID(event_id)
    except ValueError:
        logger.warning("get_meeting_deal_context: invalid event_id format: %s", event_id)
        return None

    assoc_c = cast(Any, CalendarEventDealAssociation).__table__.c
    deal_c = cast(Any, CRMDeal).__table__.c

    stmt = (
        select(
            assoc_c.deal_id,
            deal_c.name,
            deal_c.company_name,
            deal_c.stage_label,
            deal_c.stage_name,
        )
        .select_from(CalendarEventDealAssociation)
        .join(CRMDeal, deal_c.deal_id == assoc_c.deal_id)
        .where(
            assoc_c.organisation_id == organisation_id,
            assoc_c.event_id == parsed_event_id,
            assoc_c.status != AssociationStatus.REJECTED,
        )
        .limit(1)
    )

    try:
        result = await session.execute(stmt)
        row = result.first()
    except Exception:
        logger.exception("get_meeting_deal_context: error fetching deal context")
        return None

    if not row:
        return None

    deal_id, deal_name, company_name, stage_label, stage_name = row
    deal_stage = stage_label or stage_name

    return {
        "event_id": event_id,
        "deal_id": str(deal_id) if deal_id else None,
        "deal_name": deal_name,
        "company_name": company_name,
        "deal_stage": deal_stage,
    }


def create_get_meeting_deal_context(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create get_meeting_deal_context tool bound to a context."""

    async def get_meeting_deal_context_tool(event_id: str) -> dict[str, Any] | None:
        """Get deal metadata associated with this meeting.

        Use this tool when you only have an event_id and need to discover:
        - which deal the meeting belongs to
        - deal name/company/stage (best-effort)

        Args:
            event_id: Calendar event UUID

        Returns:
            Deal context dict, or None.
        """
        logger.info(f"[TOOL CALLED] get_meeting_deal_context - event_id='{event_id}'")

        if not context.session or not context.organisation_id:
            logger.warning(
                "get_meeting_deal_context unavailable: missing session or organisation_id"
            )
            return None

        async with db_session_lock():
            result = await execute_with_capabilities(
                capabilities_getter(),
                tool_name="GetMeetingDealContext",
                tool_func=get_meeting_deal_context,
                parameters={
                    "event_id": event_id,
                    "context": context,
                },
                description=f"Getting deal context for meeting: {event_id}",
                fallback_value=None,
            )

        if result:
            logger.info(
                f"[TOOL RESULT] get_meeting_deal_context found deal: {result.get('deal_name', 'Unknown')}"
            )
        else:
            logger.info("[TOOL RESULT] get_meeting_deal_context returned None (not found)")

        return dict(result) if result else None

    return get_meeting_deal_context_tool


__all__ = ["create_get_meeting_deal_context"]
