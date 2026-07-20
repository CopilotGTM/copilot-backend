"""Workflow tests for contact research orchestration.

The worker workflow loads a contact, releases the session before provider work,
then reopens write sessions for provider fields, optional summaries, and the
visible completion activity. These tests keep those phase boundaries explicit
without requiring a real database or Celery worker.
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import import_module
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact


class _Result:
    """Tiny scalar result wrapper matching the SQLAlchemy result method used here."""

    def __init__(self, row=None):
        self.row = row

    def scalar_one_or_none(self):
        """Return the configured row for service lookups."""
        return self.row

    def scalars(self):
        """Return the scalar facade used by the persistence query."""
        return self

    def first(self):
        """Return the configured row as the first scalar result."""
        return self.row


class _SessionSource:
    """Async iterable that yields fake sessions in workflow phase order."""

    def __init__(self, *sessions):
        self.sessions = list(sessions)

    async def __aiter__(self):
        for session in self.sessions:
            yield session


class _SessionFactory:
    """Factory fake that returns one session source per acquire call."""

    def __init__(self, *sessions):
        self.sessions = list(sessions)

    def __call__(self, _organisation_id):  # noqa: ANN001
        return _SessionSource(self.sessions.pop(0))


class _LoadSession:
    """Read-phase session fake used before provider calls."""

    def __init__(self, contact):
        self.contact = contact

    async def get(self, _model, _item_id):  # noqa: ANN001
        """Return the fixture contact for snapshot loading."""
        return self.contact


class _WriteSession:
    """Write-phase session fake that records commits and SQL statements."""

    def __init__(self, contact):
        self.contact = contact
        self.commits = 0
        self.statements = []

    async def get(self, _model, _item_id):  # noqa: ANN001
        """Return the fixture contact for persistence and activity phases."""
        return self.contact

    async def execute(self, stmt):  # noqa: ANN001
        """Return the fixture contact for the service's locked tenant query."""
        return _Result(self.contact)

    async def commit(self):
        """Count transaction commits issued by the workflow."""
        self.commits += 1


class _Projection:
    """Activity projection fake that captures completion cards."""

    def __init__(self, _session, _organisation_id):  # noqa: ANN001
        self.completed_calls = []
        PROJECTIONS.append(self)

    async def upsert_contact_enrichment_completed(self, contact, **kwargs):  # noqa: ANN001
        """Record the completion activity payload for assertions."""
        self.completed_calls.append((contact, kwargs))


PROJECTIONS: list[_Projection] = []


@pytest.mark.asyncio
async def test_existing_provider_enrichment_generates_summary_without_apollo(
    monkeypatch,
) -> None:
    """Apollo data captured during create should feed the summary without a second lookup."""
    workflow_module = import_module(
        "src.workers.deal_worker.services.deal_insight_services.contact_research_service"
    )
    service_module = import_module("src.service.crm.contacts.research_service")

    organisation_id = uuid4()
    contact_id = uuid4()
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        contact_id=contact_id,
        email="buyer@acme.com",
        profile_enrichment={
            "provider": "apollo",
            "name": "Ada Buyer",
            "job_title": "VP Engineering",
        },
    )
    load_session = _LoadSession(contact)
    summary_session = _WriteSession(contact)
    completion_session = _WriteSession(contact)

    monkeypatch.setattr(
        workflow_module,
        "acquire_org_async_session",
        _SessionFactory(load_session, summary_session, completion_session),
    )
    PROJECTIONS.clear()
    monkeypatch.setattr(workflow_module, "ActivityFeedProjectionService", _Projection)

    async def provider_should_not_run(_payload):  # noqa: ANN001
        """Fail if cached provider evidence triggers an unnecessary Apollo lookup."""
        raise AssertionError("existing provider enrichment should skip Apollo")

    async def summary_generator(profile_enrichment):  # noqa: ANN001
        """Return deterministic markdown after checking cached evidence reaches the generator."""
        assert profile_enrichment["name"] == "Ada Buyer"
        return service_module.ContactResearchSummaryResult(
            status="generated",
            summary_markdown="## Ada Buyer\nVP Engineering at Acme.",
        )

    workflow = workflow_module.ContactResearchWorkflowService(
        organisation_id,
        provider_lookup=provider_should_not_run,
        summary_generator=summary_generator,
    )

    result = await workflow.research_contact(contact_id, job_id=uuid4())

    assert result.provider_status == "cached"
    assert result.summary_status == "generated"
    assert contact.contact_research == "## Ada Buyer\nVP Engineering at Acme."
    assert contact.profile_enrichment["_metadata"]["prompt_version"] == "contact_career_v1"
    assert summary_session.commits == 1
    assert completion_session.commits == 1


