"""Mailbox-scoped email read tools for agents."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import String, func, or_, select
from sqlalchemy import cast as sql_cast
from sqlmodel import col

from src.agents.tools.base import execute_with_capabilities
from src.agents.tools.crm.access import AgentCRMAccessError, resolve_agent_access_actor
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.domain.models.organisation_tables.sync.calendar import AssociationStatus
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.crm.leads import CRMLead
from src.domain.models.organisation_tables.sync.email_models import DealEmailAssociation, Email
from src.infra.config.google_integration_scopes import GOOGLE_SCOPE_GMAIL_READONLY
from src.service.crm.access.record_access_policy import DealAccessPolicy, LeadAccessPolicy
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
_THREAD_CANDIDATE_BATCH = 100
_THREAD_HISTORY_LIMIT = 25
_THREAD_RESULT_LIMIT = 20


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
            return cast(KnowledgeIntegration, integration)
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
        "is_starred": getattr(email, "is_starred", False),
        "is_important": email.is_important,
        "has_attachments": email.has_attachments,
        "labels": email.labels or [],
        "snippet": email.snippet,
    }
    if include_body_preview:
        item["body_preview"] = (email.body_text or email.snippet or "")[:1000]
    return item


def _thread_key(email: Email) -> str:
    """Keep provider threads separate while retaining messages without a thread ID."""
    return email.thread_id or f"message:{email.email_id}"


def _thread_summary(
    messages: list[Email],
    *,
    mailbox_email: str,
    include_body_preview: bool,
    internal_domains: set[str],
    thread_truncated: bool = False,
) -> dict[str, Any]:
    """Summarise reply state from the complete locally synced provider thread.

    The latest sender is deliberately returned as evidence rather than attempting
    to infer whether a short acknowledgement semantically deserves another reply.
    The email workflow agent makes that judgement after reading the thread.
    """
    ordered = sorted(messages, key=lambda item: (item.received_at, item.email_id))
    latest = ordered[-1]
    latest_from_me = _clean_email(latest.from_email) == mailbox_email
    participant_emails = {
        cleaned
        for message in ordered
        for value in [message.from_email, *(message.to_emails or []), *(message.cc_emails or [])]
        if (cleaned := _clean_email(value)) and cleaned != mailbox_email
    }
    external_participants = sorted(
        email for email in participant_emails if email.rsplit("@", 1)[-1] not in internal_domains
    )
    attention_state = "waiting_on_them" if latest_from_me else "pending_from_me"
    reasons = ["latest_message_is_outbound" if latest_from_me else "latest_message_is_inbound"]
    if any(not message.is_read for message in ordered):
        reasons.append("contains_unread")
    if any(message.is_important for message in ordered):
        reasons.append("provider_marked_important")
    if any(getattr(message, "is_starred", False) for message in ordered):
        reasons.append("provider_starred")

    return {
        "thread_id": latest.thread_id,
        "latest_internet_message_id": latest.internet_message_id,
        "subject": latest.subject,
        "latest_received_at": latest.received_at.isoformat(),
        "message_count": len(ordered),
        "thread_truncated": thread_truncated,
        "unread_count": sum(not message.is_read for message in ordered),
        "attention_state": attention_state,
        "attention_reasons": reasons,
        "latest_sender": latest.from_email,
        "external_participants": external_participants,
        "is_external": bool(external_participants),
        "has_attachments": any(message.has_attachments for message in ordered),
        "is_important": any(message.is_important for message in ordered),
        "is_starred": any(getattr(message, "is_starred", False) for message in ordered),
        "_internet_message_ids": [
            message.internet_message_id for message in ordered if message.internet_message_id
        ],
        "latest_message": _format_email(latest, include_body_preview=include_body_preview),
    }


async def _enrich_thread_summaries(
    *,
    context: AgentContext,
    summaries: list[dict[str, Any]],
) -> bool:
    """Attach CRM evidence the current actor may view without gating discovery.

    Returns False when CRM identity or queries are unavailable. Mailbox results
    remain usable in that case, while explicit deal/lead filters can fail clearly.
    """
    if not summaries:
        return True
    for summary in summaries:
        summary["deals"] = []
        summary["leads"] = []
    message_to_summary = {
        message_id: summary
        for summary in summaries
        for message_id in summary["_internet_message_ids"]
    }
    participant_emails = {
        email for summary in summaries for email in summary["external_participants"]
    }
    participant_domains = {email.rsplit("@", 1)[-1] for email in participant_emails if "@" in email}
    try:
        actor = await resolve_agent_access_actor(context)
        async with context.acquire_session() as session:
            if message_to_summary:
                assoc_c = cast(Any, DealEmailAssociation).__table__.c
                deal_c = cast(Any, CRMDeal).__table__.c
                deal_stmt = (
                    select(DealEmailAssociation, CRMDeal)
                    .join(CRMDeal, deal_c.deal_id == assoc_c.deal_id)
                    .where(
                        assoc_c.organisation_id == context.organisation_id,
                        assoc_c.internet_message_id.in_(message_to_summary),
                        assoc_c.status == AssociationStatus.ACTIVE.value,
                        deal_c.organisation_id == context.organisation_id,
                        deal_c.is_deleted.is_(False),
                        DealAccessPolicy(
                            session, context.organisation_id, actor
                        ).visible_deal_predicate(deal_c),
                    )
                )
                for association, deal in (await session.execute(deal_stmt)).all():
                    matched_summary = message_to_summary.get(association.internet_message_id)
                    if matched_summary is None:
                        continue
                    deals = matched_summary.setdefault("deals", [])
                    if any(item["deal_id"] == str(deal.deal_id) for item in deals):
                        continue
                    deals.append(
                        {
                            "deal_id": str(deal.deal_id),
                            "name": deal.name,
                            "company_name": deal.company_name,
                            "stage": deal.stage_label or deal.stage_name,
                            "status": getattr(deal.deal_status, "value", deal.deal_status),
                            "priority": deal.priority,
                            "amount": float(deal.amount) if deal.amount is not None else None,
                            "currency": deal.currency,
                            "expected_close_date": deal.expected_close_date.isoformat()
                            if deal.expected_close_date
                            else None,
                        }
                    )

            if participant_emails or participant_domains:
                lead_c = cast(Any, CRMLead).__table__.c
                lead_matches = []
                if participant_emails:
                    lead_matches.append(func.lower(func.trim(lead_c.email)).in_(participant_emails))
                if participant_domains:
                    lead_matches.append(
                        func.lower(func.trim(lead_c.company_domain)).in_(participant_domains)
                    )
                lead_stmt = select(CRMLead).where(
                    lead_c.organisation_id == context.organisation_id,
                    lead_c.is_deleted.is_(False),
                    or_(*lead_matches),
                    LeadAccessPolicy(
                        session, context.organisation_id, actor
                    ).visible_lead_predicate(lead_c),
                )
                leads = (await session.execute(lead_stmt)).scalars().all()
                for summary in summaries:
                    emails = set(summary["external_participants"])
                    domains = {email.rsplit("@", 1)[-1] for email in emails if "@" in email}
                    for lead in leads:
                        if _clean_email(lead.email) not in emails and (
                            (lead.company_domain or "").strip().lower() not in domains
                        ):
                            continue
                        summary.setdefault("leads", []).append(
                            {
                                "lead_id": str(lead.lead_id),
                                "name": lead.full_name,
                                "company_name": lead.company_name,
                                "status": getattr(lead.status, "value", lead.status),
                                "priority": getattr(lead.priority, "value", lead.priority),
                                "score": float(lead.score) if lead.score is not None else None,
                            }
                        )
    except AgentCRMAccessError:
        logger.info("search_mailbox_emails: CRM actor unavailable for enrichment")
        return False
    except Exception:
        logger.warning("search_mailbox_emails: CRM enrichment failed", exc_info=True)
        return False

    return True


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
    result_mode: str = "messages",
    attention_filter: str = "all",
    business_scope: str = "all",
    start_at: str | None = None,
    end_at: str | None = None,
    is_important: bool | None = None,
    is_starred: bool | None = None,
) -> dict[str, Any]:
    """Search messages or locally complete threads in one connected mailbox.

    Message mode preserves the original raw-email contract. Thread mode discovers
    conversations from the requested time window, then loads their older synced
    messages so reply state is not guessed from a single page of recent messages.
    """
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

    if result_mode not in {"messages", "threads"}:
        return {
            "status": "error",
            "code": "invalid_result_mode",
            "message": "result_mode must be messages or threads.",
        }
    if attention_filter not in {
        "all",
        "priority",
        "pending_from_me",
        "waiting_on_them",
        "unread",
    }:
        return {
            "status": "error",
            "code": "invalid_attention_filter",
            "message": "The requested mailbox attention filter is not supported.",
        }
    if business_scope not in {"all", "external", "internal", "deals", "leads"}:
        return {
            "status": "error",
            "code": "invalid_business_scope",
            "message": "The requested mailbox business scope is not supported.",
        }
    if result_mode == "messages" and (attention_filter != "all" or business_scope != "all"):
        return {
            "status": "error",
            "code": "thread_mode_required",
            "message": "Attention and business filters require result_mode=threads.",
        }

    try:
        parsed_start = _parse_iso_datetime(start_at)
        parsed_end = _parse_iso_datetime(end_at)
    except ValueError:
        return {
            "status": "error",
            "code": "invalid_date_range",
            "message": "start_at and end_at must be ISO 8601 timestamps.",
        }
    if parsed_start and parsed_end and parsed_start >= parsed_end:
        return {
            "status": "error",
            "code": "invalid_date_range",
            "message": "start_at must be earlier than end_at.",
        }

    internal_domains: set[str] = set()
    candidate_emails: list[Email] = []
    if result_mode == "threads":
        if "@" in selected_email:
            internal_domains.add(selected_email.rsplit("@", 1)[-1])
        try:
            from src.service.crm.utils.internal_domains import get_internal_domains

            internal_domains.update(await get_internal_domains(context.organisation_id))
        except Exception:
            logger.warning(
                "search_mailbox_emails: could not load internal domains",
                exc_info=True,
            )

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
        cutoff = parsed_start or datetime.now(UTC) - timedelta(days=effective_days)
        try:
            cursor_received_at = _parse_iso_datetime(before_received_at)
            cursor_email_id = _parse_uuid(before_email_id)
        except ValueError:
            return {
                "status": "error",
                "code": "invalid_cursor",
                "message": "The mailbox email pagination cursor is invalid.",
            }

        discovery_filters = [
            col(Email.organisation_id) == context.organisation_id,
            col(Email.integration_id) == integration.integration_id,
            col(Email.is_deleted).is_(False),
            col(Email.received_at) >= cutoff,
        ]

        if parsed_end:
            discovery_filters.append(col(Email.received_at) < parsed_end)

        if cursor_received_at and result_mode == "messages":
            if cursor_email_id:
                discovery_filters.append(
                    or_(
                        col(Email.received_at) < cursor_received_at,
                        (
                            (col(Email.received_at) == cursor_received_at)
                            & (col(Email.email_id) < cursor_email_id)
                        ),
                    )
                )
            else:
                discovery_filters.append(col(Email.received_at) < cursor_received_at)

        cleaned_from = _clean_email(from_email)
        if cleaned_from:
            discovery_filters.append(func.lower(col(Email.from_email)) == cleaned_from)

        cleaned_subject = (subject_contains or "").strip()
        if cleaned_subject:
            discovery_filters.append(col(Email.subject).ilike(f"%{cleaned_subject}%"))

        cleaned_query = (query or "").strip()
        if cleaned_query:
            pattern = f"%{cleaned_query}%"
            discovery_filters.append(
                or_(
                    col(Email.subject).ilike(pattern),
                    col(Email.from_email).ilike(pattern),
                    col(Email.from_name).ilike(pattern),
                    col(Email.snippet).ilike(pattern),
                    col(Email.body_text).ilike(pattern),
                )
            )

        if is_important is not None:
            discovery_filters.append(col(Email.is_important).is_(is_important))
        if is_starred is not None:
            discovery_filters.append(col(Email.is_starred).is_(is_starred))
        if attention_filter == "unread":
            discovery_filters.append(col(Email.is_read).is_(False))

        if result_mode == "threads":
            # Rank inside each provider thread so pagination operates on distinct
            # conversations rather than letting a busy thread consume the batch.
            discovery_thread_key = func.coalesce(
                email_c.thread_id,
                sql_cast(email_c.email_id, String),
            )
            ranked_discovery = (
                select(
                    email_c.email_id.label("email_id"),
                    email_c.received_at.label("received_at"),
                    func.row_number()
                    .over(
                        partition_by=discovery_thread_key,
                        order_by=(email_c.received_at.desc(), email_c.email_id.desc()),
                    )
                    .label("thread_rank"),
                )
                .where(*discovery_filters)
                .subquery("ranked_mailbox_threads")
            )
            stmt = (
                select(Email)
                .join(ranked_discovery, email_c.email_id == ranked_discovery.c.email_id)
                .where(ranked_discovery.c.thread_rank == 1)
            )
            if cursor_received_at:
                if cursor_email_id:
                    stmt = stmt.where(
                        or_(
                            ranked_discovery.c.received_at < cursor_received_at,
                            (
                                (ranked_discovery.c.received_at == cursor_received_at)
                                & (ranked_discovery.c.email_id < cursor_email_id)
                            ),
                        )
                    )
                else:
                    stmt = stmt.where(ranked_discovery.c.received_at < cursor_received_at)
            stmt = stmt.order_by(
                ranked_discovery.c.received_at.desc(),
                ranked_discovery.c.email_id.desc(),
            ).limit(_THREAD_CANDIDATE_BATCH + 1)
        else:
            stmt = (
                select(Email)
                .where(*discovery_filters)
                .order_by(email_c.received_at.desc(), email_c.email_id.desc())
                .limit(effective_limit + 1)
            )

        try:
            result = await session.execute(stmt)
            emails = result.scalars().all()
        except Exception:
            logger.exception("search_mailbox_emails: failed to search emails")
            return {
                "status": "error",
                "code": "mailbox_email_search_failed",
                "message": "Could not search mailbox emails.",
            }

        discovery_truncated = result_mode == "threads" and len(emails) > _THREAD_CANDIDATE_BATCH
        if result_mode == "threads" and emails:
            candidate_emails = list(emails[:_THREAD_CANDIDATE_BATCH])
            discovered_keys = {_thread_key(email) for email in candidate_emails}
            thread_ids = {
                email.thread_id for email in candidate_emails if email.thread_id is not None
            }
            standalone_ids = {
                email.email_id for email in candidate_emails if email.thread_id is None
            }
            history_filters = []
            if thread_ids:
                history_filters.append(col(Email.thread_id).in_(thread_ids))
            if standalone_ids:
                history_filters.append(col(Email.email_id).in_(standalone_ids))
            history_thread_key = func.coalesce(
                email_c.thread_id,
                sql_cast(email_c.email_id, String),
            )
            ranked_history = (
                select(
                    email_c.email_id.label("email_id"),
                    func.row_number()
                    .over(
                        partition_by=history_thread_key,
                        order_by=(email_c.received_at.desc(), email_c.email_id.desc()),
                    )
                    .label("history_rank"),
                )
                .where(
                    col(Email.organisation_id) == context.organisation_id,
                    col(Email.integration_id) == integration.integration_id,
                    col(Email.is_deleted).is_(False),
                    or_(*history_filters),
                )
                .subquery("ranked_thread_history")
            )
            history_stmt = (
                select(Email)
                .join(ranked_history, email_c.email_id == ranked_history.c.email_id)
                .where(ranked_history.c.history_rank <= _THREAD_HISTORY_LIMIT + 1)
                .order_by(email_c.received_at.asc(), email_c.email_id.asc())
            )
            try:
                history_result = await session.execute(history_stmt)
                emails = [
                    email
                    for email in history_result.scalars().all()
                    if _thread_key(email) in discovered_keys
                ]
            except Exception:
                logger.exception("search_mailbox_emails: failed to load thread history")
                return {
                    "status": "error",
                    "code": "mailbox_thread_search_failed",
                    "message": "Could not load complete mailbox threads.",
                }

    if result_mode == "threads":
        grouped: dict[str, list[Email]] = {}
        for email in emails:
            grouped.setdefault(_thread_key(email), []).append(email)
        summaries = []
        for messages in grouped.values():
            thread_truncated = len(messages) > _THREAD_HISTORY_LIMIT
            summaries.append(
                _thread_summary(
                    messages[-_THREAD_HISTORY_LIMIT:],
                    mailbox_email=selected_email,
                    include_body_preview=include_body_preview,
                    internal_domains=internal_domains,
                    thread_truncated=thread_truncated,
                )
            )
        crm_enrichment_available = await _enrich_thread_summaries(
            context=context,
            summaries=summaries,
        )
        if attention_filter in {"pending_from_me", "waiting_on_them"}:
            summaries = [item for item in summaries if item["attention_state"] == attention_filter]
        elif attention_filter == "priority":
            summaries = [
                item
                for item in summaries
                if item["attention_state"] == "pending_from_me"
                or item["is_important"]
                or item["is_starred"]
                or item["deals"]
                or item["leads"]
            ]
        if business_scope == "external":
            summaries = [item for item in summaries if item["is_external"]]
        elif business_scope == "internal":
            summaries = [item for item in summaries if not item["is_external"]]
        elif business_scope in {"deals", "leads"} and not crm_enrichment_available:
            return {
                "status": "error",
                "code": "business_scope_unavailable",
                "message": f"Could not verify visible CRM {business_scope} for this mailbox search.",
            }
        elif business_scope == "deals":
            summaries = [item for item in summaries if item["deals"]]
        elif business_scope == "leads":
            summaries = [item for item in summaries if item["leads"]]
        summaries_by_key = {
            summary["thread_id"] or f"message:{summary['latest_message']['email_id']}": summary
            for summary in summaries
        }
        page_items: list[dict[str, Any]] = []
        scanned_candidate: Email | None = None
        scanned_count = 0
        for candidate in candidate_emails:
            scanned_count += 1
            scanned_candidate = candidate
            summary = summaries_by_key.get(_thread_key(candidate))
            if summary is not None and summary in summaries:
                summary.pop("_internet_message_ids", None)
                page_items.append(summary)
                if len(page_items) == min(effective_limit, _THREAD_RESULT_LIMIT):
                    break
        has_unscanned_candidates = scanned_count < len(candidate_emails)
        has_more = has_unscanned_candidates or discovery_truncated
        next_cursor_candidate = scanned_candidate if has_more else None
        return {
            "status": "success",
            "mailbox_email": selected_email,
            "integration_id": str(integration.integration_id),
            "result_mode": "threads",
            "days_back": effective_days,
            "start_at": parsed_start.isoformat() if parsed_start else None,
            "end_at": parsed_end.isoformat() if parsed_end else None,
            "crm_enrichment_available": crm_enrichment_available,
            "items": page_items,
            "has_more": has_more,
            "next_before_received_at": next_cursor_candidate.received_at.isoformat()
            if next_cursor_candidate
            else None,
            "next_before_email_id": str(next_cursor_candidate.email_id)
            if next_cursor_candidate
            else None,
        }

    page_emails = emails[:effective_limit]
    has_more = len(emails) > effective_limit
    next_cursor_email = page_emails[-1] if has_more and page_emails else None

    return {
        "status": "success",
        "mailbox_email": selected_email,
        "integration_id": str(integration.integration_id),
        "result_mode": "messages",
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
        result_mode: str = "messages",
        attention_filter: str = "all",
        business_scope: str = "all",
        start_at: str | None = None,
        end_at: str | None = None,
        is_important: bool | None = None,
        is_starred: bool | None = None,
    ) -> dict[str, Any]:
        """Search one connected Gmail or Outlook mailbox.

        Use ``result_mode=threads`` for priority, pending-reply, unread-thread,
        or attention workflows. It evaluates the latest message from the full
        locally synced thread, while the date range only determines which threads
        enter the result set. Use message mode for literal mailbox filtering.
        """
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
                "result_mode": result_mode,
                "attention_filter": attention_filter,
                "business_scope": business_scope,
                "start_at": start_at,
                "end_at": end_at,
                "is_important": is_important,
                "is_starred": is_starred,
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
