"""List deal emails tool for agents.

Tool for listing emails associated with a specific deal.
Uses DealEmailAssociation to find deal-linked emails.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import func, select

from src.agents.tools.base import db_session_lock, execute_with_capabilities
from src.domain.models.organisation_tables.sync.calendar import AssociationStatus
from src.domain.models.organisation_tables.sync.email_models import (
    DealEmailAssociation,
    DealEmailDirection,
    Email,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def list_deal_emails(
    *,
    context: AgentContext,
    deal_id: str | None = None,
    limit: int = 20,
    direction: str | None = None,
    status: str | None = None,
    sender_email: str | None = None,
    sender_name: str | None = None,
    include_body_preview: bool = False,
) -> list[dict[str, Any]]:
    """List emails associated with a deal.

    Uses DealEmailAssociation to find emails linked to the deal.
    Can filter by direction (inbound/outbound) and status (suggested/confirmed).

    Args:
        context: AgentContext containing session and organisation metadata
        deal_id: Optional deal UUID. If not provided, uses context.deal_id
        limit: Maximum number of emails to return (default: 20)
        direction: Filter by direction - "inbound", "outbound", or "internal"
        status: Filter by association status - "suggested", "confirmed", "rejected"
        sender_email: Filter emails sent from this address (exact, case-insensitive)
        sender_name: Filter emails where sender name contains this string (ILIKE)
        include_body_preview: Include snippet/preview of email body

    Returns:
        List of email dictionaries with association metadata.
    """
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("list_deal_emails: missing organisation_id")
        return []

    # Use context deal_id if not provided
    actual_deal_id = deal_id or (str(context.deal_id) if context.deal_id else None)
    if not actual_deal_id:
        logger.warning("list_deal_emails: no deal_id provided and no context deal_id")
        return []

    # Parse deal_id
    try:
        parsed_deal_id = UUID(actual_deal_id)
    except ValueError:
        logger.warning(f"list_deal_emails: invalid deal_id format: {actual_deal_id}")
        return []

    # Acquire session on-demand (fresh session pattern)
    async with context.acquire_session() as session:
        # Type-safe column access pattern
        assoc_c = cast(Any, DealEmailAssociation).__table__.c
        email_c = cast(Any, Email).__table__.c

        # Build base query for associations
        stmt = (
            select(DealEmailAssociation)
            .where(
                assoc_c.organisation_id == organisation_id,
                assoc_c.deal_id == parsed_deal_id,
            )
            .order_by(assoc_c.email_date.desc())
            .limit(limit)
        )

        # Apply direction filter
        if direction:
            direction_upper = direction.upper()
            if direction_upper in ("INBOUND", "OUTBOUND", "INTERNAL"):
                stmt = stmt.where(assoc_c.direction == direction_upper)

        # Apply status filter
        if status:
            status_upper = status.upper()
            if status_upper in ("SUGGESTED", "CONFIRMED", "REJECTED"):
                stmt = stmt.where(assoc_c.status == status_upper)

        # Apply sender email/name filters (requires joining Email table)
        if sender_email or sender_name:
            stmt = stmt.join(Email, assoc_c.internet_message_id == email_c.internet_message_id)
            if sender_email:
                stmt = stmt.where(func.lower(email_c.from_email) == sender_email.lower())
            if sender_name:
                stmt = stmt.where(email_c.from_name.ilike(f"%{sender_name}%"))

        try:
            result = await session.execute(stmt)
            associations = result.scalars().all()
        except Exception as e:
            logger.exception(f"list_deal_emails: error fetching associations: {e}")
            return []

        if not associations:
            logger.info(f"list_deal_emails: no emails found for deal {actual_deal_id}")
            return []

        # Get full email details for associations
        internet_message_ids = [a.internet_message_id for a in associations]

        email_stmt = select(Email).where(
            email_c.organisation_id == organisation_id,
            email_c.internet_message_id.in_(internet_message_ids),
        )

        try:
            email_result = await session.execute(email_stmt)
            emails = email_result.scalars().all()
            email_map = {e.internet_message_id: e for e in emails}
        except Exception as e:
            logger.exception(f"list_deal_emails: error fetching emails: {e}")
            email_map = {}

    # Build output list
    output: list[dict[str, Any]] = []

    for assoc in associations:
        email = email_map.get(assoc.internet_message_id)

        email_data: dict[str, Any] = {
            "association_id": str(assoc.association_id),
            "internet_message_id": assoc.internet_message_id,
            "subject": assoc.email_subject,
            "email_date": assoc.email_date.isoformat() if assoc.email_date else None,
            "direction": assoc.direction.value
            if isinstance(assoc.direction, DealEmailDirection)
            else assoc.direction,
            "status": assoc.status.value
            if isinstance(assoc.status, AssociationStatus)
            else assoc.status,
            "match_score": assoc.match_score,
            "matched_stakeholders": assoc.matched_stakeholder_emails or [],
        }

        # Add email details if available
        if email:
            email_data["from_email"] = email.from_email
            email_data["from_name"] = email.from_name
            email_data["to_emails"] = email.to_emails or []
            email_data["cc_emails"] = email.cc_emails or []
            email_data["has_attachments"] = email.has_attachments
            email_data["is_read"] = email.is_read
            email_data["is_important"] = email.is_important
            email_data["labels"] = email.labels or []

            if include_body_preview:
                email_data["snippet"] = email.snippet

        output.append(email_data)

    logger.info(f"list_deal_emails: returning {len(output)} emails for deal {actual_deal_id}")
    return output


def create_list_deal_emails(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create list_deal_emails tool bound to a context."""

    async def list_deal_emails_tool(
        deal_id: str | None = None,
        limit: int = 20,
        direction: str | None = None,
        status: str | None = None,
        sender_email: str | None = None,
        sender_name: str | None = None,
        include_body_preview: bool = False,
    ) -> list[dict[str, Any]]:
        """List emails associated with a deal.

        Use this tool to see email communications related to a deal.
        Emails are matched based on stakeholder participation (team members and contacts).
        Supports filtering by sender email or name.

        Args:
            deal_id: The deal UUID. If not provided, uses the current deal context.
            limit: Maximum number of emails to return (default: 20)
            direction: Filter by "inbound" (from contacts), "outbound" (from team), or "internal"
            status: Filter by "suggested" (auto-matched) or "confirmed" (user-verified)
            sender_email: Filter emails sent from this address (exact match, case-insensitive).
            sender_name: Filter emails where the sender's name contains this string
                (case-insensitive partial match).
            include_body_preview: Include a short preview of the email body
        """
        logger.info(
            f"[TOOL CALLED] list_deal_emails - deal_id='{deal_id}', limit={limit}, "
            f"direction='{direction}', status='{status}'"
        )

        if not context.organisation_id:
            logger.warning("list_deal_emails unavailable: missing organisation_id")
            return []

        async with db_session_lock():
            result = await execute_with_capabilities(
                capabilities_getter(),
                tool_name="ListDealEmails",
                tool_func=list_deal_emails,
                parameters={
                    "context": context,
                    "deal_id": deal_id,
                    "limit": limit,
                    "direction": direction,
                    "status": status,
                    "sender_email": sender_email,
                    "sender_name": sender_name,
                    "include_body_preview": include_body_preview,
                },
                description="Finding emails",
                fallback_value=[],
            )

        logger.info(f"[TOOL RESULT] list_deal_emails returned {len(result)} emails")
        return list(result) if result else []

    return list_deal_emails_tool
