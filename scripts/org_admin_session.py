"""Run legacy-shaped async maintenance jobs through one shared admin session.

These scripts still await session methods, so the adapter preserves that call
shape while binding every transaction to ``tenant_data`` and one organisation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine
from sqlmodel import Session
from src.infra.database.tenant_search_path import (
    TENANT_DATA_SCHEMA,
    TENANT_ORGANISATION_ID_SESSION_KEY,
    TENANT_SCHEMA_SESSION_KEY,
    TENANT_SEARCH_PATH_SESSION_KEY,
    TRUSTED_EXTENSION_SCHEMA,
)
from src.infra.utils.get_master_db_url import get_master_db_url


class AdminAsyncSessionAdapter:
    """Tiny async facade over a sync admin SQLAlchemy session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        """Execute without an implicit ORM flush changing maintenance order."""
        with self._session.no_autoflush:
            return self._session.execute(*args, **kwargs)

    def add(self, instance: object) -> None:
        """Stage an ORM instance in the wrapped admin session."""
        self._session.add(instance)

    async def flush(self) -> None:
        """Flush staged changes through the synchronous session."""
        self._session.flush()

    async def commit(self) -> None:
        """Commit the current tenant-bound transaction."""
        self._session.commit()

    async def rollback(self) -> None:
        """Roll back the current tenant-bound transaction."""
        self._session.rollback()


@asynccontextmanager
async def acquire_org_admin_async_session(
    organisation_id: UUID,
) -> AsyncIterator[AdminAsyncSessionAdapter]:
    """Yield an admin session scoped to one organisation in ``tenant_data``."""
    engine = create_engine(get_master_db_url(), future=True)
    session = Session(engine, expire_on_commit=False)
    session.info[TENANT_ORGANISATION_ID_SESSION_KEY] = organisation_id
    session.info[TENANT_SCHEMA_SESSION_KEY] = TENANT_DATA_SCHEMA
    session.info[TENANT_SEARCH_PATH_SESSION_KEY] = (
        TENANT_DATA_SCHEMA,
        TRUSTED_EXTENSION_SCHEMA,
        "public",
    )
    try:
        yield AdminAsyncSessionAdapter(session)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        engine.dispose()
