from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.service.crm.deals.write_service import DealWriteService


class _EmptyScalarResult:
    def scalars(self) -> _EmptyScalarResult:
        return self

    def all(self) -> list[object]:
        return []


@pytest.mark.asyncio
async def test_update_account_same_link_records_no_changed_fields() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    company_id = uuid4()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        integration_id=uuid4(),
        company_id=company_id,
        company_name="Acme",
        company_domain="acme.com",
    )
    company = SimpleNamespace(company_id=company_id, name="Acme", domain="acme.com")
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._account_writer.upsert_company_from_account = AsyncMock(return_value=(company, None))
    service._has_active_company_participant = AsyncMock(return_value=True)
    service._get_current_company = AsyncMock(return_value=None)
    service._sync_deal_company_snapshot = lambda deal, company, now: None
    service._record_mutation_event = AsyncMock()

    await service.update_account(deal.deal_id, {"name": "Acme", "domain": "acme.com"})

    kwargs = service._record_mutation_event.await_args.kwargs
    assert kwargs["event_type"] == "crm.deal.account_changed"
    assert kwargs["changed_fields"] == {}


@pytest.mark.asyncio
async def test_update_account_records_company_profile_snapshot_changes() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    company_id = uuid4()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        integration_id=uuid4(),
        company_id=company_id,
        company_name="Acme",
        company_domain="acme.com",
        company_industry="Services",
        company_website="https://old.example",
        company_linkedin_url=None,
        company_country="India",
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    company = SimpleNamespace(
        company_id=company_id,
        name="Acme",
        domain="acme.com",
        industry="Software",
        website="https://acme.com",
        linkedin_url="https://linkedin.com/company/acme",
        country="United States",
    )
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._account_writer.upsert_company_from_account = AsyncMock(return_value=(company, None))
    service._has_active_company_participant = AsyncMock(return_value=True)
    service._get_current_company = AsyncMock(return_value=None)
    service._record_mutation_event = AsyncMock()

    await service.update_account(
        deal.deal_id,
        {
            "industry": "Software",
            "website": "https://acme.com",
            "linkedin_url": "https://linkedin.com/company/acme",
            "country": "United States",
        },
    )

    kwargs = service._record_mutation_event.await_args.kwargs
    assert kwargs["event_type"] == "crm.deal.account_changed"
    assert kwargs["changed_fields"] == {
        "company_industry": {"old": "Services", "new": "Software"},
        "company_website": {"old": "https://old.example", "new": "https://acme.com"},
        "company_linkedin_url": {
            "old": None,
            "new": "https://linkedin.com/company/acme",
        },
        "company_country": {"old": "India", "new": "United States"},
    }


@pytest.mark.asyncio
async def test_update_primary_contact_fallback_records_link_and_snapshot_changes() -> None:
    session = SimpleNamespace(
        add=lambda obj: None,
        execute=AsyncMock(return_value=_EmptyScalarResult()),
    )
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    old_contact_id = uuid4()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        company_name="Acme",
        integration_id=uuid4(),
        primary_contact_id=old_contact_id,
        primary_contact_name="Old Contact",
        primary_contact_email="old@example.com",
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._upsert_contact_from_primary = AsyncMock(
        return_value=(None, "New Contact", "new@example.com")
    )
    service._record_mutation_event = AsyncMock()

    await service.update_primary_contact(
        deal.deal_id,
        primary_contact_name="New Contact",
        primary_contact_email="new@example.com",
    )

    kwargs = service._record_mutation_event.await_args.kwargs
    assert kwargs["event_type"] == "crm.deal.primary_contact_changed"
    assert kwargs["changed_fields"] == {
        "primary_contact_id": {"old": old_contact_id, "new": None},
        "primary_contact_name": {"old": "Old Contact", "new": "New Contact"},
        "primary_contact_email": {"old": "old@example.com", "new": "new@example.com"},
    }


def test_deal_mutation_plan_does_not_emit_participant_mutation_events() -> None:
    source = Path("src/service/crm/deals/write_service.py").read_text()

    assert "crm.deal.participant_added" not in source
    assert "crm.deal.participant_removed" not in source
    assert "crm.deal.stakeholder_updated" not in source
