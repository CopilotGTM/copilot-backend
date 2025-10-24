from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models.organisation_tables.knowledge_models import OrganisationDocument

if TYPE_CHECKING:
    from src.agents.context import AgentContext

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
    if context:
        session = context.session
        organisation_id = context.organisation_id
        user_id = user_id or context.user_id
        deal_id = deal_id or context.deal_id

    # Validate required parameters
    if not session or not organisation_id:
        logger.warning("InternalKnowledgeBase: missing session or organisation_id")
        return []

    trimmed_query = (query or "").strip()
    if not trimmed_query:
        logger.debug("InternalKnowledgeBase skipped: empty query provided")
        return []

    effective_limit = max(1, min(limit, 20))

    # Build query
    stmt = select(OrganisationDocument).where(
        OrganisationDocument.is_searchable.is_(True),
        OrganisationDocument.is_archived.is_(False),
    )

    # Filter by organisation-wide or user's personal docs
    if user_id:
        from sqlalchemy import or_

        stmt = stmt.where(
            or_(
                OrganisationDocument.is_org_wide.is_(True),
                OrganisationDocument.uploader_user_id == user_id,
            )
        )
    else:
        stmt = stmt.where(OrganisationDocument.is_org_wide.is_(True))

    # Optional deal filter
    if deal_id:
        stmt = stmt.where(OrganisationDocument.deal_id == deal_id)

    # Basic text search (ILIKE on content)
    like_pattern = f"%{trimmed_query}%"
    stmt = stmt.where(OrganisationDocument.content.ilike(like_pattern))  # type: ignore

    # Order by created_at desc (most recent first)
    stmt = stmt.order_by(OrganisationDocument.created_at.desc())
    stmt = stmt.limit(effective_limit)

    try:
        result = await session.execute(stmt)
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
