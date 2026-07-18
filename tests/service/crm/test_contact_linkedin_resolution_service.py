"""Regression tests for resolving CRM contacts from LinkedIn identity input."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.service.crm.contacts.contracts import ContactWriteResult
from src.service.crm.contacts.research_service import ContactProviderResearchResult
from src.service.crm.contacts.resolution_service import (
    ContactIdentityResolutionService,
    ContactIdentityResolveCommand,
)


class _FakeResult:
    def __init__(self, *, scalars=None):
        self._scalars = scalars or []

    def scalars(self):
        """Match SQLAlchemy's result API for service query tests."""
        return self

    def first(self):
        """Return the first fake scalar result like SQLAlchemy does."""
        return self._scalars[0] if self._scalars else None


class _ResolutionSession:
    def __init__(self, *, search_results: list[CRMContact | None], contact: CRMContact):
        self._search_results = list(search_results)
        self.contact = contact
        self.commits = 0
        self.flushes = 0
        self.refreshes = 0

    async def execute(self, _stmt):  # noqa: ANN001
        """Return queued contact lookup results in resolver query order."""
        # Provider persistence reloads the resolved contact after identity
        # lookup; the fallback models that independently documented write path.
        result = self._search_results.pop(0) if self._search_results else self.contact
        return _FakeResult(scalars=[result] if result else [])

    async def get(self, _model, _item_id):  # noqa: ANN001
        """Return the contact that provider persistence should update."""
        return self.contact

    async def flush(self):
        """Record non-committing persistence flushes."""
        self.flushes += 1

    async def refresh(self, contact):
        """Record refreshes that restore server-managed columns after flush."""
        assert contact is self.contact
        self.refreshes += 1

    async def commit(self):
        """Record commits so resolver tests can prove it stays transaction-local."""
        self.commits += 1


@pytest.mark.asyncio
async def test_linkedin_only_apollo_match_creates_contact_and_persists_provider_data() -> None:
    """LinkedIn-only input can create a contact only after Apollo returns email identity."""
    org_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="ada@acme.com",
        linkedin_url="https://linkedin.com/in/ada",
    )
    session = _ResolutionSession(search_results=[None], contact=contact)
    writer = AsyncMock()
    writer.find_or_create_contact_by_email = AsyncMock(
        return_value=ContactWriteResult(contact=contact, created=True, matched_by="created")
    )

    async def provider_lookup(_payload):  # noqa: ANN001
        """Return a usable Apollo person for the LinkedIn-only resolver path."""
        return ContactProviderResearchResult(
            status="found",
            enrichment_source="apollo",
            email="ada@acme.com",
            name="Ada Buyer",
            first_name="Ada",
            last_name="Buyer",
            job_title="VP Engineering",
            linkedin_url="https://linkedin.com/in/ada",
            profile_enrichment={
                "provider": "apollo",
                "apollo_id": "person_123",
                "match_source": "linkedin_url",
            },
        )

    result = await ContactIdentityResolutionService(
        session,
        org_id,
        contact_writer=writer,
        provider_lookup=provider_lookup,
    ).resolve(
        ContactIdentityResolveCommand(
            linkedin_url="https://linkedin.com/in/ada",
            name="Ada",
        )
    )

    assert result.status == "resolved"
    assert result.contact is contact
    assert result.created is True
    assert result.matched_by == "apollo"
    writer.find_or_create_contact_by_email.assert_awaited_once()
    assert contact.email == "ada@acme.com"
    assert contact.full_name == "Ada Buyer"
    assert contact.job_title == "VP Engineering"
    assert contact.profile_enrichment["apollo_id"] == "person_123"
    assert contact.profile_enrichment["match_source"] == "linkedin_url"
    assert session.flushes == 1
    assert session.refreshes == 1
    assert session.commits == 0


