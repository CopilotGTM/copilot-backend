"""Database infrastructure package.

Re-exports session acquisition functions for convenient importing.

Usage:
    from src.infra.database import acquire_org_async_session

    async for session in acquire_org_async_session(org_uuid):
        # Use session
"""

from src.infra.database.sessions import (
    acquire_master_async_session,
    acquire_master_session,
    acquire_org_async_session,
    acquire_org_session,
)

__all__ = [
    "acquire_master_session",
    "acquire_master_async_session",
    "acquire_org_session",
    "acquire_org_async_session",
]
