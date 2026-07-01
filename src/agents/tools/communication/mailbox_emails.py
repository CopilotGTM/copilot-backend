"""Mailbox-scoped email read tools for agents."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlmodel import col

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.domain.models.organisation_tables.sync.email_models import Email
from src.infra.config.google_integration_scopes import GOOGLE_SCOPE_GMAIL_READONLY
from src.service.integrations.scope_validation import (
    get_missing_scope_labels,
    is_required_scope_satisfied,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

_GOOGLE_EMAIL_TYPES = (IntegrationType.GOOGLE, IntegrationType.GMAIL)
_MICROSOFT_EMAIL_TYPES = (IntegrationType.MICROSOFT, IntegrationType.OUTLOOK_EMAIL)
_EMAIL_ACCOUNT_TYPES = (IntegrationType.GOOGLE, IntegrationType.MICROSOFT)
_MICROSOFT_SCOPE_MAIL_READ = "Mail.Read"


def _clean_email(value: str | None) -> str | None:
    cleaned = (value or "").strip().lower()
    return cleaned or None


def _parse_iso_datetime(value: str | None) -> datetime | None:
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    normalized = cleaned.removesuffix("Z") + "+00:00" if cleaned.endswith("Z") else cleaned
    parsed = datetime.fromisoformat(normalized)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_uuid(value: str | None) -> UUID | None:
    cleaned = (value or "").strip()
    return UUID(cleaned) if cleaned else None


def _email_capability_type(integration_type: IntegrationType) -> IntegrationType:
    if integration_type in _GOOGLE_EMAIL_TYPES:
        return IntegrationType.GMAIL
    if integration_type in _MICROSOFT_EMAIL_TYPES:
        return IntegrationType.OUTLOOK_EMAIL
    return integration_type


def _provider_label(integration_type: IntegrationType) -> str:
    if integration_type in _GOOGLE_EMAIL_TYPES:
        return "google"
    if integration_type in _MICROSOFT_EMAIL_TYPES:
        return "microsoft"
    return integration_type.value


def _missing_read_scopes(integration: KnowledgeIntegration) -> list[str]:
    capability_type = _email_capability_type(integration.integration_type)
    if capability_type == IntegrationType.GMAIL:
        missing = (
            set()
            if is_required_scope_satisfied(
                GOOGLE_SCOPE_GMAIL_READONLY, list(integration.scopes or [])
            )
            else {GOOGLE_SCOPE_GMAIL_READONLY}
        )
        return get_missing_scope_labels(missing)
    if capability_type == IntegrationType.OUTLOOK_EMAIL:
        missing = (
            set()
            if is_required_scope_satisfied(
                _MICROSOFT_SCOPE_MAIL_READ, list(integration.scopes or [])
            )
            else {_MICROSOFT_SCOPE_MAIL_READ}
        )
        return get_missing_scope_labels(missing)
    return []


def _can_read_email(integration: KnowledgeIntegration) -> bool:
    return bool(
        integration.is_active
        and not integration.requires_reauth
        and not _missing_read_scopes(integration)
    )


def _account_item(integration: KnowledgeIntegration) -> dict[str, Any]:
    return {
        "integration_id": str(integration.integration_id),
        "integration_email": integration.integration_email,
        "provider": _provider_label(integration.integration_type),
        "name": integration.name,
        "is_active": integration.is_active,
        "requires_reauth": integration.requires_reauth,
        "can_read_email": _can_read_email(integration),
        "missing_read_scopes": _missing_read_scopes(integration),
        "last_synced_at": integration.last_synced_at.isoformat()
        if integration.last_synced_at
        else None,
    }


async def list_connected_email_accounts(*, context: AgentContext) -> list[dict[str, Any]]:
    """List the current user's connected email accounts and read readiness."""
    if not context.organisation_id or not context.user_id:
        return []

    async with context.acquire_session() as session:
        stmt = (
            select(KnowledgeIntegration)
            .where(
                col(KnowledgeIntegration.organisation_id) == context.organisation_id,
                col(KnowledgeIntegration.created_by_user_id) == context.user_id,
                col(KnowledgeIntegration.integration_type).in_(_EMAIL_ACCOUNT_TYPES),
            )
            .order_by(col(KnowledgeIntegration.created_at).desc())
        )
        try:
            result = await session.execute(stmt)
            integrations = result.scalars().all()
        except Exception:
            logger.exception("list_connected_email_accounts: failed to load integrations")
            return []

    return [_account_item(integration) for integration in integrations]


