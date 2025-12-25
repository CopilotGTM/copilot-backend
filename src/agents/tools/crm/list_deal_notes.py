"""List deal notes tool for agents.

Tool for listing CRM notes (activity_type=NOTE) associated with a specific deal.

Notes are stored in the organisation DB in the canonical CRM activities table
(`crm_activities`) as `CRMActivity` records.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select

from src.agents.tools.base import db_session_lock, execute_with_capabilities
from src.domain.models.organisation_tables.sync.crm.activities import CRMActivity, CRMActivityType

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def list_deal_notes(
    *,
    context: AgentContext,
    deal_id: str | None = None,
    limit: int = 20,
    include_body: bool = True,
    max_body_length: int | None = 5000,
    include_properties: bool = False,
) -> list[dict[str, Any]]:
    """List CRM notes associated with a deal.

    Notes are represented as CRM activities with `activity_type == NOTE`.

    Args:
        context: AgentContext containing session and organisation metadata
        deal_id: Optional deal UUID. If not provided, uses context.deal_id
        limit: Maximum number of notes to return (default: 20, max: 100)
        include_body: Include note body content (default: True)
        max_body_length: Truncate note body to N characters (None = no limit)
        include_properties: Include provider properties dict (default: False)

    Returns:
        List of note dictionaries.
    """

    session = context.session
    organisation_id = context.organisation_id

    if not session or not organisation_id:
        logger.warning("list_deal_notes: missing session or organisation_id")
        return []

    # Use context deal_id if not provided
    actual_deal_id = deal_id or (str(context.deal_id) if context.deal_id else None)
    if not actual_deal_id:
        logger.warning("list_deal_notes: no deal_id provided and no context deal_id")
        return []

    # Parse deal_id
    try:
        parsed_deal_id = UUID(actual_deal_id)
    except ValueError:
        logger.warning(f"list_deal_notes: invalid deal_id format: {actual_deal_id}")
        return []

    effective_limit = max(1, min(limit, 100))

    c = cast(Any, CRMActivity).__table__.c

    stmt = (
        select(CRMActivity)
        .where(
            c.organisation_id == organisation_id,
            c.deal_id == parsed_deal_id,
            c.activity_type == CRMActivityType.NOTE.value,
            c.is_deleted.is_(False),
        )
        .order_by(c.source_created_at.desc(), c.synced_at.desc())
        .limit(effective_limit)
    )

    try:
        result = await session.execute(stmt)
        notes = result.scalars().all()
    except Exception as e:
        logger.exception(f"list_deal_notes: database error: {e}")
        return []

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
            "source_created_at": n.source_created_at.isoformat() if n.source_created_at else None,
            "source_modified_at": n.source_modified_at.isoformat()
            if n.source_modified_at
            else None,
            "synced_at": n.synced_at.isoformat() if n.synced_at else None,
        }

        if include_properties:
            item["properties"] = n.properties or {}

        output.append(item)

    logger.info(f"list_deal_notes: returning {len(output)} notes for deal {actual_deal_id}")
    return output


def create_list_deal_notes(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create list_deal_notes tool bound to a context."""

    async def list_deal_notes_tool(
        deal_id: str | None = None,
        limit: int = 20,
        include_body: bool = True,
        max_body_length: int | None = 5000,
        include_properties: bool = False,
    ) -> list[dict[str, Any]]:
        """List CRM notes associated with a deal.

        Use this tool to fetch notes linked to a deal from synced CRM activity data.

        Args:
            deal_id: The deal UUID. If not provided, uses the current deal context.
            limit: Maximum number of notes to return (default: 20, max: 100)
            include_body: Include note body content (default: True)
            max_body_length: Truncate note body to N characters (default: 5000)
            include_properties: Include provider properties dict (default: False)
        """
        logger.info(
            f"[TOOL CALLED] list_deal_notes - deal_id='{deal_id}', limit={limit}, include_body={include_body}"
        )

        if not context.session or not context.organisation_id:
            logger.warning("list_deal_notes unavailable: missing session or organisation_id")
            return []

        async with db_session_lock():
            result = await execute_with_capabilities(
                capabilities_getter(),
                tool_name="ListDealNotes",
                tool_func=list_deal_notes,
                parameters={
                    "context": context,
                    "deal_id": deal_id,
                    "limit": limit,
                    "include_body": include_body,
                    "max_body_length": max_body_length,
                    "include_properties": include_properties,
                },
                description="Listing deal notes",
                fallback_value=[],
            )

        logger.info(f"[TOOL RESULT] list_deal_notes returned {len(result) if result else 0} notes")
        return list(result) if result else []

    return list_deal_notes_tool
