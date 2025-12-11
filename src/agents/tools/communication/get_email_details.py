"""Get email details tool for agents.

Tool for retrieving detailed information about a specific email.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select

from src.agents.tools.base import db_session_lock, execute_with_capabilities
from src.domain.models.organisation_tables.sync.calendar import AssociationStatus
from src.domain.models.organisation_tables.sync.email_models import (
    DealEmailAssociation,
    DealEmailDirection,
    Email,
    EmailDirection,
    EmailPriority,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def get_email_details(
    internet_message_id: str,
    *,
    context: AgentContext,
    include_body: bool = True,
    include_html: bool = False,
    max_body_length: int | None = None,
) -> dict[str, Any] | None:
    """Get detailed information about a specific email.

    Args:
        internet_message_id: The RFC 822 Message-ID of the email
        context: AgentContext containing session and organisation metadata
        include_body: Include the plain text body content
        include_html: Include the HTML body content
        max_body_length: Truncate body to N characters (None = no limit)

    Returns:
        Email dictionary with full details, or None if not found.
    """
    session = context.session
    organisation_id = context.organisation_id

    if not session or not organisation_id:
        logger.warning("get_email_details: missing session or organisation_id")
        return None

    if not internet_message_id:
        logger.warning("get_email_details: no internet_message_id provided")
        return None

    # Type-safe column access pattern
    email_c = cast(Any, Email).__table__.c
    assoc_c = cast(Any, DealEmailAssociation).__table__.c

    # Fetch the email
    email_stmt = select(Email).where(
        email_c.organisation_id == organisation_id,
        email_c.internet_message_id == internet_message_id,
    )

    try:
        result = await session.execute(email_stmt)
        email = result.scalar_one_or_none()
    except Exception as e:
        logger.exception(f"get_email_details: error fetching email: {e}")
        return None

    if not email:
        logger.info(f"get_email_details: email not found: {internet_message_id}")
        return None

    # Build output
    output: dict[str, Any] = {
        "email_id": str(email.email_id),
        "internet_message_id": email.internet_message_id,
        "thread_id": email.thread_id,
        "subject": email.subject,
        "from_email": email.from_email,
        "from_name": email.from_name,
        "to_emails": email.to_emails or [],
        "cc_emails": email.cc_emails or [],
        "received_at": email.received_at.isoformat() if email.received_at else None,
        "sent_at": email.sent_at.isoformat() if email.sent_at else None,
        "direction": email.direction.value
        if isinstance(email.direction, EmailDirection)
        else email.direction,
        "priority": email.priority.value
        if isinstance(email.priority, EmailPriority)
        else email.priority,
        "is_read": email.is_read,
        "is_starred": email.is_starred,
        "is_important": email.is_important,
        "has_attachments": email.has_attachments,
        "labels": email.labels or [],
    }

    # Include attachments metadata
    if email.has_attachments and email.attachments:
        output["attachments"] = [
            {
                "name": att.get("name"),
                "size": att.get("size"),
                "mime_type": att.get("mime_type"),
            }
            for att in email.attachments
        ]
    else:
        output["attachments"] = []

    # Include body content
    if include_body and email.body_text:
        body = email.body_text
        if max_body_length and len(body) > max_body_length:
            output["body_text"] = body[:max_body_length]
            output["body_truncated"] = True
        else:
            output["body_text"] = body
            output["body_truncated"] = False
        output["body_length"] = len(email.body_text)
    elif include_body:
        output["body_text"] = email.snippet  # Fall back to snippet
        output["body_truncated"] = True
        output["body_length"] = 0

    if include_html and email.body_html:
        html = email.body_html
        if max_body_length and len(html) > max_body_length:
            output["body_html"] = html[:max_body_length]
        else:
            output["body_html"] = html

    # Fetch deal associations
    assoc_stmt = select(DealEmailAssociation).where(
        assoc_c.organisation_id == organisation_id,
        assoc_c.internet_message_id == internet_message_id,
    )

    try:
        assoc_result = await session.execute(assoc_stmt)
        associations = assoc_result.scalars().all()

        if associations:
            output["deal_associations"] = [
                {
                    "deal_id": str(assoc.deal_id),
                    "direction": assoc.direction.value
                    if isinstance(assoc.direction, DealEmailDirection)
                    else assoc.direction,
                    "status": assoc.status.value
                    if isinstance(assoc.status, AssociationStatus)
                    else assoc.status,
                    "match_score": assoc.match_score,
                    "matched_stakeholders": assoc.matched_stakeholder_emails or [],
                }
                for assoc in associations
            ]
        else:
            output["deal_associations"] = []
    except Exception as e:
        logger.warning(f"get_email_details: error fetching associations: {e}")
        output["deal_associations"] = []

    logger.info(f"get_email_details: returning details for email {internet_message_id}")
    return output


def create_get_email_details(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create get_email_details tool bound to a context."""

    async def get_email_details_tool(
        internet_message_id: str,
        include_body: bool = True,
        include_html: bool = False,
        max_body_length: int | None = 5000,
    ) -> dict[str, Any] | None:
        """Get detailed information about a specific email.

        Use this tool to read the full content of an email, including body text,
        attachments, and deal associations.

        Args:
            internet_message_id: The email Message-ID (from list_deal_emails)
            include_body: Include the plain text body content (default: True)
            include_html: Include the HTML body content (default: False)
            max_body_length: Truncate body to N characters (default: 5000)
        """
        logger.info(
            f"[TOOL CALLED] get_email_details - internet_message_id='{internet_message_id}', "
            f"include_body={include_body}, max_body_length={max_body_length}"
        )

        if not context.session or not context.organisation_id:
            logger.warning("get_email_details unavailable: missing session or organisation_id")
            return None

        async with db_session_lock:
            result = await execute_with_capabilities(
                capabilities_getter(),
                tool_name="GetEmailDetails",
                tool_func=get_email_details,
                parameters={
                    "internet_message_id": internet_message_id,
                    "context": context,
                    "include_body": include_body,
                    "include_html": include_html,
                    "max_body_length": max_body_length,
                },
                description=f"Getting email details: {internet_message_id[:50]}...",
                fallback_value=None,
            )

        if result:
            logger.info(
                f"[TOOL RESULT] get_email_details - found email with subject: {result.get('subject', 'N/A')}"
            )
        else:
            logger.info("[TOOL RESULT] get_email_details returned None (not found)")

        return dict(result) if result else None

    return get_email_details_tool
