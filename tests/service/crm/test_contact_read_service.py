"""Regression tests for tenant-scoped CRM contact read contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.service.crm.contacts.read_service import ContactReadService


class _FakeScalarResult:
    def __init__(self, rows: list[object]):
        self._rows = rows

    def scalars(self):
        """Mirror SQLAlchemy scalar chaining for contact list reads."""
        return self

    def all(self):
        """Return all configured ORM rows."""
        return self._rows

    def first(self):
        """Return the first configured ORM row when present."""
        return self._rows[0] if self._rows else None

    def scalar(self):
        """Return the first configured scalar value when present."""
        return self._rows[0] if self._rows else None


class _FakeAgentSearchResult:
    def __init__(self, rows: list[object]):
        self._rows = rows

    def all(self):
        """Return agent-search rows with match metadata."""
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
        """Capture each statement and return its queued scalar rows."""
        self.statements.append(statement)
        return _FakeScalarResult(self._results.pop(0))


class _FakeAgentSearchSession(_FakeSession):
    async def execute(self, statement: object):
        """Capture agent search SQL and return match-metadata rows."""
        self.statements.append(statement)
        return _FakeAgentSearchResult(self._results.pop(0))


@pytest.mark.asyncio
async def test_contact_source_lookup_keeps_organisation_in_query_identity() -> None:
    """A provider integration ID alone must not define the tenant boundary."""
    organisation_id = uuid4()
    integration_id = uuid4()
    session = _FakeSession([["salesforce"]])
    service = ContactReadService(session, organisation_id)  # type: ignore[arg-type]

    assert await service.resolve_source_for_contact(integration_id) == "salesforce"

    compiled = session.statements[0].compile()
    assert organisation_id in compiled.params.values()
    assert integration_id in compiled.params.values()


@pytest.mark.asyncio
async def test_contact_research_status_exposes_active_job_for_polling() -> None:
    """Contact detail polling must see the latest active contact-owned research job."""
    org_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="buyer@acme.com",
    )
    job = DealJobs(
        organisation_id=org_id,
        user_id=uuid4(),
        job_type=DealJobType.CONTACT_RESEARCH,
        status=JobStatus.IN_PROGRESS,
        job_metadata={
            "contact_id": str(contact.contact_id),
            "normalized_email": "buyer@acme.com",
            "linkedin_url": None,
        },
    )
    service = ContactReadService(_FakeSession([[job]]), org_id)  # type: ignore[arg-type]

    status = await service._contact_research_status(contact, {})

    assert status == "in_progress"
    sql = str(service.org_session.statements[0])  # type: ignore[attr-defined]
    assert "deal_jobs.job_metadata" in sql
    assert "LIMIT" in sql


@pytest.mark.asyncio
async def test_contact_research_status_restarts_when_job_used_an_older_identity() -> None:
    """A LinkedIn URL added after an email miss should make the contact researchable again."""
    org_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="buyer@acme.com",
        linkedin_url="https://linkedin.com/in/buyer",
    )
    job = DealJobs(
        organisation_id=org_id,
        user_id=uuid4(),
        job_type=DealJobType.CONTACT_RESEARCH,
        status=JobStatus.COMPLETED,
        job_metadata={
            "contact_id": str(contact.contact_id),
            "normalized_email": "buyer@acme.com",
            "linkedin_url": None,
        },
        result_data={"provider_status": "not_found", "summary_status": "skipped"},
    )
    service = ContactReadService(_FakeSession([[job]]), org_id)  # type: ignore[arg-type]

    status = await service._contact_research_status(contact, {"status": "not_found"})

    assert status == "not_started"


@pytest.mark.asyncio
async def test_contact_research_status_recovers_a_stale_active_job() -> None:
    """An abandoned pending row should become dispatchable instead of polling forever."""
    org_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="buyer@acme.com",
    )
    stale_at = datetime.now(UTC) - timedelta(minutes=10)
    job = DealJobs(
        organisation_id=org_id,
        user_id=uuid4(),
        job_type=DealJobType.CONTACT_RESEARCH,
        status=JobStatus.PENDING,
        job_metadata={
            "contact_id": str(contact.contact_id),
            "normalized_email": "buyer@acme.com",
            "linkedin_url": None,
        },
        created_at=stale_at,
        updated_at=stale_at,
    )
    service = ContactReadService(_FakeSession([[job]]), org_id)  # type: ignore[arg-type]

    status = await service._contact_research_status(contact, {})

    assert status == "not_started"


@pytest.mark.asyncio
async def test_contact_research_status_stops_polling_after_provider_miss() -> None:
    """A stored Apollo miss is terminal and must not leave the overview loader spinning."""
    org_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="buyer@acme.com",
    )
    service = ContactReadService(_FakeSession([[]]), org_id)  # type: ignore[arg-type]

    status = await service._contact_research_status(
        contact,
        {"provider": "apollo", "status": "not_found"},
    )

    assert status == "not_found"


@pytest.mark.asyncio
async def test_search_contacts_returns_page_ready_cursor_rows_and_searches_extra_emails() -> None:
    """Browse search keeps full list fields while matching alternate emails."""
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
        phone="+1 415 555 0100",
        mobile_phone="+1 415 555 0101",
        linkedin_url="https://linkedin.com/in/samaltman",
        company_id=company_id,
        owner_id=uuid4(),
        owner_name="CRM Owner",
        owner_email="owner@openai.com",
        last_contacted_at=datetime(2026, 8, 1, tzinfo=UTC),
        last_activity_at=datetime(2026, 8, 2, tzinfo=UTC),
        profile_enriched_at=datetime(2026, 8, 3, tzinfo=UTC),
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
    session = _FakeSession(
        [[(first, 1), (second, 0)], [company], [(first.integration_id, "hubspot")]]
    )

    rows, next_cursor = await ContactReadService(  # type: ignore[arg-type]
        session, org_id
    ).search_contacts(q="sam.altman@openai.com", limit=1)

    assert rows == [
        {
            "contact_id": first.contact_id,
            "full_name": "Samuel Altman",
            "email": "sam@openai.com",
            "additional_emails": ["sam.altman@openai.com"],
            "job_title": "CEO",
            "phone": "+1 415 555 0100",
            "mobile_phone": "+1 415 555 0101",
            "linkedin_url": "https://linkedin.com/in/samaltman",
            "company": {
                "id": company_id,
                "name": "OpenAI",
                "domain": "openai.com",
            },
            "owner": {
                "id": first.owner_id,
                "name": "CRM Owner",
                "email": "owner@openai.com",
            },
            "deal_count": 1,
            "last_contacted_at": datetime(2026, 8, 1, tzinfo=UTC),
            "last_activity_at": datetime(2026, 8, 2, tzinfo=UTC),
            "source": "hubspot",
            "is_profile_enriched": True,
            "profile_enriched_at": datetime(2026, 8, 3, tzinfo=UTC),
            "created_at": first.created_at,
            "updated_at": first.updated_at,
            "match_type": "additional_email",
        }
    ]
    assert next_cursor is not None
    assert "additional_emails" in str(session.statements[0])
    search_sql = str(session.statements[0])
    assert "crm_deal_participants" in search_sql
    assert "crm_deal_participants.is_deleted IS false" in search_sql
    assert "crm_deal_participants.approval_status" in search_sql


@pytest.mark.asyncio
async def test_search_contacts_for_create_does_not_search_company_name() -> None:
    """Duplicate checks must not treat a shared company as a person identity."""
    org_id = uuid4()
    session = _FakeSession([[], []])

    rows, next_cursor = await ContactReadService(  # type: ignore[arg-type]
        session, org_id
    ).search_contacts_for_create(q="openai", limit=1)

    assert rows == []
    assert next_cursor is None
    assert "crm_contacts.company_name" not in str(session.statements[0].whereclause)


def test_create_contact_match_type_does_not_match_company_name() -> None:
    """Create-flow matching excludes company-only matches from person identity."""
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
    """Agent search ranks supported identity fields and explains the winning match."""
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


@pytest.mark.asyncio
async def test_agent_contact_search_treats_like_metacharacters_literally() -> None:
    """Agent identity text must not broaden contact lookup through SQL wildcards."""
    org_id = uuid4()
    session = _FakeAgentSearchSession([[]])

    rows = await ContactReadService(  # type: ignore[arg-type]
        session,
        org_id,
    ).search_contacts_for_agent(q="buyer_%", limit=5)

    assert rows == []
    sql = str(session.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert " ESCAPE " in sql


@pytest.mark.asyncio
async def test_agent_contact_search_rejects_oversized_identity() -> None:
    """Oversized identity input must be rejected before PostgreSQL work."""
    org_id = uuid4()
    session = _FakeAgentSearchSession([])

    rows = await ContactReadService(  # type: ignore[arg-type]
        session,
        org_id,
    ).search_contacts_for_agent(q="x" * 201, limit=5)

    assert rows == []
    assert session.statements == []
