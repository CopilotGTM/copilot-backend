from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStatus
from src.service.crm.internal.deal_write_service import DealWriteService


class _FakeResult:
    def __init__(self, rows: list[object]):
        self._rows = rows

    def scalars(self) -> _FakeResult:
        return self

    def first(self) -> object | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[object]:
        return self._rows


class _FakeOrgSession:
    def __init__(self, rows: list[object]):
        self._rows = rows
        self.added: list[object] = []

    async def execute(self, _stmt):
        return _FakeResult(self._rows)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_upsert_company_overwrites_existing_industry_and_country_when_provided() -> None:
    existing_company = SimpleNamespace(
        name="Acme",
        domain="acme.com",
        website=None,
        linkedin_url=None,
        industry="Powerline",
        country="India",
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    session = _FakeOrgSession([existing_company])
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )

    now = datetime.now(UTC)
    company, fallback = await service._upsert_company_from_account(
        account_payload={
            "name": "Acme",
            "domain": "acme.com",
            "industry": "Telecom",
            "country": "US",
        },
        integration_id=uuid4(),
        now=now,
    )

    assert fallback is None
    assert company is existing_company
    assert existing_company.industry == "Telecom"
    assert existing_company.country == "US"
    assert session.added == [existing_company]


@pytest.mark.asyncio
async def test_upsert_company_preserves_industry_and_country_when_not_provided() -> None:
    existing_company = SimpleNamespace(
        name="Acme",
        domain="acme.com",
        website=None,
        linkedin_url=None,
        industry="Powerline",
        country="India",
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    session = _FakeOrgSession([existing_company])
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )

    company, fallback = await service._upsert_company_from_account(
        account_payload={"name": "Acme", "domain": "acme.com"},
        integration_id=uuid4(),
        now=datetime.now(UTC),
    )

    assert fallback is None
    assert company is existing_company
    assert existing_company.industry == "Powerline"
    assert existing_company.country == "India"


@pytest.mark.asyncio
async def test_upsert_company_clears_company_fields_when_explicitly_provided_as_empty() -> None:
    existing_company = SimpleNamespace(
        name="Acme",
        domain="acme.com",
        website="https://acme.com",
        linkedin_url="https://linkedin.com/company/acme",
        industry="Powerline",
        country="India",
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    session = _FakeOrgSession([existing_company])
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )

    company, fallback = await service._upsert_company_from_account(
        account_payload={
            "name": "Acme",
            "domain": "acme.com",
            "website": None,
            "linkedin_url": None,
            "industry": None,
            "country": None,
        },
        integration_id=uuid4(),
        now=datetime.now(UTC),
    )

    assert fallback is None
    assert company is existing_company
    assert existing_company.website is None
    assert existing_company.linkedin_url is None
    assert existing_company.industry is None
    assert existing_company.country is None


@pytest.mark.asyncio
async def test_update_deal_rewrites_nullable_fields_when_none_is_provided() -> None:
    existing_deal = SimpleNamespace(
        deal_id=uuid4(),
        name="Existing Deal",
        amount=5000,
        currency="USD",
        deal_status=CRMDealStatus.OPEN,
        close_date=datetime.now(UTC),
        expected_close_date=datetime.now(UTC),
        closed_won_at=datetime.now(UTC),
        closed_lost_at=None,
        closed_reason="Old reason",
        crm_url="https://crm.example/deals/1",
        source_created_at=datetime.now(UTC),
        last_activity_at=datetime.now(UTC),
        description="Old description",
        deal_type="Old Type",
        priority="High",
        lead_source="Inbound",
        probability=70.0,
        forecast_category="Commit",
        properties={"is_internal_crm": True},
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    session = _FakeOrgSession([])
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service._load_internal_deal = AsyncMock(return_value=existing_deal)

    updated = await service.update_deal(
        existing_deal.deal_id,
        {
            "priority": None,
            "description": None,
            "deal_status": None,
            "closed_reason": None,
        },
    )

    assert updated is existing_deal
    assert existing_deal.priority is None
    assert existing_deal.description is None
    assert existing_deal.closed_reason is None
    assert existing_deal.deal_status == CRMDealStatus.OPEN


@pytest.mark.asyncio
async def test_update_account_skips_participant_replace_when_company_link_already_exists() -> None:
    session = _FakeOrgSession([])
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )

    deal_id = uuid4()
    company_id = uuid4()
    existing_deal = SimpleNamespace(
        deal_id=deal_id,
        company_id=company_id,
        company_external_id="company-ext-1",
        company_name="Acme",
        company_domain="acme.com",
        integration_id=uuid4(),
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    resolved_company = SimpleNamespace(
        company_id=company_id,
        external_id="company-ext-1",
        name="Acme",
        domain="acme.com",
    )

    service._load_internal_deal = AsyncMock(return_value=existing_deal)
    service._upsert_company_from_account = AsyncMock(return_value=(resolved_company, None))
    service._has_active_company_participant = AsyncMock(return_value=True)
    service._replace_company_participant = AsyncMock()

    result = await service.update_account(
        deal_id,
        {
            "name": "Acme",
            "domain": "acme.com",
            "website": None,
            "linkedin_url": None,
            "industry": "Powerline",
            "country": "India",
        },
    )

    assert result is existing_deal
    service._replace_company_participant.assert_not_called()
    service._has_active_company_participant.assert_awaited_once_with(
        deal=existing_deal,
        company_id=company_id,
        company_external_id="company-ext-1",
    )


@pytest.mark.asyncio
async def test_update_primary_contact_clears_internal_link_when_only_fallback_values_exist() -> (
    None
):
    session = _FakeOrgSession([])
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )

    deal_id = uuid4()
    existing_deal = SimpleNamespace(
        deal_id=deal_id,
        company_name="Acme",
        integration_id=uuid4(),
        primary_contact_id=uuid4(),
        primary_contact_external_id="contact-ext-1",
        primary_contact_name="Old Contact",
        primary_contact_email="old@example.com",
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )

    service._load_internal_deal = AsyncMock(return_value=existing_deal)
    service._upsert_contact_from_primary = AsyncMock(
        return_value=(None, "New Contact", "new@example.com")
    )

    result = await service.update_primary_contact(
        deal_id,
        primary_contact_name="New Contact",
        primary_contact_email="new@example.com",
    )

    assert result is existing_deal
    assert existing_deal.primary_contact_id is None
    assert existing_deal.primary_contact_external_id is None
    assert existing_deal.primary_contact_name == "New Contact"
    assert existing_deal.primary_contact_email == "new@example.com"
