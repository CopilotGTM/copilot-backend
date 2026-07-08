"""Tests for AccountsAccessService utilities."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealStatus
from src.service.crm.access.accounts_access_service import AccountsAccessService
from src.service.crm.accounts.contracts import AccountReadServiceError
from src.service.crm.accounts.read_service import (
    AccountReadService,
    _decode_account_search_cursor,
    _encode_account_search_cursor,
)


def test_normalize_domain_plain_uppercase() -> None:
    assert AccountsAccessService._normalize_domain("ACME.COM") == "acme.com"


def test_normalize_domain_https_www() -> None:
    assert AccountsAccessService._normalize_domain("https://www.acme.com") == "acme.com"


def test_normalize_domain_http_with_path() -> None:
    assert AccountsAccessService._normalize_domain("http://Acme.com/about") == "acme.com"


def test_normalize_domain_https_www_with_path() -> None:
    assert AccountsAccessService._normalize_domain("https://www.ACME.com/about") == "acme.com"


def test_normalize_domain_empty_string() -> None:
    assert AccountsAccessService._normalize_domain("") is None


def test_normalize_domain_none() -> None:
    assert AccountsAccessService._normalize_domain(None) is None


def test_normalize_domain_whitespace_only() -> None:
    assert AccountsAccessService._normalize_domain("   ") is None


def test_normalize_domain_strips_query_string() -> None:
    assert AccountsAccessService._normalize_domain("https://acme.com?ref=foo") == "acme.com"


def test_normalize_domain_strips_fragment() -> None:
    assert AccountsAccessService._normalize_domain("https://acme.com#section") == "acme.com"


def test_normalize_domain_no_protocol() -> None:
    assert AccountsAccessService._normalize_domain("www.Acme.COM") == "acme.com"


def _mock_scalars_first(row):
    mock_scalars = MagicMock()
    mock_scalars.first.return_value = row
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    return mock_result


class _FakeScalarResult:
    def __init__(self, rows: list[object]):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeAgentSearchResult:
    def __init__(self, rows: list[object]):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeAgentSearchRow:
    def __init__(
        self,
        company: CRMCompany,
        *,
        contact_count: int,
        deal_count: int,
        score: float,
        match_field: str,
        match_value: str,
        match_type: str = "fuzzy",
    ) -> None:
        self._company = company
        self.contact_count = contact_count
        self.deal_count = deal_count
        self.score = score
        self.match_field = match_field
        self.match_value = match_value
        self.match_type = match_type

    def __getitem__(self, index: int):
        if index == 0:
            return self._company
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


def test_account_search_cursor_round_trips_name_and_id() -> None:
    company_id = uuid4()
    cursor = _encode_account_search_cursor("Acme", company_id)

    assert _decode_account_search_cursor(cursor) == ("acme", company_id)


def test_account_search_cursor_rejects_invalid_payload() -> None:
    with pytest.raises(AccountReadServiceError) as exc_info:
        _decode_account_search_cursor("not-a-cursor")

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "Invalid cursor"


def test_account_owner_payload_uses_id_name_and_email() -> None:
    owner_id = uuid4()
    company = SimpleNamespace(
        owner_id=owner_id,
        owner_name="Jane Smith",
        owner_email="jane@example.com",
    )

    assert AccountReadService._account_owner_payload(company) == {
        "id": owner_id,
        "name": "Jane Smith",
        "email": "jane@example.com",
    }


@pytest.mark.asyncio
async def test_search_accounts_for_agent_uses_identity_fields_and_match_metadata() -> None:
    organisation_id = uuid4()
    company = CRMCompany(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        company_id=uuid4(),
        name="Acme Corp",
        domain="acme.com",
        website="https://www.acme.com",
        industry="Software",
    )
    session = _FakeAgentSearchSession(
        [
            [
                _FakeAgentSearchRow(
                    company,
                    contact_count=3,
                    deal_count=2,
                    score=1.8,
                    match_field="domain",
                    match_value="acme.com",
                    match_type="exact",
                )
            ]
        ]
    )

    rows = await AccountReadService(  # type: ignore[arg-type]
        session,
        organisation_id,
    ).search_accounts_for_agent(q="acme", limit=5)

    assert rows[0]["company"] == company
    assert rows[0]["contact_count"] == 3
    assert rows[0]["deal_count"] == 2
    assert rows[0]["match"] == {
        "field": "domain",
        "value": "acme.com",
        "type": "exact",
        "score": 1.8,
    }
    statement_sql = str(session.statements[0]).lower()
    assert "crm_companies.name" in statement_sql
    assert "crm_companies.domain" in statement_sql
    assert "crm_companies.website" in statement_sql
    assert "industry ilike" not in statement_sql
    assert "industry %" not in statement_sql


@pytest.mark.asyncio
async def test_get_account_detail_maps_row_like_count_results() -> None:
    organisation_id = uuid4()
    company_id = uuid4()
    contact_with_count_id = uuid4()
    contact_without_count_id = uuid4()

    mock_db = AsyncMock()
    service = AccountReadService(org_session=mock_db, organisation_id=organisation_id)

    company = MagicMock()
    company.model_dump.return_value = {"company_id": company_id, "name": "Acme"}
    company.account_summary = "- Acme is a software company."
    company.last_researched_at = datetime(2026, 6, 26, tzinfo=UTC)

    contact_with_count = MagicMock(spec=CRMContact)
    contact_with_count.contact_id = contact_with_count_id
    contact_with_count.full_name = "Alice"
    contact_with_count.email = "alice@acme.com"
    contact_with_count.job_title = "VP"

    contact_without_count = MagicMock(spec=CRMContact)
    contact_without_count.contact_id = contact_without_count_id
    contact_without_count.full_name = "Bob"
    contact_without_count.email = "bob@acme.com"
    contact_without_count.job_title = "Director"

    open_deal = MagicMock(spec=CRMDeal)
    open_deal.amount = 1250
    open_deal.deal_status = CRMDealStatus.OPEN.value
    open_deal.model_dump.return_value = {"deal_id": str(uuid4()), "amount": 1250}

    won_deal = MagicMock(spec=CRMDeal)
    won_deal.amount = 900
    won_deal.deal_status = CRMDealStatus.WON.value
    won_deal.model_dump.return_value = {"deal_id": str(uuid4()), "amount": 900}

    mock_db.execute = AsyncMock(return_value=_mock_scalars_first(company))
    service.list_account_contacts = AsyncMock(
        return_value=[
            {
                "contact_id": contact_with_count.contact_id,
                "full_name": contact_with_count.full_name,
                "email": contact_with_count.email,
                "job_title": contact_with_count.job_title,
                "deal_count": 3,
            },
            {
                "contact_id": contact_without_count.contact_id,
                "full_name": contact_without_count.full_name,
                "email": contact_without_count.email,
                "job_title": contact_without_count.job_title,
                "deal_count": 0,
            },
        ]
    )
    service.get_account_deals = AsyncMock(return_value=[open_deal, won_deal])

    detail = await service.get_account_detail(company_id)

    assert detail["contact_count"] == 2
    assert detail["deal_count"] == 2
    assert detail["pipeline_value"] == 1250.0
    assert detail["account_summary"] == {
        "status": "completed",
        "content": "- Acme is a software company.",
        "last_researched_at": datetime(2026, 6, 26, tzinfo=UTC),
    }
    assert detail["contacts"] == [
        {
            "contact_id": contact_with_count_id,
            "full_name": "Alice",
            "email": "alice@acme.com",
            "job_title": "VP",
        },
        {
            "contact_id": contact_without_count_id,
            "full_name": "Bob",
            "email": "bob@acme.com",
            "job_title": "Director",
        },
    ]
