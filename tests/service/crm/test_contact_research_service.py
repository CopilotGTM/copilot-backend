"""Regression tests for contact research provider normalization and persistence.

These tests lock the contract that Apollo/provider data is persisted to the
canonical CRMContact first, then mirrored into active open-deal stakeholder
snapshots only where list endpoints need denormalized participant fields.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStatus
from src.service.crm.contacts.research_service import (
    ContactProviderResearchInput,
    ContactProviderResearchResult,
    ContactResearchService,
    ContactResearchSummaryResult,
    research_contact_with_apollo,
)


class _WriteSession:
    """Minimal async session fake that records writes issued by the service."""

    def __init__(self, contact: CRMContact):
        self.contact = contact
        self.commits = 0
        self.statements = []

    async def get(self, _model, _item_id):  # noqa: ANN001
        """Return the fixture contact for service load calls."""
        return self.contact

    async def execute(self, stmt):  # noqa: ANN001
        """Capture SQL statements so tests can assert propagation scope."""
        self.statements.append(stmt)

    async def commit(self):
        """Count commits without needing a real database transaction."""
        self.commits += 1


def _compiled_statement(stmt):  # noqa: ANN001
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


@pytest.mark.asyncio
async def test_apollo_headline_is_not_used_as_structured_job_title(monkeypatch) -> None:
    """Apollo headlines can be taglines, so only title/job_title may fill CRM title."""
    organisation_id = uuid4()
    contact_id = uuid4()

    async def fake_apollo_enrich_person(**_kwargs):  # noqa: ANN003
        """Return an Apollo profile whose headline must not become job_title."""
        return {
            "success": True,
            "apollo_id": "person_123",
            "name": "Sam Altman",
            "first_name": "Sam",
            "last_name": "Altman",
            "title": None,
            "job_title": None,
            "headline": "Changing World",
            "linkedin_url": "https://linkedin.com/in/sam-altman-b54557359",
            "organization_name": "OpenAI",
            "organization_domain": "openai.com",
        }

    monkeypatch.setattr(
        "src.service.crm.contacts.research_service.apollo_enrich_person",
        fake_apollo_enrich_person,
    )

    result = await research_contact_with_apollo(
        ContactProviderResearchInput(
            organisation_id=organisation_id,
            contact_id=contact_id,
            email="sam.altman@openai.com",
            name="Sam Altman",
            company_domain="openai.com",
            linkedin_url="https://linkedin.com/in/sam-altman-b54557359",
        )
    )

    assert result.status == "found"
    assert result.job_title is None
    assert result.profile_enrichment["headline"] == "Changing World"
    assert result.profile_enrichment["job_title"] is None


@pytest.mark.asyncio
async def test_apollo_current_employment_title_fills_structured_job_title(monkeypatch) -> None:
    """When Apollo title is absent, the current employment record is the safest role fallback."""
    organisation_id = uuid4()
    contact_id = uuid4()

    async def fake_apollo_enrich_person(**_kwargs):  # noqa: ANN003
        """Return an Apollo profile where current employment supplies the title."""
        return {
            "success": True,
            "name": "Sam Altman",
            "title": None,
            "job_title": None,
            "headline": "Changing World",
            "employment_history": [
                {
                    "title": "Chief Executive Officer",
                    "company": "OpenAI",
                    "start_date": "2019-03-01",
                    "end_date": None,
                    "current": True,
                },
                {
                    "title": "President",
                    "company": "Y Combinator",
                    "start_date": "2014-02-01",
                    "end_date": "2019-03-01",
                    "current": False,
                },
            ],
        }

    monkeypatch.setattr(
        "src.service.crm.contacts.research_service.apollo_enrich_person",
        fake_apollo_enrich_person,
    )

    result = await research_contact_with_apollo(
        ContactProviderResearchInput(
            organisation_id=organisation_id,
            contact_id=contact_id,
            email="sam.altman@openai.com",
            name="Sam Altman",
            company_domain="openai.com",
        )
    )

    assert result.status == "found"
    assert result.job_title == "Chief Executive Officer"
    assert result.profile_enrichment["job_title"] == "Chief Executive Officer"
    assert result.profile_enrichment["headline"] == "Changing World"


@pytest.mark.asyncio
async def test_apollo_open_ended_employment_title_counts_as_current(monkeypatch) -> None:
    """Apollo history can imply current employment by omitting end_date."""
    organisation_id = uuid4()
    contact_id = uuid4()

    async def fake_apollo_enrich_person(**_kwargs):  # noqa: ANN003
        """Return an Apollo history entry that implies the role is current."""
        return {
            "success": True,
            "name": "Sam Altman",
            "title": None,
            "headline": "Changing World",
            "employment_history": [
                {
                    "title": "Chief Executive Officer",
                    "company": "OpenAI",
                    "start_date": "2019-03-01",
                    "end_date": None,
                    "current": False,
                }
            ],
        }

    monkeypatch.setattr(
        "src.service.crm.contacts.research_service.apollo_enrich_person",
        fake_apollo_enrich_person,
    )

    result = await research_contact_with_apollo(
        ContactProviderResearchInput(
            organisation_id=organisation_id,
            contact_id=contact_id,
            email="sam.altman@openai.com",
            name="Sam Altman",
            company_domain="openai.com",
        )
    )

    assert result.status == "found"
    assert result.job_title == "Chief Executive Officer"


@pytest.mark.asyncio
async def test_provider_result_does_not_overwrite_existing_job_title() -> None:
    """Existing CRM titles win over researched titles because they may be user-entered."""
    organisation_id = uuid4()
    contact_id = uuid4()
    now = datetime(2026, 6, 25, 10, 0, tzinfo=UTC)
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        contact_id=contact_id,
        email="sam.altman@openai.com",
        full_name="Sam Altman",
        job_title="CEO",
        created_at=now,
        updated_at=now,
    )
    session = _WriteSession(contact)

    persisted = await ContactResearchService(session, organisation_id).persist_provider_result(
        contact_id,
        ContactProviderResearchResult(
            status="found",
            profile_enrichment={"provider": "apollo", "job_title": "Changing World"},
            enrichment_source="apollo",
            job_title="Changing World",
            linkedin_url="https://linkedin.com/in/sam-altman-b54557359",
        ),
    )

    assert persisted is True
    assert contact.job_title == "CEO"
    assert contact.profile_enrichment["job_title"] == "Changing World"
    assert contact.linkedin_url == "https://linkedin.com/in/sam-altman-b54557359"
    assert session.commits == 1


@pytest.mark.asyncio
async def test_provider_result_updates_open_deal_stakeholder_snapshots() -> None:
    """Contact enrichment should refresh active open-deal stakeholder snapshots."""
    organisation_id = uuid4()
    contact_id = uuid4()
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        contact_id=contact_id,
        email=" Buyer@Example.COM ",
    )
    session = _WriteSession(contact)

    persisted = await ContactResearchService(session, organisation_id).persist_provider_result(
        contact_id,
        ContactProviderResearchResult(
            status="found",
            profile_enrichment={"provider": "apollo"},
            enrichment_source="apollo",
            name="Ada Buyer",
            job_title="VP Engineering",
            linkedin_url="https://linkedin.com/in/ada-buyer",
        ),
    )

    assert persisted is True
    assert len(session.statements) == 1
    sql = _compiled_statement(session.statements[0])
    # The propagation contract is SQL-scoped: one bulk update should refresh
    # only approved contact stakeholders on open deals for this researched person.
    assert "UPDATE crm_deal_participants" in sql
    assert "related_name='Ada Buyer'" in sql
    assert "job_title='VP Engineering'" in sql
    assert "linkedin_url='https://linkedin.com/in/ada-buyer'" in sql
    assert f"related_local_id = '{contact_id.hex}'" in sql
    assert "lower(trim(crm_deal_participants.related_email)) = 'buyer@example.com'" in sql
    assert "crm_deals.deal_status = 'open'" in sql
    assert "crm_deal_participants.approval_status = 'APPROVED'" in sql
    assert "crm_deal_participants.is_deleted IS false" in sql
    assert session.commits == 1


@pytest.mark.asyncio
async def test_summary_updates_open_deal_stakeholder_research_snapshots() -> None:
    """Generated contact research should become the stakeholder list snapshot."""
    organisation_id = uuid4()
    contact_id = uuid4()
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        contact_id=contact_id,
        email="buyer@example.com",
    )
    session = _WriteSession(contact)

    persisted = await ContactResearchService(session, organisation_id).persist_summary(
        contact_id,
        ContactResearchSummaryResult(
            status="generated",
            summary_markdown="## Ada Buyer\nResearch profile",
        ),
    )

    assert persisted is True
    assert contact.contact_research == "## Ada Buyer\nResearch profile"
    assert len(session.statements) == 1
    sql = _compiled_statement(session.statements[0])
    # Contact research markdown powers stakeholder list cards, which do not load
    # the canonical CRMContact row. Keep the denormalized copy in sync here.
    assert "UPDATE crm_deal_participants" in sql
    assert "stakeholder_research='## Ada Buyer" in sql
    assert "crm_deals.deal_status = 'open'" in sql
    assert f"related_local_id = '{contact_id.hex}'" in sql
    assert CRMDealStatus.OPEN.value in sql
    assert session.commits == 1
