from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import Response as FastAPIResponse
from pydantic import ValidationError
from src.api.routers.crm.accounts_handler import (
    create_account,
    get_account_contacts,
    search_accounts,
    update_account,
)
from src.api.schemas.crm_account_models import CreateAccountRequest, UpdateAccountRequest
from src.service.crm.accounts.contracts import AccountWriteContext, AccountWriteResult


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


def test_create_account_request_accepts_name_or_web_identity() -> None:
    assert CreateAccountRequest(name="Acme").name == "Acme"
    assert CreateAccountRequest(name="Acme", domain="acme.com").domain == "acme.com"
    assert CreateAccountRequest(domain="acme.com").domain == "acme.com"
    assert CreateAccountRequest(name="Acme", website="https://acme.com").website == (
        "https://acme.com"
    )
    assert CreateAccountRequest(website="https://acme.com").website == "https://acme.com"


def test_create_account_request_rejects_missing_identity() -> None:
    with pytest.raises(ValidationError) as exc_info:
        CreateAccountRequest(name="   ")

    assert "name, domain, or website is required" in str(exc_info.value)


@pytest.mark.asyncio
async def test_create_account_commits_session_after_service_write() -> None:
    user_ids = SimpleNamespace(user_id=uuid4(), organisation_id=uuid4())
    company = SimpleNamespace(model_dump=lambda: _company_payload())
    service = SimpleNamespace(
        session=SimpleNamespace(commit=AsyncMock()),
        create_account=AsyncMock(return_value=AccountWriteResult(company, True, "created")),
    )

    response = await create_account(
        response=FastAPIResponse(),
        body=CreateAccountRequest(
            name="Acme",
            domain="https://www.acme.com",
            website="https://acme.com",
            industry="Software",
        ),
        user_ids=user_ids,
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
        context=AccountWriteContext(
            source="api",
            actor_user_id=user_ids.user_id,
            created_via="accounts_app",
            duplicate_scope="strict_org",
        ),
    )
    service.session.commit.assert_awaited_once()
    assert response.status == "SUCCESS"


@pytest.mark.asyncio
async def test_update_account_commits_session_after_service_write() -> None:
    user_ids = SimpleNamespace(user_id=uuid4(), organisation_id=uuid4())
    company_id = uuid4()
    company = SimpleNamespace(model_dump=lambda: _company_payload())
    service = SimpleNamespace(
        session=SimpleNamespace(commit=AsyncMock()),
        update_account=AsyncMock(return_value=company),
    )

    response = await update_account(
        company_id=company_id,
        response=FastAPIResponse(),
        body=UpdateAccountRequest(name="Acme Corp", website="https://acme.com"),
        user_ids=user_ids,
        service=service,
        _crm_write_guard=None,
        _rl_write=None,
    )

    service.update_account.assert_awaited_once_with(
        company_id,
        {"name": "Acme Corp", "website": "https://acme.com"},
        context=AccountWriteContext(
            source="api",
            actor_user_id=user_ids.user_id,
            created_via="accounts_app",
            duplicate_scope="strict_org",
        ),
    )
    service.session.commit.assert_awaited_once()
    assert response.status == "SUCCESS"


@pytest.mark.asyncio
async def test_search_accounts_returns_cursor_page() -> None:
    account_id = uuid4()
    service = SimpleNamespace(
        search_accounts=AsyncMock(
            return_value=(
                [
                    {
                        "company_id": account_id,
                        "name": "Acme",
                        "domain": "acme.com",
                        "website": "https://acme.com",
                        "industry": "Software",
                    }
                ],
                "next-token",
            )
        )
    )

    response = await search_accounts(
        response=FastAPIResponse(),
        q="acme",
        limit=20,
        cursor=None,
        service=service,
        _rl_read=None,
    )

    service.search_accounts.assert_awaited_once_with(q="acme", limit=20, cursor=None)
    assert response.status == "SUCCESS"
    assert response.data is not None
    assert response.data.items[0].company_id == account_id
    assert response.data.next_cursor == "next-token"
    assert response.data.has_more is True


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