async def _resolve_readable_mailbox(
    *,
    context: AgentContext,
    mailbox_email: str,
    session: Any,
) -> KnowledgeIntegration | None:
    selected_email = _clean_email(mailbox_email)
    if not selected_email:
        return None

    stmt = (
        select(KnowledgeIntegration)
        .where(
            col(KnowledgeIntegration.organisation_id) == context.organisation_id,
            col(KnowledgeIntegration.created_by_user_id) == context.user_id,
            col(KnowledgeIntegration.integration_type).in_(_EMAIL_ACCOUNT_TYPES),
            func.lower(col(KnowledgeIntegration.integration_email)) == selected_email,
        )
        .order_by(col(KnowledgeIntegration.created_at).desc())
    )
    result = await session.execute(stmt)
    integrations = result.scalars().all()
    for integration in integrations:
        if _can_read_email(integration):
            return integration
    return None


def _format_email(email: Email, *, include_body_preview: bool) -> dict[str, Any]:
    item: dict[str, Any] = {
        "email_id": str(email.email_id),
        "integration_id": str(email.integration_id),
        "internet_message_id": email.internet_message_id,
        "thread_id": email.thread_id,
        "subject": email.subject,
        "from_email": email.from_email,
        "from_name": email.from_name,
        "to_emails": email.to_emails or [],
        "cc_emails": email.cc_emails or [],
        "received_at": email.received_at.isoformat() if email.received_at else None,
        "sent_at": email.sent_at.isoformat() if email.sent_at else None,
        "is_read": email.is_read,
        "is_important": email.is_important,
        "has_attachments": email.has_attachments,
        "labels": email.labels or [],
        "snippet": email.snippet,
    }
    if include_body_preview:
        item["body_preview"] = (email.body_text or email.snippet or "")[:1000]
    return item


