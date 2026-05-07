"""Resolve active integration emails for user-owned integrations.

Used by deal mappers to expand internal team participants with their connected
mailbox/calendar accounts.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationType,
    KnowledgeIntegration,
)

EMAIL_MATCHER_INTEGRATION_TYPES = (IntegrationType.GMAIL, IntegrationType.OUTLOOK_EMAIL)
CALENDAR_MATCHER_INTEGRATION_TYPES = (
    IntegrationType.GOOGLE_CALENDAR,
    IntegrationType.OUTLOOK_CALENDAR,
)


def _normalize_email(email: str | None) -> str | None:
    """Normalize email to canonical form used for stakeholder matching."""
    if not email:
        return None

    normalized = email.strip().lower()
    if not normalized or normalized == "null":
        return None

    return normalized


async def get_active_integration_emails_for_users(
    session: AsyncSession,
    *,
    organisation_id: UUID,
    created_by_user_ids: Iterable[UUID],
    integration_types: tuple[IntegrationType, ...],
) -> set[str]:
    """Resolve active integration emails for one or more internal user ids.

    Args:
        session: Async session in the org schema.
        organisation_id: Organization boundary for lookup.
        created_by_user_ids: Internal CRM user IDs from deal participants.
        integration_types: Allowed integration types to resolve (e.g., Gmail only).

    Returns:
        Set of normalized integration emails for the matching users.
    """
    emails_by_user = await get_active_integration_emails_grouped_by_user(
        session=session,
        organisation_id=organisation_id,
        created_by_user_ids=created_by_user_ids,
        integration_types=integration_types,
    )

    return {email for emails in emails_by_user.values() for email in emails}


async def get_active_integration_emails_grouped_by_user(
    session: AsyncSession,
    *,
    organisation_id: UUID,
    created_by_user_ids: Iterable[UUID],
    integration_types: tuple[IntegrationType, ...],
) -> dict[UUID, set[str]]:
    """Resolve active integration emails grouped by owning internal user id."""
    user_ids = list(created_by_user_ids)
    if not user_ids:
        return {}

    stmt = (
        select(
            col(KnowledgeIntegration.created_by_user_id),
            col(KnowledgeIntegration.integration_email),
        )
        .where(
            col(KnowledgeIntegration.organisation_id) == organisation_id,
            col(KnowledgeIntegration.created_by_user_id).in_(user_ids),
            col(KnowledgeIntegration.integration_type).in_(integration_types),
            col(KnowledgeIntegration.is_active).is_(True),
            col(KnowledgeIntegration.integration_email).is_not(None),
            func.trim(col(KnowledgeIntegration.integration_email)) != "",
        )
        .distinct()
    )
    result = await session.execute(stmt)

    integration_emails_by_user: dict[UUID, set[str]] = {}
    for created_by_user_id, integration_email in result.all():
        normalized = _normalize_email(integration_email)
        if normalized and created_by_user_id:
            integration_emails_by_user.setdefault(created_by_user_id, set()).add(normalized)

    return integration_emails_by_user
