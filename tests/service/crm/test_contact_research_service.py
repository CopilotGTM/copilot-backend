from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.service.crm.contacts.research_service import (
    ContactProviderResearchInput,
    ContactProviderResearchResult,
    ContactResearchService,
    research_contact_with_apollo,
)


class _WriteSession:
    def __init__(self, contact: CRMContact):
        self.contact = contact
        self.commits = 0

    async def get(self, _model, _item_id):  # noqa: ANN001
        return self.contact

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_apollo_headline_is_not_used_as_structured_job_title(monkeypatch) -> None:
    """Apollo headlines can be taglines, so only title/job_title may fill CRM title."""
    organisation_id = uuid4()
    contact_id = uuid4()

    async def fake_apollo_enrich_person(**_kwargs):  # noqa: ANN003
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
