"""Shared utilities for all Celery workers.

Functions here are used across multiple workers (deal, chat, etc.)
and operate on the master (public) schema.
"""

from __future__ import annotations

import contextlib
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from src.domain.models.master_tables.organisation_models import Organisation
from src.service.orgnisation.kai_training_service import load_organisation_runtime_context


async def get_organisation_name(session: AsyncSession, org_id: UUID) -> str | None:
    """Load the organisation display name from the public schema (best-effort).

    The Organisation table lives in the public (master) schema, so callers
    should pass a master session (``acquire_master_async_session``).

    Returns None on failure so callers can fall back to a default name.
    """
    stmt = select(col(Organisation.organisation_name)).where(
        col(Organisation.organisation_id) == org_id,
    )
    try:
        result = await session.execute(stmt)
        return result.scalar_one_or_none()
    except SQLAlchemyError:
        with contextlib.suppress(Exception):
            await session.rollback()
        return None


async def get_organisation_agent_context(
    session: AsyncSession, org_id: UUID
) -> tuple[str | None, str | None]:
    """Load the name and effective Kai profile for an agent invocation."""
    try:
        return await load_organisation_runtime_context(session, org_id)
    except SQLAlchemyError:
        with contextlib.suppress(Exception):
            await session.rollback()
        return None, None


async def get_organisation_briefing_context(
    session: AsyncSession,
    org_id: UUID,
) -> tuple[str | None, list[str], str | None]:
    """Load seller identity and website context for a meeting briefing.

    Meeting-prep workers use the public-schema record instead of knowledge
    documents so the briefing agent receives an authoritative organisation name,
    configured websites, and summary before it performs any web research.
    Missing records and database failures return empty values because seller
    context should improve a briefing, not make generation fail.
    """
    stmt = select(
        col(Organisation.organisation_name),
        col(Organisation.website_urls),
        col(Organisation.summary),
    ).where(col(Organisation.organisation_id) == org_id)
    try:
        result = await session.execute(stmt)
        row = result.one_or_none()
        if not row:
            return None, [], None
        return row.organisation_name, list(row.website_urls or []), row.summary
    except SQLAlchemyError:
        # Seller context is optional for generation. Roll back the failed master
        # query and let the worker continue with its existing safe defaults.
        with contextlib.suppress(Exception):
            await session.rollback()
        return None, [], None
