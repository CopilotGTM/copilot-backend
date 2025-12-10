"""Agent execution context encapsulating session and metadata.

This module provides AgentContext, a unified container for runtime dependencies
(database session, organisation/user IDs, etc.) passed to agents and tools.
Using a context object instead of individual parameters improves:
- Type safety and validation
- API ergonomics (single parameter vs many)
- Extensibility (add tracing, caching without signature changes)
- Connection pooling (session acquired once and reused)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class AgentContext:
    """Runtime context for agent execution.

    Encapsulates database session and tenant metadata required by agents and tools.

    Attributes:
        session: SQLAlchemy async session (organisation-scoped)
        organisation_id: Tenant organisation UUID
        org_name: Organisation display name (for prompt personalization)
        user_id: Current user UUID (optional, for user-scoped queries)
        deal_id: Associated deal identifier (optional, for deal-specific filtering)
        message_id: Current message correlation ID (optional, for tracing)
        metadata: Extensible dict for additional context (caching, rate limiting, etc.)
    """

    session: AsyncSession
    organisation_id: UUID
    org_name: str = "Our Organization"
    user_id: UUID | None = None
    deal_id: str | None = None
    message_id: str | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        """Validate required fields."""
        if not self.session:
            raise ValueError("AgentContext requires a valid session")
        if not self.organisation_id:
            raise ValueError("AgentContext requires organisation_id")

    async def search_internal_knowledge(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[dict[str, str]]:
        """Search internal knowledge base (convenience wrapper).

        Args:
            query: Search query string
            limit: Maximum number of results (default 5)

        Returns:
            List of dicts with keys: label, url, snippet
        """
        from src.agents.tools.internal_knowledge_base import internal_knowledge_base

        return await internal_knowledge_base(
            session=self.session,
            organisation_id=self.organisation_id,
            query=query,
            limit=limit,
            user_id=self.user_id,
            deal_id=self.deal_id,
        )

    def with_message_id(self, message_id: str) -> AgentContext:
        """Create a copy with updated message_id (immutable-style update)."""
        return AgentContext(
            session=self.session,
            organisation_id=self.organisation_id,
            org_name=self.org_name,
            user_id=self.user_id,
            deal_id=self.deal_id,
            message_id=message_id,
            metadata=self.metadata.copy(),
        )

    def with_metadata(self, **kwargs) -> AgentContext:
        """Create a copy with merged metadata."""
        new_metadata = {**self.metadata, **kwargs}
        return AgentContext(
            session=self.session,
            organisation_id=self.organisation_id,
            org_name=self.org_name,
            user_id=self.user_id,
            deal_id=self.deal_id,
            message_id=self.message_id,
            metadata=new_metadata,
        )


__all__ = ["AgentContext"]
