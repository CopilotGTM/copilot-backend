"""Internal knowledge base search tool.

Searches organisation documents and returns normalized results.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.tools.base import db_session_lock, execute_with_capabilities
from src.domain.models.organisation_tables.knowledge_models import OrganisationDocument

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def internal_knowledge_base(
    session: AsyncSession | None = None,
    organisation_id: UUID | None = None,
    query: str | None = None,
    *,
    limit: int = 5,
    user_id: UUID | None = None,
    deal_id: str | None = None,
    context: AgentContext | None = None,
) -> list[dict[str, str]]:
    """Search internal knowledge base (organisation documents) and return normalized results.

    Can be called with either explicit parameters OR an AgentContext object:
    - New style: internal_knowledge_base(context=ctx, query="...", limit=5)
    - Legacy style: internal_knowledge_base(session=s, organisation_id=oid, query="...", ...)

    Returns a list of dictionaries with keys ``label``, ``url``, and ``snippet``.
    - ``label``: Document name or a fallback identifier
    - ``url``: Internal link format ``kb://org/{org_id}/doc/{document_id}#page={page}`` (if page available)
    - ``snippet``: Content excerpt (first 200 chars)

    Filters applied:
    - ``is_searchable = True``
    - ``is_archived = False``
    - Organisation-wide docs (``is_org_wide = True``) OR user's personal docs
    - Optional: filter by ``deal_id`` if provided

    Args:
        session: SQLAlchemy async session (organisation-scoped) - ignored if context provided
        organisation_id: The organisation UUID - ignored if context provided
        query: Search query string (basic ILIKE match on content)
        limit: Maximum number of results to return (default 5)
        user_id: Optional user UUID for filtering personal documents - ignored if context provided
        deal_id: Optional deal identifier for filtering by deal - ignored if context provided
        context: AgentContext object (preferred, contains session + metadata)
    """
    # Extract parameters from context if provided
    use_context_session = False
    if context:
        organisation_id = context.organisation_id
        user_id = user_id or context.user_id
        deal_id = deal_id or context.deal_id
        # Use context's acquire_session if no explicit session provided
        if session is None:
            use_context_session = True

    # Validate required parameters
    if not organisation_id:
        logger.warning("InternalKnowledgeBase: missing organisation_id")
        return []

    if not use_context_session and not session:
        logger.warning("InternalKnowledgeBase: missing session")
        return []

    trimmed_query = (query or "").strip()
    if not trimmed_query:
        logger.debug("InternalKnowledgeBase skipped: empty query provided")
        return []

    effective_limit = max(1, min(limit, 20))

    # Use table columns for SQLAlchemy operations (keeps type checker happy)
    c = cast(Any, OrganisationDocument).__table__.c

    # Build query
    stmt = select(OrganisationDocument).where(
        c.is_searchable.is_(True),
        c.is_archived.is_(False),
    )

    # Filter by organisation-wide or user's personal docs
    if user_id:
        stmt = stmt.where(
            or_(
                c.is_org_wide.is_(True),
                c.uploader_user_id == user_id,
            )
        )
    else:
        stmt = stmt.where(c.is_org_wide.is_(True))

    # Optional deal filter
    if deal_id:
        stmt = stmt.where(c.deal_id == deal_id)

    # Basic text search (ILIKE on content)
    like_pattern = f"%{trimmed_query}%"
    stmt = stmt.where(c.content.ilike(like_pattern))

    # Order by created_at desc (most recent first)
    stmt = stmt.order_by(c.created_at.desc())
    stmt = stmt.limit(effective_limit)

    # Execute query - acquire session on-demand if using context
    try:
        if use_context_session:
            async with context.acquire_session() as ctx_session:  # type: ignore[union-attr]
                result = await ctx_session.execute(stmt)
                documents = result.scalars().all()
        else:
            result = await session.execute(stmt)  # type: ignore[union-attr]
            documents = result.scalars().all()
    except Exception as exc:
        logger.warning("InternalKnowledgeBase query failed: %s", exc, exc_info=True)
        return []

    results: list[dict[str, str]] = []
    for doc in documents:
        doc_id = str(doc.document_id)
        org_id = str(organisation_id)

        # Extract page number from metadata if available
        page_num = None
        if doc.meta_data and isinstance(doc.meta_data, dict):
            page_num = doc.meta_data.get("page")

        # Build internal URL
        url = f"kb://org/{org_id}/doc/{doc_id}"
        if page_num is not None:
            url += f"#page={page_num}"

        # Label: use document name or fallback
        label = doc.name or f"Document {doc_id[:8]}"

        # Snippet: first 200 chars of content
        snippet = ""
        if doc.content:
            snippet = doc.content[:200] + ("..." if len(doc.content) > 200 else "")

        results.append({"label": label, "url": url, "snippet": snippet})

    if not results:
        logger.info("InternalKnowledgeBase returned no results for query: '%s'", trimmed_query)

    return results


def create_search_internal_knowledge(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable:
    """Factory that returns a tool function bound to context and capabilities.

    Args:
        context: AgentContext with session and org metadata
        capabilities_getter: Callable that returns current AgentCapabilities

    Returns:
        Async function suitable for use as an agent tool
    """

    async def search_internal_knowledge(query: str, limit: int = 5) -> list[dict[str, str]]:
        """Search internal knowledge base for company documents and information.

        Use this tool to find information from the organization's internal documents,
        knowledge base articles, product documentation, and other internal resources.
        Returns relevant document snippets with citations.

        Args:
            query: The search query to find relevant internal documents.
            limit: Maximum number of results to return (default: 5).

        Returns:
            List of dictionaries containing document information with keys:
            - label: Document name or title
            - url: Internal reference link (kb://org/{org_id}/doc/{doc_id})
            - snippet: Relevant text excerpt from the document
        """
        logger.info(f"[TOOL CALLED] search_internal_knowledge - query='{query}', limit={limit}")

        if not context.organisation_id:
            logger.warning("InternalKnowledgeBase unavailable: missing organisation_id")
            return []

        # Use lock to prevent concurrent session access
        # Agno may call multiple tools in parallel, but AsyncSession doesn't support that
        async with db_session_lock():
            result = await execute_with_capabilities(
                capabilities_getter(),
                tool_name="InternalKnowledgeBase",
                tool_func=internal_knowledge_base,
                parameters={
                    "context": context,  # Use context mode - acquires session on-demand
                    "query": query,
                    "limit": limit,
                },
                description=f"Searching internal documents for: {query}",
                fallback_value=[],
            )

        logger.info(
            f"[TOOL RESULT] search_internal_knowledge returned {len(result) if result else 0} results"
        )
        return list(result) if result else []

    return search_internal_knowledge


__all__ = ["internal_knowledge_base", "create_search_internal_knowledge"]
