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