@pytest.mark.asyncio
async def test_provider_success_persists_enrichment_when_summary_fails(monkeypatch) -> None:
    """Provider fields are durable even when the optional UI summary fails."""
    workflow_module = import_module(
        "src.workers.deal_worker.services.deal_insight_services.contact_research_service"
    )
    service_module = import_module("src.service.crm.contacts.research_service")

    organisation_id = uuid4()
    contact_id = uuid4()
    job_id = uuid4()
    now = datetime(2026, 6, 25, 10, 0, tzinfo=UTC)
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        contact_id=contact_id,
        email="buyer@acme.com",
        company_name="Acme",
        company_id=uuid4(),
        created_at=now,
        updated_at=now,
    )
    load_session = _LoadSession(contact)
    write_session = _WriteSession(contact)
    completion_session = _WriteSession(contact)

    monkeypatch.setattr(
        workflow_module,
        "acquire_org_async_session",
        _SessionFactory(load_session, write_session, completion_session),
    )
    PROJECTIONS.clear()
    monkeypatch.setattr(workflow_module, "ActivityFeedProjectionService", _Projection)

    async def fake_provider(payload):  # noqa: ANN001
        """Return durable Apollo fields while the summary phase fails."""
        return service_module.ContactProviderResearchResult(
            status="found",
            profile_enrichment={
                "provider": "apollo",
                "name": "Jane Doe",
                "job_title": "VP Sales",
                "linkedin_url": "https://linkedin.com/in/jane",
            },
            name="Jane Doe",
            first_name="Jane",
            last_name="Doe",
            job_title="VP Sales",
            linkedin_url="https://linkedin.com/in/jane",
        )

    async def failing_summary(_profile_enrichment):  # noqa: ANN001
        """Simulate an LLM summary failure after provider persistence succeeds."""
        return service_module.ContactResearchSummaryResult(
            status="error",
            error_detail="summary model unavailable",
        )

    workflow = workflow_module.ContactResearchWorkflowService(
        organisation_id,
        provider_lookup=fake_provider,
        summary_generator=failing_summary,
    )

    result = await workflow.research_contact(contact_id, job_id=job_id)

    assert result.provider_status == "found"
    assert result.summary_status == "error"
    assert contact.full_name == "Jane Doe"
    assert contact.first_name == "Jane"
    assert contact.last_name == "Doe"
    assert contact.job_title == "VP Sales"
    assert contact.linkedin_url == "https://linkedin.com/in/jane"
    assert contact.profile_enrichment["provider"] == "apollo"
    assert contact.profile_enrichment_source == "apollo"
    assert contact.profile_enriched_at is not None
    assert contact.last_researched_at is None
    assert contact.contact_research is None
    assert write_session.commits == 1
    assert completion_session.commits == 1
    assert len(PROJECTIONS) == 1
    assert PROJECTIONS[0].completed_calls == [
        (
            contact,
            {
                "job_id": job_id,
                "provider_status": "found",
                "summary_status": "error",
            },
        )
    ]


