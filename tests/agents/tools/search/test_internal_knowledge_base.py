"""Tenant-boundary tests for internal knowledge search."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.agents.tools.search.internal_knowledge_base import internal_knowledge_base


class _EmptyScalarResult:
    def scalars(self) -> "_EmptyScalarResult":
        return self

    def all(self) -> list[object]:
        return []


@pytest.mark.asyncio
async def test_explicit_session_query_filters_by_organisation() -> None:
    """An explicit session must not weaken the organisation boundary."""
    organisation_id = uuid4()
    session = AsyncMock()
    session.execute.return_value = _EmptyScalarResult()

    await internal_knowledge_base(
        session=session,
        organisation_id=organisation_id,
        query="renewal policy",
    )

    statement = session.execute.await_args.args[0]
    compiled = statement.compile()
    assert "organisation_id" in str(compiled)
    assert organisation_id in compiled.params.values()
