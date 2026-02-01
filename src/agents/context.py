"""Agent execution context encapsulating organisation metadata.

This module provides AgentContext, a unified container for runtime dependencies
(organisation/user IDs, etc.) passed to agents and tools.
Using a context object instead of individual parameters improves:
- Type safety and validation
- API ergonomics (single parameter vs many)
- Extensibility (add tracing, caching without signature changes)
- Fresh session pattern (sessions acquired on-demand, released promptly)

SECURITY BOUNDARIES (immutable, baked into context at initialization):
- organisation_id: Tools use context.organisation_id, cannot accept as parameter.
  This prevents cross-org data access attacks. Agents CANNOT modify this value.
- deal_id: For deal-scoped agents, tools use context.deal_id, cannot accept as parameter.
  This prevents agents from querying data from other deals.

IMPORTANT: organisation_id and deal_id are set at context creation and CANNOT be
modified by agents or tools. This is a critical security feature that prevents
agents from accessing data from other organisations or deals.

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
    Sessions are acquired on-demand via acquire_session() method - this prevents
    holding database connections during long-running LLM calls.

    SECURITY BOUNDARIES (immutable once set - CANNOT be modified by agents):
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
    """

    organisation_id: UUID
    deal_id: UUID | None = None  # SECURITY: Immutable for deal-scoped agents
    org_name: str = "Our Organization"
    user_id: UUID | None = None
    user_email: str | None = None
    channel: str = (
        "web"  # "web" or "teams" — used by tool registry to filter channel-specific tools
    )
    message_id: str | None = None
    metadata: dict = field(default_factory=dict)

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
        """Create a copy with updated message_id (immutable-style update).

        Note: organisation_id and deal_id are preserved (security boundaries).
        """
        return AgentContext(
            organisation_id=self.organisation_id,
            deal_id=self.deal_id,
            org_name=self.org_name,
            user_id=self.user_id,
            user_email=self.user_email,
            channel=self.channel,
            message_id=message_id,
            metadata=self.metadata.copy(),
        )

    def with_metadata(self, **kwargs) -> AgentContext:
        """Create a copy with merged metadata.

        Note: organisation_id and deal_id are preserved (security boundaries).
        """
        new_metadata = {**self.metadata, **kwargs}
        return AgentContext(
            organisation_id=self.organisation_id,
            deal_id=self.deal_id,
            org_name=self.org_name,
            user_id=self.user_id,
            user_email=self.user_email,
            channel=self.channel,
            message_id=self.message_id,
            metadata=new_metadata,
        )


__all__ = ["AgentContext"]