@pytest.mark.asyncio
async def test_force_refresh_summarizes_persisted_cumulative_evidence(monkeypatch) -> None:
    """The summary phase must receive both earlier and newly fetched identity facts."""
    workflow_module = import_module(
        "src.workers.deal_worker.services.deal_insight_services.contact_research_service"
    )
    service_module = import_module("src.service.crm.contacts.research_service")

    organisation_id = uuid4()
    contact_id = uuid4()
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        contact_id=contact_id,
        email="buyer@acme.com",
        linkedin_url="https://linkedin.com/in/buyer",
        profile_enrichment={
            "provider": "apollo",
            "apollo_id": "person_123",
            "education": [{"degree": "BSc", "school_name": "Example University"}],
            "_metadata": {"payload_hash": "email-hash"},
        },
    )
    load_session = _LoadSession(contact)
    provider_session = _WriteSession(contact)
    summary_session = _WriteSession(contact)
    monkeypatch.setattr(
        workflow_module,
        "acquire_org_async_session",
        _SessionFactory(load_session, provider_session, summary_session),
    )

    async def fake_provider(_payload):  # noqa: ANN001
        """Return complementary career history from the newly complete identity set."""
        return service_module.ContactProviderResearchResult(
            status="found",
            profile_enrichment={
                "provider": "apollo",
                "apollo_id": "person_123",
                "employment_history": [{"title": "VP Engineering", "organization_name": "Acme"}],
                "_metadata": {
                    "payload_hash": "combined-hash",
                    "identity_kind": "email+linkedin",
                    "identity_fingerprint": "combined-fingerprint",
                },
            },
        )

    async def summary_generator(profile_enrichment):  # noqa: ANN001
        """Verify orchestration reloads the merged profile before model synthesis."""
        fact_pack = service_module.build_contact_research_fact_pack(profile_enrichment)
        assert fact_pack["education"] == [{"degree": "BSc", "school_name": "Example University"}]
        assert fact_pack["employment_history"] == [
            {"title": "VP Engineering", "organization_name": "Acme"}
        ]
        return service_module.ContactResearchSummaryResult(
            status="generated",
            summary_markdown="## Buyer\nCumulative career summary.",
        )

    workflow = workflow_module.ContactResearchWorkflowService(
        organisation_id,
        provider_lookup=fake_provider,
        summary_generator=summary_generator,
    )

    result = await workflow.research_contact(contact_id, force_refresh=True)

    assert result.summary_status == "generated"
    assert contact.contact_research == "## Buyer\nCumulative career summary."
    assert provider_session.commits == 1
    assert summary_session.commits == 1


@pytest.mark.asyncio
async def test_provider_not_found_marks_attempt_without_summary(monkeypatch) -> None:
    """A no-match provider response should stop retries without fabricating summary text."""
    workflow_module = import_module(
        "src.workers.deal_worker.services.deal_insight_services.contact_research_service"
    )
    service_module = import_module("src.service.crm.contacts.research_service")

    organisation_id = uuid4()
    contact_id = uuid4()
    job_id = uuid4()
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        contact_id=contact_id,
        email="buyer@acme.com",
    )
    load_session = _LoadSession(contact)
    write_session = _WriteSession(contact)
    completion_session = _WriteSession(contact)

    monkeypatch.setattr(
        workflow_module,
        "acquire_org_async_session",
        _SessionFactory(load_session, write_session, completion_session),
    )
    PROJECTIONS.clear()
    monkeypatch.setattr(workflow_module, "ActivityFeedProjectionService", _Projection)

    async def fake_provider(payload):  # noqa: ANN001
        """Return a provider no-match that should mark the attempt complete."""
        return service_module.ContactProviderResearchResult(status="not_found")

    summary_called = False

    async def summary_generator(_profile_enrichment):  # noqa: ANN001
        """Fail the test if summary generation runs after a provider no-match."""
        nonlocal summary_called
        summary_called = True
        return service_module.ContactResearchSummaryResult(status="generated", summary_markdown="")

    workflow = workflow_module.ContactResearchWorkflowService(
        organisation_id,
        provider_lookup=fake_provider,
        summary_generator=summary_generator,
    )

    result = await workflow.research_contact(contact_id, job_id=job_id)

    assert result.provider_status == "not_found"
    assert result.summary_status == "skipped"
    assert summary_called is False
    assert contact.last_researched_at is None
    assert contact.profile_enrichment["status"] == "not_found"
    assert contact.contact_research is None
    assert write_session.commits == 1
    assert completion_session.commits == 1
    assert PROJECTIONS[0].completed_calls == [
        (
            contact,
            {
                "job_id": job_id,
                "provider_status": "not_found",
                "summary_status": "skipped",
            },
        )
    ]


