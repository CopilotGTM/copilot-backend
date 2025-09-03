"""Master-level database session dependencies.

Centralizes acquisition of a master (shared/public) database Session so we
avoid duplicating inline `next(get_master_session())` patterns or direct
engine handling across routers, event handlers, and workers.

Exports:
  * get_master_session  - FastAPI dependency (yielding Session)
  * acquire_master_session  - Imperative generator for long-lived flows

Backed by the new provider abstraction (`get_sync_provider`).
"""
from __future__ import annotations

from typing import Iterator
from sqlmodel import Session

from src.infra.database.factory import get_sync_provider


def _master_session_gen() -> Iterator[Session]:
    provider = get_sync_provider()
    with provider.master_session() as session:  # type: ignore
        yield session


def get_master_session() -> Iterator[Session]:
    """FastAPI dependency yielding a master-scoped Session.

    Use in endpoint signatures: `session: Session = Depends(get_master_session)`.
    """
    yield from _master_session_gen()


def acquire_master_session() -> Iterator[Session]:
    """Imperative helper for code outside FastAPI dependency system.

    Example:
        sess_gen = acquire_master_session()
        session = next(sess_gen)
        try:
            ...
        finally:
            next(sess_gen, None)
    """
    return _master_session_gen()

__all__ = [
    "get_master_session",
    "acquire_master_session",
]