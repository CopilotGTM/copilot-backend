"""Agent tool for sending new Outlook emails."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from src.agents.tools.base import execute_with_capabilities
from src.service.email_sends.models import (
    EmailSendAddress,
    EmailSendCreateRequest,
    EmailSendError,
)
from src.service.email_sends.service import EmailSendService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities


def _clean_text(value: str | None) -> str:
    return (value or "").strip()


def _parse_recipients(values: list[str] | list[dict[str, Any]]) -> list[EmailSendAddress]:
    recipients: list[EmailSendAddress] = []
    for value in values:
        if isinstance(value, str):
            recipients.append(EmailSendAddress(email=value))
            continue
        if isinstance(value, dict):
            email = value.get("email") or value.get("address")
            if email:
                recipients.append(
                    EmailSendAddress(
                        email=str(email),
                        name=str(value["name"]) if value.get("name") else None,
                    )
                )
    return recipients


def _has_resolved_recipient(values: list[str] | list[dict[str, Any]]) -> bool:
    return any(
        isinstance(value, dict)
        and any(
            value.get(key) for key in ("name", "source", "source_id", "confidence", "input_value")
        )
        for value in values
    )


def _public_recipients(recipients: list[EmailSendAddress]) -> list[dict[str, str | None]]:
    return [recipient.model_dump() for recipient in recipients]


async def send_email(
    *,
    context: AgentContext,
    to: list[str] | list[dict[str, Any]],
    subject: str,
    body: str,
    cc: list[str] | list[dict[str, Any]] | None = None,
    bcc: list[str] | list[dict[str, Any]] | None = None,
    deal_id: str | None = None,
    sender_email: str | None = None,
    body_content_type: str = "Text",
    save_to_sent_items: bool = True,
    idempotency_key: str | None = None,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Normalize tool input and delegate Outlook send side effects to the service."""
    user_id = context.user_id
    if not user_id:
        return {"status": "error", "message": "Cannot send email: user not identified."}

    try:
        to_recipients = _parse_recipients(to)
        cc_recipients = _parse_recipients(cc or [])
        bcc_recipients = _parse_recipients(bcc or [])
    except ValueError:
        return {
            "status": "needs_clarification",
            "code": "email_send_recipient_email_required",
            "message": "I need exact recipient email addresses before I can send this email.",
        }
    missing_fields = []
    if not to_recipients:
        missing_fields.append("to")
    if not _clean_text(subject):
        missing_fields.append("subject")
    if not _clean_text(body):
        missing_fields.append("body")
    if missing_fields:
        return {
            "status": "needs_clarification",
            "code": "email_send_missing_required_fields",
            "message": f"I need {', '.join(missing_fields)} before I can send this email.",
            "missing_fields": missing_fields,
        }

    confirmation_reasons: list[str] = []
    if not sender_email:
        confirmation_reasons.append("sender_not_explicit")
    if (
        _has_resolved_recipient(to)
        or _has_resolved_recipient(cc or [])
        or _has_resolved_recipient(bcc or [])
    ):
        confirmation_reasons.append("recipient_resolved_from_name")

    if confirmation_reasons and not confirmed:
        return {
            "status": "confirmation_required",
            "code": "email_send_confirmation_required",
            "message": "Please confirm the sender and recipients before I send this email.",
            "confirmation_payload": {
                "to": _public_recipients(to_recipients),
                "cc": _public_recipients(cc_recipients),
                "bcc": _public_recipients(bcc_recipients),
                "subject": _clean_text(subject),
                "body": _clean_text(body),
                "deal_id": deal_id,
                "sender_email": sender_email,
                "confirmation_reasons": confirmation_reasons,
            },
            "confirmation_reasons": confirmation_reasons,
        }

    try:
        request = EmailSendCreateRequest(
            organisation_id=context.organisation_id,
            requested_by_user_id=user_id,
            to=to_recipients,
            cc=cc_recipients,
            bcc=bcc_recipients,
            subject=subject,
            body=body,
            body_content_type=body_content_type,
            save_to_sent_items=save_to_sent_items,
            deal_id=deal_id,
            sender_email=sender_email,
            idempotency_key=idempotency_key,
        )
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}

    async with context.acquire_session() as session:
        service = EmailSendService(
            session,
            organisation_id=context.organisation_id,
            requested_by_user_id=user_id,
        )
        try:
            result = await service.send_email(request)
        except EmailSendError as exc:
            return {"status": "error", "message": exc.message, "code": exc.code}

    return {
        "status": result.status,
        "message": "Email sent.",
        "request_id": str(result.request_id) if result.request_id else None,
        "activity_request_id": str(result.request_id) if result.request_id else None,
        "activity_type": "send_email",
        "linked_record_type": result.linked_record_type,
        "linked_record_id": str(result.linked_record_id) if result.linked_record_id else None,
        "provider_resource_id": result.provider_resource_id,
        "provider_status_code": result.provider_status_code,
        "subject": result.subject,
    }


def create_send_email(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create the send_email tool bound to a context."""

    async def send_email_tool(
        to: list[str] | list[dict[str, Any]],
        subject: str,
        body: str,
        cc: list[str] | list[dict[str, Any]] | None = None,
        bcc: list[str] | list[dict[str, Any]] | None = None,
        deal_id: str | None = None,
        sender_email: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Send a new Outlook email.

        Use only for new emails, not replies, drafts, scheduled sends, Gmail,
        attachments, or thread continuation. Required before sending: explicit
        recipient email, subject, and body. If sender_email is not explicit or a
        recipient came from a name/contact resolution, ask for confirmation and
        call again with confirmed=True after the user approves.
        """
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="SendEmail",
            tool_func=send_email,
            parameters={
                "context": context,
                "to": to,
                "subject": subject,
                "body": body,
                "cc": cc,
                "bcc": bcc,
                "deal_id": deal_id,
                "sender_email": sender_email,
                "confirmed": confirmed,
            },
            description="Sending email",
            fallback_value={"status": "error", "message": "Failed to send email."},
        )
        return dict(result) if result else {"status": "error", "message": "Unknown error."}

    return send_email_tool
