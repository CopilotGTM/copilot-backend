from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.service.crm.deals.read_service import DealReadService


class _FakeScalarResult:
    def __init__(self, rows: list[object]):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, results: list[list[object]]):
        self._results = results
        self.statements: list[object] = []

    async def execute(self, statement: object):
        self.statements.append(statement)
        return _FakeScalarResult(self._results.pop(0))


@pytest.mark.asyncio
async def test_search_deals_searches_deal_name_and_company_name() -> None:
    organisation_id = uuid4()
    now = datetime.now(UTC)
    deal = CRMDeal(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        deal_id=uuid4(),
        name="Acme Renewal",
        company_name="Acme",
        created_at=now,
        updated_at=now,
    )
    session = _FakeSession([[deal]])

    rows, next_cursor = await DealReadService(  # type: ignore[arg-type]
        session,
        organisation_id,
    ).search_deals(q="acme", limit=20)

    assert rows == [deal]
    assert next_cursor is None
    where_sql = str(session.statements[0].whereclause)
    assert "crm_deals.name" in where_sql
    assert "crm_deals.company_name" in where_sql
