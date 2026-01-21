"""List deal notes tool for agents.

Tool for listing CRM notes (activity_type=NOTE) associated with a specific deal.

Notes are stored in the organisation DB in the canonical CRM activities table
(`crm_activities`) as `CRMActivity` records.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access
- deal_id: Prevents cross-deal data access (deal-scoped tool)

This is a DEAL-SCOPED tool. It does NOT accept organisation_id or deal_id
as parameters - both come from context.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.crm.activities import CRMActivity, CRMActivityType

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def list_deal_notes(
    *,
    context: AgentContext,
    limit: int = 20,
    include_body: bool = True,
    max_body_length: int | None = 5000,
    include_properties: bool = False,
) -> list[dict[str, Any]]:
    """List CRM notes associated with a deal.

    Notes are represented as CRM activities with `activity_type == NOTE`.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access
    - deal_id: Prevents cross-deal data access

    Args:
        context: AgentContext containing organisation and deal metadata
        limit: Maximum number of notes to return (default: 20, max: 100)
        include_body: Include note body content (default: True)
        max_body_length: Truncate note body to N characters (None = no limit)
        include_properties: Include provider properties dict (default: False)

    Returns:
        List of note dictionaries.
    """
    # SECURITY: Both organisation_id and deal_id come from context, not parameters
    organisation_id = context.organisation_id
    deal_id = context.deal_id

    if not organisation_id:
        logger.warning("list_deal_notes: missing organisation_id in context")
        return []

    if not deal_id:
        logger.warning("list_deal_notes: missing deal_id in context")
        return []

    effective_limit = max(1, min(limit, 100))

    c = cast(Any, CRMActivity).__table__.c

    # SECURITY: Both organisation_id and deal_id from context (immutable)
    stmt = (
        select(CRMActivity)
        .where(
            c.organisation_id == organisation_id,
            c.deal_id == deal_id,
            c.activity_type == CRMActivityType.NOTE.value,
            c.is_deleted.is_(False),
        )
        .order_by(c.source_created_at.desc(), c.synced_at.desc())
        .limit(effective_limit)
    )

    # Acquire fresh session for database operations
    async with context.acquire_session() as session:
        try:
            result = await session.execute(stmt)
            notes = result.scalars().all()
        except Exception as e:
            logger.exception(f"list_deal_notes: database error: {e}")
            return []

        # Transform to output format (while session is active)
        output: list[dict[str, Any]] = []
        for n in notes:
            note_body: str | None = None
            if include_body:
                note_body = n.body
                if note_body and max_body_length and len(note_body) > max_body_length:
                    note_body = note_body[:max_body_length]

            item: dict[str, Any] = {
                "activity_id": str(n.activity_id),
                "external_id": n.external_id,
                "subject": n.subject,
                "body": note_body,
                "owner": {
                    "external_id": n.owner_external_id,
                    "name": n.owner_name,
                    "email": n.owner_email,
                },
                "source_created_at": n.source_created_at.isoformat()
                if n.source_created_at
                else None,
                "source_modified_at": n.source_modified_at.isoformat()
                if n.source_modified_at
                else None,
                "synced_at": n.synced_at.isoformat() if n.synced_at else None,
            }

            if include_properties:
                item["properties"] = n.properties or {}

            output.append(item)
    # Session released

    logger.info("list_deal_notes: returning %d notes for deal %s", len(output), deal_id)
    return output


def create_list_deal_notes(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create list_deal_notes tool bound to a context.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access
    - deal_id: Prevents cross-deal data access

    This is a DEAL-SCOPED tool. It does NOT accept organisation_id or deal_id
    as parameters - both come from context.
    """

    async def list_deal_notes_tool(
        limit: int = 20,
        include_body: bool = True,
        max_body_length: int | None = 5000,
        include_properties: bool = False,
    ) -> list[dict[str, Any]]:
        """List CRM notes associated with the current deal.

        Use this tool to fetch notes linked to a deal from synced CRM activity data.

        Args:
            limit: Maximum number of notes to return (default: 20, max: 100)
            include_body: Include note body content (default: True)
            max_body_length: Truncate note body to N characters (default: 5000)
            include_properties: Include provider properties dict (default: False)

        Note: organisation_id and deal_id are automatically set from context
        and cannot be overridden. This tool only queries the current deal.
        """
        logger.info(
            "[TOOL CALLED] list_deal_notes - limit=%d, include_body=%s",
            limit,
            include_body,
        )

        if not context.organisation_id:
            logger.warning("list_deal_notes unavailable: missing organisation_id in context")
            return []

        if not context.deal_id:
            logger.warning("list_deal_notes unavailable: missing deal_id in context")
            return []

        # Tool acquires its own sessions on-demand, no lock needed
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="ListDealNotes",
            tool_func=list_deal_notes,
            parameters={
                "context": context,
                "limit": limit,
                "include_body": include_body,
                "max_body_length": max_body_length,
                "include_properties": include_properties,
            },
            description=f"Listing notes for deal: {context.deal_id}",
            fallback_value=[],
        )

        logger.info("[TOOL RESULT] list_deal_notes returned %d notes", len(result) if result else 0)
        return list(result) if result else []

    return list_deal_notes_tool
