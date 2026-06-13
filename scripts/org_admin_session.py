"""Admin-backed org session helper for maintenance scripts.

Runtime tenant sessions may need per-tenant DB passwords. Data-migration scripts
run with the master/admin connection and set the tenant schema through
search_path, matching the org migration credential model.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine
from sqlmodel import Session
from src.infra.database.tenant_search_path import (
    TENANT_SCHEMA_SESSION_KEY,
    TENANT_SEARCH_PATH_SESSION_KEY,
    TRUSTED_EXTENSION_SCHEMA,
)
from src.infra.utils.get_master_db_url import get_master_db_url
from src.infra.utils.resource_naming import get_schema_name


class AdminAsyncSessionAdapter:
    """Tiny async facade over a sync admin SQLAlchemy session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        return self._session.execute(*args, **kwargs)

    def add(self, instance: object) -> None:
        self._session.add(instance)

    async def flush(self) -> None:
        self._session.flush()

    async def commit(self) -> None:
        self._session.commit()

    async def rollback(self) -> None:
        self._session.rollback()


@asynccontextmanager
async def acquire_org_admin_async_session(
    organisation_id: UUID,
) -> AsyncIterator[AdminAsyncSessionAdapter]:
    engine = create_engine(get_master_db_url(), future=True)
    session = Session(engine)
    schema = get_schema_name(organisation_id)
    session.info[TENANT_SCHEMA_SESSION_KEY] = schema
    session.info[TENANT_SEARCH_PATH_SESSION_KEY] = (
        schema,
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