@pytest.mark.asyncio
async def test_linkedin_resolution_reports_account_created_by_contact_writer() -> None:
    """LinkedIn-created contacts must preserve account creation for post-commit dispatch."""
    org_id = uuid4()
    company_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="ada@acme.com",
        linkedin_url="https://linkedin.com/in/ada",
    )
    session = _ResolutionSession(search_results=[None], contact=contact)
    writer = AsyncMock()
    writer.find_or_create_contact_by_email = AsyncMock(
        return_value=ContactWriteResult(
            contact=contact,
            created=True,
            matched_by="created",
            created_account_id=company_id,
        )
    )

    async def provider_lookup(_payload):  # noqa: ANN001
        return ContactProviderResearchResult(
            status="found",
            enrichment_source="apollo",
            email="ada@acme.com",
            name="Ada Buyer",
            linkedin_url="https://linkedin.com/in/ada",
            profile_enrichment={"provider": "apollo"},
        )

    result = await ContactIdentityResolutionService(
        session,
        org_id,
        contact_writer=writer,
        provider_lookup=provider_lookup,
    ).resolve(ContactIdentityResolveCommand(linkedin_url="https://linkedin.com/in/ada"))

    assert result.status == "resolved"
    assert result.created_account_id == company_id


@pytest.mark.asyncio
async def test_linkedin_only_apollo_not_found_returns_no_contact() -> None:
    """Unresolved LinkedIn-only input returns a typed miss without creating contacts."""
    org_id = uuid4()
    session = _ResolutionSession(search_results=[None], contact=None)  # type: ignore[arg-type]
    writer = AsyncMock()

    async def provider_lookup(_payload):  # noqa: ANN001
        """Return Apollo's no-match outcome for the resolver miss path."""
        return ContactProviderResearchResult(status="not_found", error_detail="No matching person")

    result = await ContactIdentityResolutionService(
        session,
        org_id,
        contact_writer=writer,
        provider_lookup=provider_lookup,
    ).resolve(ContactIdentityResolveCommand(linkedin_url="https://linkedin.com/in/missing"))

    assert result.status == "not_found"
    assert result.contact is None
    writer.find_or_create_contact_by_email.assert_not_awaited()
    assert session.commits == 0


@pytest.mark.asyncio
async def test_linkedin_only_provider_match_without_email_creates_contact() -> None:
    """Provider-found LinkedIn identity can create a contact without requiring email."""
    org_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email=None,
        linkedin_url="https://linkedin.com/in/no-email",
    )
    session = _ResolutionSession(search_results=[None], contact=contact)
    writer = AsyncMock()
    writer._create_contact_record = AsyncMock(
        return_value=ContactWriteResult(contact=contact, created=True, matched_by="created")
    )

    async def provider_lookup(_payload):  # noqa: ANN001
        return ContactProviderResearchResult(
            status="found",
            enrichment_source="apollo",
            email=None,
            name="No Email Buyer",
            linkedin_url="https://linkedin.com/in/no-email",
            profile_enrichment={"provider": "apollo"},
        )

    result = await ContactIdentityResolutionService(
        session,
        org_id,
        contact_writer=writer,
        provider_lookup=provider_lookup,
    ).resolve(ContactIdentityResolveCommand(linkedin_url="https://linkedin.com/in/no-email"))

    assert result.status == "resolved"
    assert result.contact is contact
    assert result.created is True
    assert result.matched_by == "apollo"
    writer.find_or_create_contact_by_email.assert_not_awaited()
    writer._create_contact_record.assert_awaited_once()
    created_command = writer._create_contact_record.await_args.args[0]
    assert created_command.email is None
    assert created_command.name == "No Email Buyer"
    assert created_command.linkedin_url == "https://linkedin.com/in/no-email"
    assert contact.full_name == "No Email Buyer"
    assert contact.profile_enrichment["provider"] == "apollo"
    assert session.flushes == 1
    assert session.refreshes == 1


