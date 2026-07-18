"""Agent tool for sending new Gmail or Outlook emails."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import ValidationError

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


def _display_name(user: Any, fallback: str) -> str:
    first = getattr(user, "first_name", "") or ""
    last = getattr(user, "last_name", "") or ""
    return f"{first} {last}".strip() or str(getattr(user, "email", fallback))


def _normalized_full_name(user: Any) -> str:
    first = getattr(user, "first_name", "") or ""
    last = getattr(user, "last_name", "") or ""
    return f"{first} {last}".strip().lower()


async def _resolve_internal_user_recipient(
    organisation_id: UUID,
    name_or_email: str,
) -> EmailSendAddress | None:
    """Resolve an org app user to an email recipient when the user gives a name."""
    from sqlalchemy import func, or_, select
    from sqlmodel import col

    from src.domain.models.master_tables.user_models import User
    from src.infra.database.sessions import acquire_master_async_session

    search = name_or_email.strip()
    if not search:
        return None

    def same_org(user: Any) -> bool:
        return getattr(user, "organisation_id", None) == organisation_id

    async for session in acquire_master_async_session():
        if "@" in search:
            stmt = select(User).where(
                col(User.organisation_id) == organisation_id,
                col(User.email) == search.lower(),
            )
            result = await session.execute(stmt)
            user = result.scalars().first()
            if user and same_org(user):
                return EmailSendAddress(email=str(user.email), name=_display_name(user, search))
            return None

        lower_search = search.lower()
        tokens = [token for token in re.split(r"\s+", lower_search) if token]
        full_name_expr = func.concat(col(User.first_name), " ", col(User.last_name))
        conditions: list[Any] = [
            full_name_expr.ilike(f"%{lower_search}%"),
            col(User.first_name).ilike(f"%{lower_search}%"),
            col(User.last_name).ilike(f"%{lower_search}%"),
            col(User.email).ilike(f"%{lower_search}%"),
        ]
        for token in tokens:
            conditions.extend(
                [
                    col(User.first_name).ilike(f"%{token}%"),
                    col(User.last_name).ilike(f"%{token}%"),
                    col(User.email).ilike(f"%{token}%"),
                ]
            )

        stmt = (
            select(User)
            .where(
                col(User.organisation_id) == organisation_id,
                or_(*conditions),
            )
            .limit(10)
        )
        result = await session.execute(stmt)
        users = [user for user in result.scalars().all() if same_org(user)]

    if not users:
        return None

    for user in users:
        if _normalized_full_name(user) == lower_search:
            return EmailSendAddress(email=str(user.email), name=_display_name(user, search))

    if len(tokens) >= 2:
        first_token = tokens[0]
        last_token = tokens[-1]
        ordered_matches = [
            user
            for user in users
            if (getattr(user, "first_name", "") or "").lower().startswith(first_token)
            and (getattr(user, "last_name", "") or "").lower().startswith(last_token)
        ]
        if len(ordered_matches) == 1:
            user = ordered_matches[0]
            return EmailSendAddress(email=str(user.email), name=_display_name(user, search))
        return None

    if len(users) == 1:
        user = users[0]
        return EmailSendAddress(email=str(user.email), name=_display_name(user, search))

    for user in users:
        if (getattr(user, "email", "") or "").lower() == lower_search:
            return EmailSendAddress(email=str(user.email), name=_display_name(user, search))

    return None


async def _parse_recipients(
    organisation_id: UUID,
    values: list[str] | list[dict[str, Any]],
) -> tuple[list[EmailSendAddress], bool]:
    recipients: list[EmailSendAddress] = []
    resolved_from_user = False
    for value in values:
        if isinstance(value, str):
            try:
                recipients.append(EmailSendAddress(email=value))
                continue
            except ValidationError:
                resolved = await _resolve_internal_user_recipient(organisation_id, value)
                if resolved is None:
                    raise ValueError("recipient email required") from None
                recipients.append(resolved)
                resolved_from_user = True
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
                continue
            name = value.get("name") or value.get("input_value")
            if name:
                resolved = await _resolve_internal_user_recipient(organisation_id, str(name))
                if resolved is None:
                    raise ValueError("recipient email required") from None
                recipients.append(resolved)
                resolved_from_user = True
    return recipients, resolved_from_user


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
    """Normalize tool input and delegate mailbox send side effects to the service."""
    user_id = context.user_id
    if not user_id:
        return {"status": "error", "message": "Cannot send email: user not identified."}

    try:
        to_recipients, to_resolved_from_user = await _parse_recipients(context.organisation_id, to)
        cc_recipients, cc_resolved_from_user = await _parse_recipients(
            context.organisation_id, cc or []
        )
        bcc_recipients, bcc_resolved_from_user = await _parse_recipients(
            context.organisation_id, bcc or []
        )
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
    # External sending is irreversible, so even an exact user-supplied payload
    # must cross the same explicit final-confirmation boundary as calendar sends.
    confirmation_reasons.append("final_send_confirmation")
    if not sender_email:
        confirmation_reasons.append("sender_not_explicit")
    if (
        _has_resolved_recipient(to)
        or _has_resolved_recipient(cc or [])
        or _has_resolved_recipient(bcc or [])
        or to_resolved_from_user
        or cc_resolved_from_user
        or bcc_resolved_from_user
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
        """Send a new Gmail or Outlook email.

        Use only when the user explicitly insists on immediate delivery of a new
        email, not replies, drafts, scheduled sends, attachments,
        or thread continuation. Required before sending: explicit recipient email,
        subject, and body. If sender_email is not explicit or a recipient came from
        a name/contact resolution, ask for confirmation and call again with
        confirmed=True after the user approves.
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
