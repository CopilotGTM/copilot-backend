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
    user_ids = list(created_by_user_ids)
    if not user_ids:
        return set()

    stmt = (
        select(col(KnowledgeIntegration.integration_email))
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

    integration_emails = set()
    for integration_email in result.scalars().all():
        normalized = _normalize_email(integration_email)
        if normalized:
            integration_emails.add(normalized)

    return integration_emails
