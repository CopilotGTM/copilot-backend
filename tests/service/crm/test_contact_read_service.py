from __future__ import annotations

from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.service.crm.contacts.read_service import ContactReadService


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
        contact: CRMContact,
        *,
        score: float,
        match_field: str,
        match_value: str,
        match_type: str = "fuzzy",
    ) -> None:
        self._contact = contact
        self.score = score
        self.match_field = match_field
        self.match_value = match_value
        self.match_type = match_type

    def __getitem__(self, index: int) -> CRMContact:
        if index == 0:
            return self._contact
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
async def test_search_contacts_returns_lightweight_cursor_page_and_searches_extra_emails() -> None:
    org_id = uuid4()
    company_id = uuid4()
    first = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        full_name="Samuel Altman",
        email="sam@openai.com",
        additional_emails=["sam.altman@openai.com"],
        job_title="CEO",
        company_id=company_id,
    )
    second = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        full_name="Sam A",
        email="sam.a@example.com",
    )
    company = CRMCompany(
        organisation_id=org_id,
        integration_id=uuid4(),
        company_id=company_id,
        name="OpenAI",
        domain="openai.com",
    )
    session = _FakeSession([[first, second], [company]])

    rows, next_cursor = await ContactReadService(  # type: ignore[arg-type]
        session, org_id
    ).search_contacts(q="sam.altman@openai.com", limit=1)

    assert rows == [
        {
            "contact_id": first.contact_id,
            "full_name": "Samuel Altman",
            "email": "sam@openai.com",
            "job_title": "CEO",
            "company": {
                "id": company_id,
                "name": "OpenAI",
                "domain": "openai.com",
            },
            "match_type": "additional_email",
        }
    ]
    assert next_cursor is not None
    assert "additional_emails" in str(session.statements[0])


@pytest.mark.asyncio
async def test_search_contacts_for_create_does_not_search_company_name() -> None:
    org_id = uuid4()
    session = _FakeSession([[], []])

    rows, next_cursor = await ContactReadService(  # type: ignore[arg-type]
        session, org_id
    ).search_contacts_for_create(q="openai", limit=1)

    assert rows == []
    assert next_cursor is None
    assert "crm_contacts.company_name" not in str(session.statements[0].whereclause)


def test_create_contact_match_type_does_not_match_company_name() -> None:
    contact = CRMContact(
        organisation_id=uuid4(),
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="sam@example.com",
        full_name="Sam Altman",
        company_name="OpenAI",
    )

    assert ContactReadService._contact_match_type(contact, "openai", False) == "other"
    assert ContactReadService._contact_match_type(contact, "openai", True) == "company"


@pytest.mark.asyncio
async def test_search_contacts_for_agent_uses_identity_fields_and_match_metadata() -> None:
    org_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        full_name="Jane Buyer",
        email="jane@acme.com",
        additional_emails=["jane.buyer@example.com"],
        company_name="Acme",
    )
    session = _FakeAgentSearchSession(
        [
            [
                _FakeAgentSearchRow(
                    contact,
                    score=1.4,
                    match_field="additional_emails",
                    match_value="jane.buyer@example.com",
                )
            ]
        ]
    )

    rows = await ContactReadService(  # type: ignore[arg-type]
        session,
        org_id,
    ).search_contacts_for_agent(q="jane buyer", limit=5)

    assert rows[0]["contact"] == contact
    assert rows[0]["match"] == {
        "field": "additional_emails",
        "value": "jane.buyer@example.com",
        "type": "fuzzy",
        "score": 1.4,
    }
    statement_sql = str(session.statements[0]).lower()
    assert "crm_contacts.full_name" in statement_sql
    assert "crm_contacts.email" in statement_sql
    assert "crm_contacts.company_name" in statement_sql
    assert "additional_emails" in statement_sql
    assert "crm_contacts.is_deleted" in statement_sql
    assert "job_title ilike" not in statement_sql
    assert "job_title %" not in statement_sql
