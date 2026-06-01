from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import Response as FastAPIResponse
from src.api.routers.crm.accounts_handler import create_account, get_account_contacts
from src.api.schemas.crm_account_models import CreateAccountRequest


def _company_payload() -> dict:
    return {
        "company_id": uuid4(),
        "name": "Acme",
        "domain": "acme.com",
        "website": "https://acme.com",
        "industry": "Software",
        "lifecycle_stage": None,
        "owner_name": None,
        "number_of_employees": None,
        "annual_revenue": None,
        "phone": None,
        "country": None,
        "linkedin_url": None,
        "is_active": True,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }


@pytest.mark.asyncio
async def test_create_account_commits_session_after_service_write() -> None:
    service = SimpleNamespace(
        org_session=SimpleNamespace(commit=AsyncMock()),
        create_account=AsyncMock(
            return_value=SimpleNamespace(model_dump=lambda: _company_payload())
        ),
    )

    response = await create_account(
        response=FastAPIResponse(),
        body=CreateAccountRequest(
            name="Acme",
            domain="https://www.acme.com",
            website="https://acme.com",
            industry="Software",
        ),
        service=service,
        _crm_write_guard=None,
        _rl_write=None,
    )

    service.create_account.assert_awaited_once_with(
        name="Acme",
        domain="https://www.acme.com",
        website="https://acme.com",
        industry="Software",
        linkedin_url=None,
    )
    service.org_session.commit.assert_awaited_once()
    assert response.status == "SUCCESS"


@pytest.mark.asyncio
async def test_get_account_contacts_passes_limit_and_preserves_deal_count() -> None:
    company_id = uuid4()
    contact_id = uuid4()
    service = SimpleNamespace(
        list_account_contacts=AsyncMock(
            return_value=[
                {
                    "contact_id": contact_id,
                    "full_name": "Sarah Chen",
                    "email": "sarah@acme.com",
                    "job_title": "VP Sales",
                    "deal_count": 3,
                }
            ]
        )
    )

    response = await get_account_contacts(
        company_id=company_id,
        response=FastAPIResponse(),
        limit=25,
        service=service,
        _rl_read=None,
    )

    service.list_account_contacts.assert_awaited_once_with(company_id, limit=25)
    assert response.status == "SUCCESS"
    assert response.data is not None
    assert response.data[0].contact_id == contact_id
    assert response.data[0].full_name == "Sarah Chen"
    assert response.data[0].deal_count == 3
