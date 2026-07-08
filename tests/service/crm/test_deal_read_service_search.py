from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealStatus
from src.service.crm.deals.read_service import (
    DealReadService,
    _decode_deal_search_cursor,
    _normalize_agent_deal_search_query,
)


class _FakeScalarResult:
    def __init__(self, rows: list[object]):
        self._rows = rows

    def scalar(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeAgentSearchResult:
    def __init__(self, rows: list[object]):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeAgentSearchRow:
    def __init__(
        self,
        deal: CRMDeal,
        *,
        score: float,
        name_score: float,
        company_score: float,
        primary_contact_score: float,
        match_field: str,
        match_value: str,
        match_type: str = "fuzzy",
    ) -> None:
        self._deal = deal
        self.score = score
        self.name_score = name_score
        self.company_score = company_score
        self.primary_contact_score = primary_contact_score
        self.match_field = match_field
        self.match_value = match_value
        self.match_type = match_type

    def __getitem__(self, index: int) -> CRMDeal:
        if index == 0:
            return self._deal
        raise IndexError(index)


class _FakeSession:
    def __init__(self, results: list[list[object]]):
        self._results = results
        self.statements: list[object] = []

    async def execute(self, statement: object):
        self.statements.append(statement)
        return _FakeScalarResult(self._results.pop(0))


class _FakeAgentSearchSession(_FakeSession):
    async def execute(self, statement: object):
        self.statements.append(statement)
        return _FakeAgentSearchResult(self._results.pop(0))


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


@pytest.mark.asyncio
async def test_search_deals_orders_blank_names_by_company_name() -> None:
    """Cursor sort key should match the cursor fallback for blank deal names."""
    organisation_id = uuid4()
    now = datetime.now(UTC)
    first = CRMDeal(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        deal_id=uuid4(),
        name="",
        company_name="Acme",
        created_at=now,
        updated_at=now,
    )
    second = CRMDeal(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        deal_id=uuid4(),
        name="Beta Renewal",
        company_name="Beta",
        created_at=now,
        updated_at=now,
    )
    session = _FakeSession([[first, second]])

    rows, next_cursor = await DealReadService(  # type: ignore[arg-type]
        session,
        organisation_id,
    ).search_deals(limit=1)

    assert rows == [first]
    assert next_cursor is not None
    cursor_name, cursor_deal_id = _decode_deal_search_cursor(next_cursor)
    assert cursor_name == "acme"
    assert cursor_deal_id == first.deal_id
    statement_sql = str(session.statements[0])
    assert "nullif" in statement_sql.lower()


@pytest.mark.asyncio
async def test_search_deals_cursor_sort_trims_company_name_fallback() -> None:
    """The DB sort key must match the encoded cursor key for provider-padded names."""
    organisation_id = uuid4()
    now = datetime.now(UTC)
    deal = CRMDeal(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        deal_id=uuid4(),
        name="",
        company_name="  Acme  ",
        created_at=now,
        updated_at=now,
    )
    session = _FakeSession([[deal]])

    _rows, next_cursor = await DealReadService(  # type: ignore[arg-type]
        session,
        organisation_id,
    ).search_deals(limit=1)

    assert next_cursor is None
    statement_sql = str(session.statements[0]).lower()
    assert "trim(crm_deals.company_name)" in statement_sql


@pytest.mark.asyncio
async def test_list_deals_ignores_blank_search_query() -> None:
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
    session = _FakeSession([[1], [deal]])

    rows, total_count = await DealReadService(  # type: ignore[arg-type]
        session,
        organisation_id,
    ).list_deals(q="   ")

    assert rows == [deal]
    assert total_count == 1
    statement_sql = str(session.statements[1].whereclause)
    count_sql = str(session.statements[0].whereclause)
    assert "crm_deals.name" not in statement_sql
    assert "crm_deals.company_name" not in statement_sql
    assert "crm_deals.name" not in count_sql
    assert "crm_deals.company_name" not in count_sql


@pytest.mark.asyncio
async def test_list_deals_filters_by_expected_close_date_range() -> None:
    organisation_id = uuid4()
    now = datetime.now(UTC)
    deal = CRMDeal(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        deal_id=uuid4(),
        name="Forecasted Renewal",
        expected_close_date=datetime(2026, 7, 15, tzinfo=UTC),
        close_date=None,
        created_at=now,
        updated_at=now,
    )
    session = _FakeSession([[1], [deal]])

    rows, total_count = await DealReadService(  # type: ignore[arg-type]
        session,
        organisation_id,
    ).list_deals(
        min_expected_close_date=datetime(2026, 7, 1, tzinfo=UTC),
        max_expected_close_date=datetime(2026, 7, 31, tzinfo=UTC),
    )

    assert rows == [deal]
    assert total_count == 1
    statement_sql = str(session.statements[1].whereclause)
    count_sql = str(session.statements[0].whereclause)
    assert "crm_deals.expected_close_date" in statement_sql
    assert "crm_deals.close_date" not in statement_sql
    assert "crm_deals.expected_close_date" in count_sql
    assert "crm_deals.close_date" not in count_sql


@pytest.mark.asyncio
async def test_get_all_deals_searches_company_name_like_list_deals() -> None:
    organisation_id = uuid4()
    now = datetime.now(UTC)
    deal = CRMDeal(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        deal_id=uuid4(),
        name="Renewal",
        company_name="Acme",
        created_at=now,
        updated_at=now,
    )
    session = _FakeSession([[deal]])

    rows = await DealReadService(  # type: ignore[arg-type]
        session,
        organisation_id,
    ).get_all_deals(q=" acme ")

    assert rows == [deal]
    statement_sql = str(session.statements[0].whereclause)
    assert "crm_deals.name" in statement_sql
    assert "crm_deals.company_name" in statement_sql


@pytest.mark.asyncio
async def test_get_deals_stats_preserves_zero_amount_aggregates() -> None:
    organisation_id = uuid4()
    session = _FakeSession([[1], [], [], [(0, 0)]])

    stats = await DealReadService(  # type: ignore[arg-type]
        session,
        organisation_id,
    ).get_deals_stats()

    assert stats["total_amount"] == 0.0
    assert stats["average_amount"] == 0.0


@pytest.mark.asyncio
async def test_get_deal_stage_history_preserves_zero_amount_at_stage() -> None:
    organisation_id = uuid4()
    history_id = uuid4()
    stage_id = uuid4()
    entered_at = datetime.now(UTC)
    session = _FakeSession(
        [
            [
                SimpleNamespace(
                    history_id=history_id,
                    stage_id=stage_id,
                    from_stage_id=None,
                    entered_at=entered_at,
                    exited_at=entered_at,
                    amount_at_stage=0,
                    probability_at_stage=None,
                    source="manual",
                    stage_name="Discovery",
                    stage_label="Discovery",
                )
            ]
        ]
    )

    history = await DealReadService(  # type: ignore[arg-type]
        session,
        organisation_id,
    ).get_deal_stage_history(uuid4())

    assert history[0]["amount_at_stage"] == 0.0


@pytest.mark.asyncio
async def test_resolve_current_user_deal_owners_trims_email_identity() -> None:
    organisation_id = uuid4()
    session = _FakeSession([[]])

    await DealReadService(  # type: ignore[arg-type]
        session,
        organisation_id,
    ).resolve_current_user_deal_owners(
        user_id=uuid4(),
        user_email=" Test@Example.com ",
    )

    statement_sql = str(session.statements[0]).lower()
    assert "trim(crm_users.email)" in statement_sql
    assert "trim(crm_deals.owner_email)" in statement_sql


@pytest.mark.asyncio
async def test_list_deal_owners_trims_search_query() -> None:
    organisation_id = uuid4()
    session = _FakeSession([[]])

    await DealReadService(  # type: ignore[arg-type]
        session,
        organisation_id,
    ).list_deal_owners(q=" Jane ")

    compiled = session.statements[0].compile()
    assert "%jane%" in compiled.params.values()
    assert "% jane %" not in compiled.params.values()


@pytest.mark.asyncio
async def test_list_deal_owners_ignores_blank_search_query() -> None:
    organisation_id = uuid4()
    session = _FakeSession([[]])

    await DealReadService(  # type: ignore[arg-type]
        session,
        organisation_id,
    ).list_deal_owners(q="   ")

    where_sql = str(session.statements[0].whereclause)
    assert "owner_name" not in where_sql
    assert "owner_email" not in where_sql


def test_normalize_agent_deal_search_defaults_to_open_and_removes_filler() -> None:
    query, statuses = _normalize_agent_deal_search_query("please find the Acme renewal deal")

    assert query == "acme renewal"
    assert statuses == [CRMDealStatus.OPEN.value]


def test_normalize_agent_deal_search_treats_status_as_filter_not_search() -> None:
    query, statuses = _normalize_agent_deal_search_query("find won Acme deal")

    assert query == "acme"
    assert statuses == [CRMDealStatus.WON.value]


@pytest.mark.asyncio
async def test_search_deals_for_agent_uses_status_filter_and_primary_contact() -> None:
    organisation_id = uuid4()
    now = datetime.now(UTC)
    deal = CRMDeal(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        deal_id=uuid4(),
        name="Enterprise Expansion",
        company_name="Acme",
        primary_contact_name="Jane Buyer",
        deal_status=CRMDealStatus.OPEN,
        created_at=now,
        updated_at=now,
    )
    session = _FakeAgentSearchSession(
        [
            [
                _FakeAgentSearchRow(
                    deal,
                    score=0.5,
                    name_score=0.2,
                    company_score=0.1,
                    primary_contact_score=0.7,
                    match_field="primary_contact_name",
                    match_value="Jane Buyer",
                )
            ]
        ]
    )

    rows = await DealReadService(  # type: ignore[arg-type]
        session,
        organisation_id,
    ).search_deals_for_agent(q="find jane buyer", limit=5)

    assert rows[0]["deal"] == deal
    assert rows[0]["match"]["field"] == "primary_contact_name"
    assert rows[0]["match"]["value"] == "Jane Buyer"
    assert rows[0]["match"]["type"] == "fuzzy"
    assert rows[0]["match"]["score"] == 0.5
    statement_sql = str(session.statements[0]).lower()
    assert "crm_deals.deal_status" in statement_sql
    assert "crm_deals.primary_contact_name" in statement_sql
