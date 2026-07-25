"""Get email details tool for agents.

Tool for retrieving detailed information about a specific email.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select

from src.agents.tools.base import db_session_lock, execute_with_capabilities
from src.agents.tools.communication.email_attachments import (
    list_email_attachments,
    resolve_readable_email_source,
)
from src.agents.tools.crm.access import (
    resolve_agent_access_actor,
)
from src.domain.models.organisation_tables.sync.calendar import AssociationStatus
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.email_models import (
    DealEmailAssociation,
    DealEmailDirection,
    Email,
    EmailDirection,
    EmailPriority,
)
from src.service.crm.access.record_access_policy import DealAccessPolicy

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

# These limits keep a single agent tool result small enough to reason over while
# preserving the 10–15-message conversations expected by the mailbox workflow.
_DEFAULT_THREAD_MESSAGES = 15
_MAX_THREAD_MESSAGES = 25
_MAX_MESSAGE_CHARACTERS = 8_000
_MAX_THREAD_CHARACTERS = 60_000
# copilotgtm: four parallel mailbox calls bound provider pressure; use a shared
# provider rate limiter if attachment hydration moves outside this one tool call.
_ATTACHMENT_MANIFEST_CONCURRENCY = 4


def _format_attachment_manifest(email: Email, attachment: dict[str, Any]) -> dict[str, Any]:
    """Normalize cached and legacy metadata without changing its readability decision."""
    output = {
        "attachment_id": attachment.get("attachment_id"),
        "internet_message_id": email.internet_message_id,
        "filename": attachment.get("filename") or attachment.get("name"),
        "size_bytes": attachment.get("size_bytes", attachment.get("size")),
        "mime_type": attachment.get("mime_type"),
        "readable": attachment.get("readable", bool(attachment.get("attachment_id"))),
    }
    if "unreadable_reason" in attachment:
        output["unreadable_reason"] = attachment.get("unreadable_reason")
    return output


def _format_email_details(
    email: Email,
    *,
    include_body: bool,
    include_html: bool,
    max_body_length: int,
) -> dict[str, Any]:
    """Format one loaded message within a shared plain-text/HTML character budget."""
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
        "attachments": [
            _format_attachment_manifest(email, attachment)
            for attachment in (email.attachments or [])
        ]
        if email.has_attachments
        else [],
    }
    remaining_characters = max_body_length
    content_characters = 0
    if include_body and remaining_characters:
        body = email.body_text or email.snippet or ""
        visible_body = body[:remaining_characters]
        output["body_length"] = len(email.body_text or "")
        output["body_truncated"] = not email.body_text or len(visible_body) < len(body)
        output["body_text"] = visible_body
        remaining_characters -= len(visible_body)
        content_characters += len(visible_body)
    if include_html and email.body_html and remaining_characters:
        visible_html = email.body_html[:remaining_characters]
        output["body_html"] = visible_html
        output["html_truncated"] = len(visible_html) < len(email.body_html)
        content_characters += len(visible_html)
    output["content_characters"] = content_characters
    return output


async def _hydrate_attachment_manifests(
    output: dict[str, Any],
    *,
    context: AgentContext,
    mailbox_email: str | None,
    deal_id: str | None,
) -> None:
    """Populate provider handles once per message after the DB session is released.

    The selected message is repeated inside `thread_messages`. Hydrating unique
    RFC Message-IDs prevents duplicate provider calls while preserving each
    attachment's owning-message boundary in both response locations.
    """
    messages = [output, *output.get("thread_messages", [])]
    manifests_by_message: dict[str, dict[str, Any]] = {}
    pending_message_ids: list[str] = []
    seen_message_ids: set[str] = set()

    for message in messages:
        message_id = message.get("internet_message_id")
        if not message_id or not message.get("has_attachments"):
            continue
        existing = message.get("attachments") or []
        if existing and all(item.get("attachment_id") for item in existing):
            manifests_by_message.setdefault(
                message_id,
                {"status": "success", "attachments": existing},
            )
            seen_message_ids.add(message_id)
            continue
        if message_id in seen_message_ids:
            continue
        seen_message_ids.add(message_id)
        pending_message_ids.append(message_id)

    semaphore = asyncio.Semaphore(_ATTACHMENT_MANIFEST_CONCURRENCY)

    async def _hydrate_one(message_id: str) -> dict[str, Any]:
        """Load one manifest while the shared semaphore limits provider pressure."""
        async with semaphore:
            return await list_email_attachments(
                message_id,
                context=context,
                mailbox_email=mailbox_email,
                deal_id=deal_id,
            )

    hydrated = await asyncio.gather(
        *(_hydrate_one(message_id) for message_id in pending_message_ids)
    )
    manifests_by_message.update(
        zip(
            pending_message_ids,
            hydrated,
            strict=True,
        )
    )

    for message in messages:
        message_id = message.get("internet_message_id")
        result = manifests_by_message.get(message_id)
        if result is None:
            message["attachment_manifest_status"] = (
                "not_applicable" if not message.get("has_attachments") else "unavailable"
            )
            continue
        if result.get("status") == "success":
            message["attachments"] = result.get("attachments", [])
            message["attachment_manifest_status"] = "complete"
        else:
            message["attachment_manifest_status"] = "unavailable"
            message["attachment_manifest_error"] = result.get("code")

    output["attachment_instance_count"] = len(output.get("attachments") or [])
    output["readable_attachment_count"] = sum(
        bool(item.get("readable")) for item in output.get("attachments") or []
    )
    if "thread_messages" in output:
        thread_attachments = [
            attachment
            for message in output["thread_messages"]
            for attachment in message.get("attachments") or []
        ]
        output["thread_attachment_instance_count"] = len(thread_attachments)
        output["thread_readable_attachment_count"] = sum(
            bool(item.get("readable")) for item in thread_attachments
        )


async def get_email_details(
    internet_message_id: str,
    *,
    context: AgentContext,
    include_body: bool = True,
    include_html: bool = False,
    max_body_length: int | None = _MAX_MESSAGE_CHARACTERS,
    mailbox_email: str | None = None,
    include_thread: bool = False,
    max_thread_messages: int = _DEFAULT_THREAD_MESSAGES,
    deal_id: str | None = None,
) -> dict[str, Any] | None:
    """Read an email within one owned-mailbox or authorized-deal boundary.

    A mailbox scope may expand the provider thread within that same integration.
    A deal scope returns only active email associations for that visible deal, so
    an RFC Message-ID alone can never grant access to another employee's mailbox.

    Args:
        internet_message_id: The RFC 822 Message-ID of the email
        context: AgentContext containing session and organisation metadata
        include_body: Include the plain text body content
        include_html: Include the HTML body content
        max_body_length: Per-message content limit, capped at 8,000 characters
        mailbox_email: Restrict lookup to one connected mailbox
        include_thread: Include the complete locally synced provider conversation
        max_thread_messages: Maximum chronological messages returned for a thread
        deal_id: Visible deal UUID; falls back to context.deal_id when mailbox is absent

    Returns:
        Email dictionary with full details, or None if not found.
    """
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("get_email_details: missing organisation_id")
        return None

    if not internet_message_id:
        logger.warning("get_email_details: no internet_message_id provided")
        return None

    if mailbox_email and deal_id:
        logger.warning("get_email_details: mailbox_email and deal_id are mutually exclusive")
        return None

    effective_deal_id = deal_id or (
        str(context.deal_id) if context.deal_id and not mailbox_email else None
    )
    if not mailbox_email and not effective_deal_id:
        logger.warning("get_email_details: owned mailbox or authorized deal scope required")
        return None
    if max_body_length is None:
        effective_body_limit = _MAX_MESSAGE_CHARACTERS
    elif max_body_length <= 0:
        logger.warning("get_email_details: max_body_length must be positive")
        return None
    else:
        effective_body_limit = min(max_body_length, _MAX_MESSAGE_CHARACTERS)
    if max_thread_messages <= 0:
        logger.warning("get_email_details: max_thread_messages must be positive")
        return None
    effective_thread_limit = min(max_thread_messages, _MAX_THREAD_MESSAGES)

    try:
        source = await resolve_readable_email_source(
            context=context,
            internet_message_id=internet_message_id,
            mailbox_email=mailbox_email,
            deal_id=effective_deal_id,
            require_provider=False,
        )
    except Exception:
        logger.exception("get_email_details: failed to resolve authorized email")
        return None
    if source is None or source.email is None:
        logger.info("get_email_details: email is unavailable in the requested scope")
        return None

    email = source.email
    parsed_deal_id = source.deal_id

    # The shared resolver has already enforced mailbox/deal access. This second,
    # short session loads thread history and CRM enrichment only.
    async with context.acquire_session() as session:
        email_c = cast(Any, Email).__table__.c
        assoc_c = cast(Any, DealEmailAssociation).__table__.c

        thread_messages = [email]
        if include_thread and email.thread_id:
            thread_stmt = (
                select(Email)
                .where(
                    email_c.organisation_id == organisation_id,
                    email_c.integration_id == email.integration_id,
                    email_c.thread_id == email.thread_id,
                    email_c.is_deleted.is_(False),
                )
                .order_by(email_c.received_at.desc(), email_c.email_id.desc())
                .limit(effective_thread_limit + 1)
            )
            if parsed_deal_id is not None:
                # Deal reads may use organization-wide synchronized copies, but
                # only messages actively associated with the authorized deal.
                thread_stmt = thread_stmt.join(
                    DealEmailAssociation,
                    assoc_c.internet_message_id == email_c.internet_message_id,
                ).where(
                    assoc_c.organisation_id == organisation_id,
                    assoc_c.deal_id == parsed_deal_id,
                    assoc_c.status == AssociationStatus.ACTIVE.value,
                )
            try:
                thread_result = await session.execute(thread_stmt)
                thread_messages = list(reversed(thread_result.scalars().all()))
            except Exception as e:
                logger.exception(f"get_email_details: error fetching thread: {e}")
                return None

        association_message_ids = {
            item.internet_message_id for item in thread_messages if item.internet_message_id
        }
        try:
            if parsed_deal_id is not None:
                assoc_stmt = select(DealEmailAssociation).where(
                    assoc_c.organisation_id == organisation_id,
                    assoc_c.deal_id == parsed_deal_id,
                    assoc_c.status == AssociationStatus.ACTIVE.value,
                    assoc_c.internet_message_id.in_(association_message_ids),
                )
            else:
                actor = await resolve_agent_access_actor(context)
                deal_c = cast(Any, CRMDeal).__table__.c
                assoc_stmt = (
                    select(DealEmailAssociation)
                    .join(CRMDeal, deal_c.deal_id == assoc_c.deal_id)
                    .where(
                        assoc_c.organisation_id == organisation_id,
                        assoc_c.status == AssociationStatus.ACTIVE.value,
                        assoc_c.internet_message_id.in_(association_message_ids),
                        deal_c.organisation_id == organisation_id,
                        deal_c.is_deleted.is_(False),
                        DealAccessPolicy(session, organisation_id, actor).visible_deal_predicate(
                            deal_c
                        ),
                    )
                )
            associations = (await session.execute(assoc_stmt)).scalars().all()
        except Exception as e:
            # Email ownership remains valid if CRM is unavailable; withholding
            # enrichment fails closed without hiding the user's own message.
            logger.warning(f"get_email_details: error fetching associations: {e}")
            associations = []

        output = _format_email_details(
            email,
            include_body=include_body,
            include_html=include_html,
            max_body_length=effective_body_limit,
        )
        output["access_scope"] = source.access_scope
        output["scope_id"] = str(source.scope_id)
        if source.access_scope == "mailbox":
            output["mailbox_email"] = mailbox_email.strip().lower() if mailbox_email else None

        # Add deal associations to output
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

        if include_thread:
            # The top-level message is repeated inside thread_messages for a
            # self-contained timeline, so count both copies against one response.
            remaining_characters = max(
                0,
                _MAX_THREAD_CHARACTERS - output["content_characters"],
            )
            formatted_thread: list[dict[str, Any]] = []
            for message in thread_messages[:effective_thread_limit]:
                formatted = _format_email_details(
                    message,
                    include_body=include_body,
                    include_html=include_html,
                    max_body_length=min(effective_body_limit, remaining_characters),
                )
                remaining_characters -= formatted["content_characters"]
                formatted_thread.append(formatted)
                if remaining_characters <= 0:
                    break
            output["thread_messages"] = formatted_thread
            output["thread_message_count"] = len(formatted_thread)
            output["thread_truncated"] = (
                len(thread_messages) > len(formatted_thread) or remaining_characters <= 0
            )
            output["thread_content_characters"] = sum(
                message["content_characters"] for message in formatted_thread
            )
            output["total_content_characters"] = (
                output["content_characters"] + output["thread_content_characters"]
            )
            output["thread_content_truncated"] = remaining_characters <= 0 or any(
                message.get("body_truncated") or message.get("html_truncated")
                for message in formatted_thread
            )

    # Provider listing is intentionally outside the database session. Gmail or
    # Graph latency must not hold an org-scoped pooled connection.
    await _hydrate_attachment_manifests(
        output,
        context=context,
        mailbox_email=mailbox_email,
        deal_id=str(parsed_deal_id) if parsed_deal_id else None,
    )
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
        max_body_length: int | None = _MAX_MESSAGE_CHARACTERS,
        mailbox_email: str | None = None,
        include_thread: bool = False,
        max_thread_messages: int = _DEFAULT_THREAD_MESSAGES,
        deal_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get detailed information about a specific email.

        Use this tool to read the full content of an email, including body text,
        attachments, and deal associations.

        Args:
            internet_message_id: The email Message-ID (from list_deal_emails)
            include_body: Include the plain text body content (default: True)
            include_html: Include the HTML body content (default: False)
            max_body_length: Per-message content limit (default/max: 8000)
            mailbox_email: Connected mailbox owning the message copy
            include_thread: Include chronological messages from the provider thread
            max_thread_messages: Maximum thread messages (default: 15, max: 25)
            deal_id: Authorized deal UUID; use instead of mailbox_email
        """
        logger.info(
            f"[TOOL CALLED] get_email_details - internet_message_id='{internet_message_id}', "
            f"include_body={include_body}, max_body_length={max_body_length}"
        )

        if not context.organisation_id:
            logger.warning("get_email_details unavailable: missing organisation_id")
            return None

        async with db_session_lock():
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
                    "mailbox_email": mailbox_email,
                    "include_thread": include_thread,
                    "max_thread_messages": max_thread_messages,
                    "deal_id": deal_id,
                },
                description="Reading email",
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
