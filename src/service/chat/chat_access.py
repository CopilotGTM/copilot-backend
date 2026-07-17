"""Central authorization queries for durable web-chat sessions.

Chat services call these resolvers before reads or mutations. Both queries keep
organisation scope in SQL so an externally supplied session ID can never cross
the tenant boundary; readable access additionally accepts an explicit viewer grant.
"""

from uuid import UUID

from sqlalchemy import exists, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select
from src.domain.models.organisation_tables.chat_session_access_models import (
    ChatSessionAccess,
)
from src.domain.models.organisation_tables.chat_session_models import ChatSession


async def resolve_owned_chat_session(
    db: AsyncSession,
    *,
    organisation_id: UUID,
    user_id: UUID,
    session_id: UUID,
) -> ChatSession:
    """Return an active chat only when the caller is its owner.

    Mutation paths use this resolver so read-only grants cannot rename, delete,
    favorite, or append messages to another user's canonical chat.
    """
    result = await db.execute(
        select(ChatSession).where(
            col(ChatSession.session_id) == session_id,
            col(ChatSession.organisation_id) == organisation_id,
            col(ChatSession.user_id) == user_id,
            col(ChatSession.is_deleted).is_(False),
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise ValueError("Chat session not found")
    return session


async def resolve_readable_chat_session(
    db: AsyncSession,
    *,
    organisation_id: UUID,
    user_id: UUID,
    session_id: UUID,
) -> ChatSession:
    """Return an active chat when the caller owns it or has a viewer grant.

    The grant subquery repeats organisation and session scope deliberately: a
    viewer identity or session UUID alone is never trusted as an access boundary.
    """
    grant_exists = exists(
        select(ChatSessionAccess.session_id).where(
            col(ChatSessionAccess.organisation_id) == organisation_id,
            col(ChatSessionAccess.session_id) == session_id,
            col(ChatSessionAccess.viewer_user_id) == user_id,
        )
    )
    result = await db.execute(
        select(ChatSession).where(
            col(ChatSession.session_id) == session_id,
            col(ChatSession.organisation_id) == organisation_id,
            col(ChatSession.is_deleted).is_(False),
            or_(col(ChatSession.user_id) == user_id, grant_exists),
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise ValueError("Chat session not found")
    return session