async def search_mailbox_emails(
    *,
    context: AgentContext,
    mailbox_email: str,
    days_back: int = 14,
    query: str | None = None,
    from_email: str | None = None,
    subject_contains: str | None = None,
    limit: int = 20,
    include_body_preview: bool = False,
    before_received_at: str | None = None,
    before_email_id: str | None = None,
) -> dict[str, Any]:
    """Search synced emails for one explicitly selected connected mailbox."""
    if not context.organisation_id or not context.user_id:
        return {
            "status": "error",
            "code": "user_context_required",
            "message": "I need the current user and organization before reading mailbox emails.",
        }

    selected_email = _clean_email(mailbox_email)
    if not selected_email:
        return {
            "status": "error",
            "code": "mailbox_required",
            "message": "Tell me which connected mailbox to read.",
        }

    async with context.acquire_session() as session:
        try:
            integration = await _resolve_readable_mailbox(
                context=context,
                mailbox_email=selected_email,
                session=session,
            )
        except Exception:
            logger.exception("search_mailbox_emails: failed to resolve mailbox")
            integration = None

        if integration is None:
            return {
                "status": "error",
                "code": "mailbox_not_found",
                "message": f"I could not find a connected readable mailbox for {selected_email}.",
            }

        email_c = cast(Any, Email).__table__.c
        effective_limit = max(1, min(limit, 100))
        effective_days = max(1, min(days_back, 365))
        cutoff = datetime.now(UTC) - timedelta(days=effective_days)
        try:
            cursor_received_at = _parse_iso_datetime(before_received_at)
            cursor_email_id = _parse_uuid(before_email_id)
        except ValueError:
            return {
                "status": "error",
                "code": "invalid_cursor",
                "message": "The mailbox email pagination cursor is invalid.",
            }

        stmt = (
            select(Email)
            .where(
                col(Email.organisation_id) == context.organisation_id,
                col(Email.integration_id) == integration.integration_id,
                col(Email.is_deleted).is_(False),
                col(Email.received_at) >= cutoff,
            )
            .order_by(email_c.received_at.desc(), email_c.email_id.desc())
            .limit(effective_limit + 1)
        )

        if cursor_received_at:
            if cursor_email_id:
                stmt = stmt.where(
                    or_(
                        col(Email.received_at) < cursor_received_at,
                        (
                            (col(Email.received_at) == cursor_received_at)
                            & (col(Email.email_id) < cursor_email_id)
                        ),
                    )
                )
            else:
                stmt = stmt.where(col(Email.received_at) < cursor_received_at)

        cleaned_from = _clean_email(from_email)
        if cleaned_from:
            stmt = stmt.where(func.lower(col(Email.from_email)) == cleaned_from)

        cleaned_subject = (subject_contains or "").strip()
        if cleaned_subject:
            stmt = stmt.where(col(Email.subject).ilike(f"%{cleaned_subject}%"))

        cleaned_query = (query or "").strip()
        if cleaned_query:
            pattern = f"%{cleaned_query}%"
            stmt = stmt.where(
                or_(
                    col(Email.subject).ilike(pattern),
                    col(Email.from_email).ilike(pattern),
                    col(Email.from_name).ilike(pattern),
                    col(Email.snippet).ilike(pattern),
                    col(Email.body_text).ilike(pattern),
                )
            )

        try:
            result = await session.execute(stmt)
            emails = result.scalars().all()
        except Exception:
            logger.exception("search_mailbox_emails: failed to search emails")
            emails = []

    page_emails = emails[:effective_limit]
    has_more = len(emails) > effective_limit
    next_cursor_email = page_emails[-1] if has_more and page_emails else None

    return {
        "status": "success",
        "mailbox_email": selected_email,
        "integration_id": str(integration.integration_id),
        "days_back": effective_days,
        "items": [
            _format_email(email, include_body_preview=include_body_preview) for email in page_emails
        ],
        "has_more": has_more,
        "next_before_received_at": next_cursor_email.received_at.isoformat()
        if next_cursor_email and next_cursor_email.received_at
        else None,
        "next_before_email_id": str(next_cursor_email.email_id) if next_cursor_email else None,
    }


def create_list_connected_email_accounts(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory for the connected email account listing tool."""

    async def list_connected_email_accounts_tool() -> list[dict[str, Any]]:
        """List the current user's connected email accounts and read readiness."""
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="ListConnectedEmailAccounts",
            tool_func=list_connected_email_accounts,
            parameters={"context": context},
            description="Listing connected email accounts",
            fallback_value=[],
        )
        return list(result) if result else []

    return list_connected_email_accounts_tool


def create_search_mailbox_emails(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory for the mailbox-scoped email search tool."""

    async def search_mailbox_emails_tool(
        mailbox_email: str,
        days_back: int = 14,
        query: str | None = None,
        from_email: str | None = None,
        subject_contains: str | None = None,
        limit: int = 20,
        include_body_preview: bool = False,
        before_received_at: str | None = None,
        before_email_id: str | None = None,
    ) -> dict[str, Any]:
        """Search synced emails for one explicitly selected connected mailbox."""
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="SearchMailboxEmails",
            tool_func=search_mailbox_emails,
            parameters={
                "context": context,
                "mailbox_email": mailbox_email,
                "days_back": days_back,
                "query": query,
                "from_email": from_email,
                "subject_contains": subject_contains,
                "limit": limit,
                "include_body_preview": include_body_preview,
                "before_received_at": before_received_at,
                "before_email_id": before_email_id,
            },
            description=f"Searching {mailbox_email} emails",
            fallback_value={
                "status": "error",
                "code": "mailbox_email_search_failed",
                "message": "Could not search mailbox emails.",
            },
        )
        return dict(result)

    return search_mailbox_emails_tool


__all__ = [
    "list_connected_email_accounts",
    "search_mailbox_emails",
    "create_list_connected_email_accounts",
    "create_search_mailbox_emails",
]
