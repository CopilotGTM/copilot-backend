"""Agent tools for saving and sending drafts in the current user's mailbox.

The authenticated `AgentContext` fixes the organisation and user boundaries.
Provider draft IDs are never sufficient by themselves: the service first resolves
the user's selected Gmail or Outlook integration before reading or mutating them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from src.agents.tools.base import execute_with_capabilities
from src.agents.tools.communication.send_email import _parse_recipients, _public_recipients
from src.service.email_sends.models import EmailDraftSaveRequest, EmailSendError
from src.service.email_sends.service import EmailSendService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities


async def save_email_draft(
    *,
    context: AgentContext,
    to: list[str] | list[dict[str, Any]],
    subject: str,
    body: str,
    cc: list[str] | list[dict[str, Any]] | None = None,
    bcc: list[str] | list[dict[str, Any]] | None = None,
    sender_email: str | None = None,
    body_content_type: str = "Text",
    draft_id: str | None = None,
    deal_id: str | None = None,
    reply_to_internet_message_id: str | None = None,
    reply_mode: str = "reply",
) -> dict[str, Any]:
    """Save a new, reply, or replacement draft within the user's mailbox boundary."""

    if not context.user_id:
        return {"status": "error", "message": "Cannot save draft: user not identified."}
    try:
        to_values, _ = await _parse_recipients(context.organisation_id, to)
        cc_values, _ = await _parse_recipients(context.organisation_id, cc or [])
        bcc_values, _ = await _parse_recipients(context.organisation_id, bcc or [])
        request = EmailDraftSaveRequest(
            organisation_id=context.organisation_id,
            requested_by_user_id=context.user_id,
            to=to_values,
            cc=cc_values,
            bcc=bcc_values,
            subject=subject,
            body=body,
            body_content_type=body_content_type,
            sender_email=sender_email,
            draft_id=draft_id,
            deal_id=deal_id,
            reply_to_internet_message_id=reply_to_internet_message_id,
            reply_mode=reply_mode,
        )
    except (ValueError, TypeError) as exc:
        return {
            "status": "needs_clarification",
            "code": "email_draft_fields_required",
            "message": str(exc),
        }

    async with context.acquire_session() as session:
        service = EmailSendService(
            session,
            organisation_id=context.organisation_id,
            requested_by_user_id=context.user_id,
        )
        try:
            result = await service.save_draft(request)
        except EmailSendError as exc:
            return {"status": "error", "code": exc.code, "message": exc.message}

    snapshot = result.snapshot
    if snapshot is None:
        return {"status": "error", "message": "Draft saved without a readable provider copy."}
    return {
        "status": "draft_saved",
        "message": (
            "Reply saved in the existing email thread."
            if request.reply_to_internet_message_id
            else "Email saved to your Drafts folder."
        ),
        "draft_kind": "reply" if request.reply_to_internet_message_id else "new",
        "draft_id": snapshot.draft_id,
        "sender_email": snapshot.sender_email,
        "to": _public_recipients(snapshot.to),
        "cc": _public_recipients(snapshot.cc),
        "bcc": _public_recipients(snapshot.bcc),
        "subject": snapshot.subject,
        "body": snapshot.body,
        "body_content_type": snapshot.body_content_type,
        "content_hash": snapshot.content_hash,
        "web_link": snapshot.web_link,
        "activity_request_id": str(result.request_id) if result.request_id else None,
    }


async def send_email_draft(
    *,
    context: AgentContext,
    draft_id: str,
    sender_email: str | None = None,
    expected_content_hash: str | None = None,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Preview or send one unchanged draft from the authenticated user's mailbox."""

    if not context.user_id:
        return {"status": "error", "message": "Cannot send draft: user not identified."}
    async with context.acquire_session() as session:
        service = EmailSendService(
            session,
            organisation_id=context.organisation_id,
            requested_by_user_id=context.user_id,
        )
        try:
            snapshot = await service.get_draft(
                draft_id=draft_id,
                sender_email=sender_email,
            )
            preview = {
                "draft_id": snapshot.draft_id,
                "sender_email": snapshot.sender_email,
                "to": _public_recipients(snapshot.to),
                "cc": _public_recipients(snapshot.cc),
                "bcc": _public_recipients(snapshot.bcc),
                "subject": snapshot.subject,
                "body": snapshot.body,
                "body_content_type": snapshot.body_content_type,
                "content_hash": snapshot.content_hash,
            }
            if not confirmed:
                return {
                    "status": "confirmation_required",
                    "code": "email_draft_send_confirmation_required",
                    "message": "Please confirm this exact mailbox draft before I send it.",
                    "confirmation_payload": preview,
                }
            if not expected_content_hash:
                return {
                    "status": "confirmation_required",
                    "code": "email_draft_content_hash_required",
                    "message": "Please review the latest draft before sending it.",
                    "confirmation_payload": preview,
                }
            result = await service.send_draft(
                draft_id=draft_id,
                sender_email=sender_email,
                expected_content_hash=expected_content_hash,
            )
        except EmailSendError as exc:
            return {"status": "error", "code": exc.code, "message": exc.message}

    return {
        "status": "sent",
        "message": "Email draft sent.",
        "draft_id": draft_id,
        "subject": snapshot.subject,
        "activity_request_id": str(result.request_id) if result.request_id else None,
        "provider_status_code": result.provider_status_code,
    }


def create_save_email_draft(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Bind the mailbox-draft save tool to immutable agent context."""

    async def save_email_draft_tool(
        to: list[str] | list[dict[str, Any]],
        subject: str,
        body: str,
        cc: list[str] | list[dict[str, Any]] | None = None,
        bcc: list[str] | list[dict[str, Any]] | None = None,
        sender_email: str | None = None,
        body_content_type: str = "Text",
        draft_id: str | None = None,
        deal_id: str | None = None,
        reply_to_internet_message_id: str | None = None,
        reply_mode: str = "reply",
    ) -> dict[str, Any]:
        """Save a new or threaded email draft; this never sends the message.

        Omit `reply_to_internet_message_id` for a new email. For a reply, pass
        the exact source Message-ID returned by mailbox search/details. Threaded
        drafts preserve that source message's external To and CC recipients.
        """

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="SaveEmailDraft",
            tool_func=save_email_draft,
            parameters={
                "context": context,
                "to": to,
                "subject": subject,
                "body": body,
                "cc": cc,
                "bcc": bcc,
                "sender_email": sender_email,
                "body_content_type": body_content_type,
                "draft_id": draft_id,
                "deal_id": deal_id,
                "reply_to_internet_message_id": reply_to_internet_message_id,
                "reply_mode": reply_mode,
            },
            description="Saving email draft",
            fallback_value={"status": "error", "message": "Failed to save email draft."},
        )
        return dict(result)

    return save_email_draft_tool


def create_send_email_draft(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Bind the approval-gated draft-send tool to immutable agent context."""

    async def send_email_draft_tool(
        draft_id: str,
        sender_email: str | None = None,
        expected_content_hash: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Preview, then send, an existing unchanged Gmail or Outlook draft."""

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="SendEmailDraft",
            tool_func=send_email_draft,
            parameters={
                "context": context,
                "draft_id": draft_id,
                "sender_email": sender_email,
                "expected_content_hash": expected_content_hash,
                "confirmed": confirmed,
            },
            description="Sending email draft",
            fallback_value={"status": "error", "message": "Failed to send email draft."},
        )
        return dict(result)

    return send_email_draft_tool