@pytest.mark.asyncio
async def test_force_refresh_retries_prior_miss_with_all_current_identities(monkeypatch) -> None:
    """A new identity must bypass the prior miss and reach Apollo with both identifiers."""
    workflow_module = import_module(
        "src.workers.deal_worker.services.deal_insight_services.contact_research_service"
    )
    service_module = import_module("src.service.crm.contacts.research_service")

    organisation_id = uuid4()
    contact_id = uuid4()
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        contact_id=contact_id,
        email="buyer@gmail.com",
        linkedin_url="https://linkedin.com/in/buyer",
        profile_enrichment={"provider": "apollo", "status": "not_found"},
    )
    load_session = _LoadSession(contact)
    write_session = _WriteSession(contact)
    completion_session = _WriteSession(contact)
    monkeypatch.setattr(
        workflow_module,
        "acquire_org_async_session",
        _SessionFactory(load_session, write_session, completion_session),
    )
    PROJECTIONS.clear()
    monkeypatch.setattr(workflow_module, "ActivityFeedProjectionService", _Projection)
    payloads = []

    async def fake_provider(payload):  # noqa: ANN001
        """Capture the provider identity set and return a terminal miss."""
        payloads.append(payload)
        return service_module.ContactProviderResearchResult(status="not_found")

    workflow = workflow_module.ContactResearchWorkflowService(
        organisation_id,
        provider_lookup=fake_provider,
    )

    result = await workflow.research_contact(contact_id, force_refresh=True)

    assert result.provider_status == "not_found"
    assert len(payloads) == 1
    assert payloads[0].email == "buyer@gmail.com"
    assert payloads[0].linkedin_url == "https://linkedin.com/in/buyer"


@pytest.mark.asyncio
async def test_provider_error_records_no_completed_activity(monkeypatch) -> None:
    """Retryable provider failures should not look like completed enrichment."""
    workflow_module = import_module(
        "src.workers.deal_worker.services.deal_insight_services.contact_research_service"
    )
    service_module = import_module("src.service.crm.contacts.research_service")

    organisation_id = uuid4()
    contact_id = uuid4()
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        contact_id=contact_id,
        email="buyer@acme.com",
    )
    load_session = _LoadSession(contact)

    monkeypatch.setattr(
        workflow_module,
        "acquire_org_async_session",
        lambda _organisation_id: _SessionSource(load_session),
    )
    PROJECTIONS.clear()
    monkeypatch.setattr(workflow_module, "ActivityFeedProjectionService", _Projection)

    async def failing_provider(payload):  # noqa: ANN001
        """Return a retryable provider failure without writing completion activity."""
        return service_module.ContactProviderResearchResult(
            status="error",
            error_detail="apollo unavailable",
        )

    workflow = workflow_module.ContactResearchWorkflowService(
        organisation_id,
        provider_lookup=failing_provider,
    )

    with pytest.raises(workflow_module.ContactResearchWorkflowError):
        await workflow.research_contact(contact_id, job_id=uuid4())

    assert PROJECTIONS == []


@pytest.mark.asyncio
async def test_unsafe_free_email_domain_skips_provider(monkeypatch) -> None:
    """Free provider subdomains should not call Apollo for automatic contact research."""
    workflow_module = import_module(
        "src.workers.deal_worker.services.deal_insight_services.contact_research_service"
    )

    organisation_id = uuid4()
    contact_id = uuid4()
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        contact_id=contact_id,
        email="nick@enter.gmail.com",
    )
    load_session = _LoadSession(contact)

    monkeypatch.setattr(
        workflow_module,
        "acquire_org_async_session",
        lambda _organisation_id: _SessionSource(load_session),
    )

    async def provider_should_not_run(payload):  # noqa: ANN001
        """Guard that unsafe free-email domains skip provider lookup entirely."""
        raise AssertionError("provider should not be called for unsafe domains")

    workflow = workflow_module.ContactResearchWorkflowService(
        organisation_id,
        provider_lookup=provider_should_not_run,
    )

    result = await workflow.research_contact(contact_id)

    assert result.provider_status == "skipped"
    assert result.summary_status == "skipped"