@pytest.mark.asyncio
async def test_provider_canonical_linkedin_reuses_existing_contact_without_email() -> None:
    """Provider canonical LinkedIn should dedupe before no-email contact creation."""
    org_id = uuid4()
    existing = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email=None,
        full_name="Existing Buyer",
        linkedin_url="https://linkedin.com/in/canonical",
    )
    session = _ResolutionSession(search_results=[None, existing], contact=existing)
    writer = AsyncMock()

    async def provider_lookup(_payload):  # noqa: ANN001
        return ContactProviderResearchResult(
            status="found",
            enrichment_source="apollo",
            email=None,
            name="Existing Buyer",
            linkedin_url="https://linkedin.com/in/canonical",
            profile_enrichment={"provider": "apollo"},
        )

    result = await ContactIdentityResolutionService(
        session,
        org_id,
        contact_writer=writer,
        provider_lookup=provider_lookup,
    ).resolve(ContactIdentityResolveCommand(linkedin_url="https://linkedin.com/in/typed"))

    assert result.status == "resolved"
    assert result.contact is existing
    assert result.created is False
    assert result.matched_by == "linkedin"
    writer._create_contact_record.assert_not_awaited()
    assert existing.profile_enrichment["provider"] == "apollo"
    assert session.flushes == 1


@pytest.mark.asyncio
async def test_provider_email_match_does_not_overwrite_existing_contact_profile() -> None:
    """Apollo/provider fields should not replace stronger CRM profile values."""
    org_id = uuid4()
    existing = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="buyer@acme.com",
        full_name="Existing Buyer",
        job_title="CFO",
        linkedin_url="https://linkedin.com/in/existing",
    )
    session = _ResolutionSession(search_results=[None, None], contact=existing)
    writer = AsyncMock()
    writer.find_or_create_contact_by_email = AsyncMock(
        return_value=ContactWriteResult(contact=existing, created=False, matched_by="email")
    )

    async def provider_lookup(_payload):  # noqa: ANN001
        return ContactProviderResearchResult(
            status="found",
            enrichment_source="apollo",
            email="buyer@acme.com",
            name="Provider Buyer",
            job_title="VP Sales",
            linkedin_url="https://linkedin.com/in/provider",
            profile_enrichment={"provider": "apollo", "job_title": "VP Sales"},
        )

    result = await ContactIdentityResolutionService(
        session,
        org_id,
        contact_writer=writer,
        provider_lookup=provider_lookup,
    ).resolve(ContactIdentityResolveCommand(linkedin_url="https://linkedin.com/in/typed"))

    assert result.status == "resolved"
    writer.find_or_create_contact_by_email.assert_awaited_once()
    assert writer.find_or_create_contact_by_email.await_args.kwargs["profile_updates"] == {}
    assert existing.full_name == "Existing Buyer"
    assert existing.job_title == "CFO"
    assert existing.linkedin_url == "https://linkedin.com/in/existing"
    assert existing.profile_enrichment["job_title"] == "VP Sales"


@pytest.mark.asyncio
async def test_linkedin_only_provider_error_uses_public_error() -> None:
    """Provider transport/config errors should stay hidden behind public wording."""
    org_id = uuid4()
    session = _ResolutionSession(search_results=[None], contact=None)  # type: ignore[arg-type]
    writer = AsyncMock()

    async def provider_lookup(_payload):  # noqa: ANN001
        return ContactProviderResearchResult(
            status="error",
            error_detail="Apollo rate limit exceeded",
        )

    result = await ContactIdentityResolutionService(
        session,
        org_id,
        contact_writer=writer,
        provider_lookup=provider_lookup,
    ).resolve(ContactIdentityResolveCommand(linkedin_url="https://linkedin.com/in/rate-limited"))

    assert result.status == "error"
    assert result.error_detail == "LinkedIn research could not resolve this contact"
    writer.find_or_create_contact_by_email.assert_not_awaited()
