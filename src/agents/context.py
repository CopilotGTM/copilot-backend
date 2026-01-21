"""Agent execution context encapsulating session and metadata.

This module provides AgentContext, a unified container for runtime dependencies
(database session, organisation/user IDs, etc.) passed to agents and tools.
Using a context object instead of individual parameters improves:
- Type safety and validation
- API ergonomics (single parameter vs many)
- Extensibility (add tracing, caching without signature changes)
- Fresh session pattern (sessions acquired on-demand, released promptly)

SECURITY BOUNDARIES (immutable, baked into context):
- organisation_id: Tools use context.organisation_id, cannot accept as parameter.
  This prevents cross-org data access attacks.
- deal_id: For deal-scoped agents, tools use context.deal_id, cannot accept as parameter.
  This prevents agents from querying data from other deals.

TOOL CATEGORIES:
1. Deal-scoped tools (stakeholders, meetings, etc.) - use both organisation_id AND deal_id from context
2. Organisation-scoped tools (cross-deal queries) - use organisation_id from context, may query multiple deals
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class AgentContext:
    """Runtime context for agent execution.

    Encapsulates tenant metadata required by agents and tools.
    Session is acquired on-demand via acquire_session() method.

    SECURITY BOUNDARIES (immutable once set):
    - organisation_id: Required. Tools use context.organisation_id and cannot
      accept it as a parameter. Prevents cross-org data access.
    - deal_id: Optional. For deal-scoped agents, tools use context.deal_id and
      cannot accept it as a parameter. Prevents cross-deal data access.

    Attributes:
        organisation_id: Tenant organisation UUID (IMMUTABLE - security boundary)
        deal_id: Associated deal UUID (IMMUTABLE for deal-scoped agents - security boundary)
        org_name: Organisation display name (for prompt personalization)
        user_id: Current user UUID (optional, for user-scoped queries)
        message_id: Current message correlation ID (optional, for tracing)
        metadata: Extensible dict for additional context (caching, rate limiting, etc.)
        session: Legacy session field (deprecated - use acquire_session() instead)
    """

    organisation_id: UUID
    deal_id: UUID | None = None  # SECURITY: Immutable for deal-scoped agents
    org_name: str = "Our Organization"
    user_id: UUID | None = None
    message_id: str | None = None
    metadata: dict = field(default_factory=dict)
    # Legacy session field for backward compatibility with existing agents
    # New agents/tools should use acquire_session() instead
    session: AsyncSession | None = None

    def __post_init__(self):
        """Validate required fields."""
        if not self.organisation_id:
            raise ValueError("AgentContext requires organisation_id")

    @asynccontextmanager
    async def acquire_session(self) -> AsyncGenerator[AsyncSession, None]:
        """Acquire a fresh database session for org-scoped operations.

        This is the preferred way to access the database in tools.
        Sessions are acquired on-demand and released after use.

        Usage:
            async with context.acquire_session() as session:
                result = await session.execute(query)

        Yields:
            AsyncSession scoped to this organisation's schema
        """
        from src.infra.database.sessions import acquire_org_async_session

        async for session in acquire_org_async_session(self.organisation_id):
            yield session

    def with_message_id(self, message_id: str) -> AgentContext:
        """Create a copy with updated message_id (immutable-style update)."""
        return AgentContext(
            organisation_id=self.organisation_id,
            deal_id=self.deal_id,
            org_name=self.org_name,
            user_id=self.user_id,
            message_id=message_id,
            metadata=self.metadata.copy(),
            session=self.session,  # Preserve legacy session if set
        )

    def with_metadata(self, **kwargs) -> AgentContext:
        """Create a copy with merged metadata."""
        new_metadata = {**self.metadata, **kwargs}
        return AgentContext(
            organisation_id=self.organisation_id,
            deal_id=self.deal_id,
            org_name=self.org_name,
            user_id=self.user_id,
            message_id=self.message_id,
            metadata=new_metadata,
            session=self.session,  # Preserve legacy session if set
        )

    @property
    def has_legacy_session(self) -> bool:
        """Check if a legacy session is attached (for backward compatibility)."""
        return self.session is not None


__all__ = ["AgentContext"]
