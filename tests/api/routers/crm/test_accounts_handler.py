from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import src.api.routers.crm.accounts_handler as accounts_handler
from fastapi import Response as FastAPIResponse
from pydantic import ValidationError
from src.api.routers.crm.accounts_handler import (
    create_account,
    get_account_contacts,
    get_account_detail,
    list_accounts,
    search_accounts,
    update_account,
)
from src.api.schemas.crm_account_models import CreateAccountRequest, UpdateAccountRequest
from src.service.crm.accounts.contracts import (
    AccountCreateCommand,
    AccountWriteContext,
    AccountWriteResult,
)


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
        AccountCreateCommand(
            name="Acme",
            domain="https://www.acme.com",
            website="https://acme.com",
            industry="Software",
            linkedin_url=None,
        ),
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
async def test_create_account_dispatches_account_research_after_commit(monkeypatch) -> None:
    user_ids = SimpleNamespace(user_id=uuid4(), organisation_id=uuid4())
    company_id = uuid4()
    company = SimpleNamespace(company_id=company_id, model_dump=lambda: _company_payload())
    service = SimpleNamespace(
        session=SimpleNamespace(commit=AsyncMock()),
        create_account=AsyncMock(return_value=AccountWriteResult(company, True, "created")),
    )
    calls: list[dict] = []

    async def fake_dispatch(**kwargs):
        calls.append(
            {
                **kwargs,
                "commit_count_at_dispatch": service.session.commit.await_count,
            }
        )

    monkeypatch.setattr(
        accounts_handler,
        "dispatch_account_research_async",
        fake_dispatch,
        raising=False,
    )

    response = await create_account(
        response=FastAPIResponse(),
        body=CreateAccountRequest(name="Acme", domain="acme.com"),
        user_ids=user_ids,
        service=service,
        _crm_write_guard=None,
        _rl_write=None,
    )

    assert response.status == "SUCCESS"
    assert calls == [
        {
            "organisation_id": user_ids.organisation_id,
            "company_id": company_id,
            "user_id": user_ids.user_id,
            "trigger": "account_created",
            "commit_count_at_dispatch": 1,
        }
    ]


@pytest.mark.asyncio
async def test_create_account_ignores_account_research_dispatch_failure(monkeypatch) -> None:
    user_ids = SimpleNamespace(user_id=uuid4(), organisation_id=uuid4())
    company = SimpleNamespace(company_id=uuid4(), model_dump=lambda: _company_payload())
    service = SimpleNamespace(
        session=SimpleNamespace(commit=AsyncMock()),
        create_account=AsyncMock(return_value=AccountWriteResult(company, True, "created")),
    )

    async def failing_dispatch(**_kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(
        accounts_handler,
        "dispatch_account_research_async",
        failing_dispatch,
        raising=False,
    )

    response = await create_account(
        response=FastAPIResponse(),
        body=CreateAccountRequest(name="Acme", domain="acme.com"),
        user_ids=user_ids,
        service=service,
        _crm_write_guard=None,
        _rl_write=None,
    )

    assert response.status == "SUCCESS"
    service.session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_accounts_returns_nested_owner_without_source() -> None:
    company_id = uuid4()
    owner_id = uuid4()
    now = datetime.now(UTC)
    service = SimpleNamespace(
        list_accounts=AsyncMock(
            return_value=(
                [
                    {
                        "company_id": company_id,
                        "name": "Acme",
                        "domain": "acme.com",
                        "website": "https://acme.com",
                        "industry": "Software",
                        "lifecycle_stage": "customer",
                        "owner_name": "Jane Smith",
                        "owner": {
                            "id": owner_id,
                            "name": "Jane Smith",
                            "email": "jane@example.com",
                        },
                        "number_of_employees": 250,
                        "annual_revenue": 50000000.0,
                        "phone": "+1-555-0100",
                        "country": "US",
                        "linkedin_url": "https://linkedin.com/company/acme",
                        "is_active": True,
                        "contact_count": 12,
                        "deal_count": 3,
                        "pipeline_value": 150000.0,
                        "created_at": now,
                        "updated_at": now,
                    }
                ],
                1,
            )
        )
    )

    response = await list_accounts(
        response=FastAPIResponse(),
        page=1,
        limit=25,
        q=None,
        industry=None,
        service=service,
        _rl_read=None,
    )

    service.list_accounts.assert_awaited_once_with(page=1, limit=25, q=None, industry=None)
    assert response.status == "SUCCESS"
    assert response.data is not None
    item = response.data.items[0]
    dumped = item.model_dump()
    assert dumped["owner"] == {
        "id": owner_id,
        "name": "Jane Smith",
        "email": "jane@example.com",
    }
    assert dumped["owner_name"] == "Jane Smith"
    assert "source" not in dumped


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
async def test_update_account_accepts_all_advertised_profile_fields() -> None:
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
        body=UpdateAccountRequest(
            name="Acme Corp",
            domain="acme.com",
            website="https://acme.com",
            industry="Software",
            lifecycle_stage="customer",
            company_type="Private",
            description="Account description",
            number_of_employees=250,
            annual_revenue=50000000.0,
            phone="+1-555-0100",
            linkedin_url="https://linkedin.com/company/acme",
            address="123 Main St",
            city="Pune",
            state="Maharashtra",
            postal_code="411001",
            country="IN",
            timezone="Asia/Kolkata",
        ),
        user_ids=user_ids,
        service=service,
        _crm_write_guard=None,
        _rl_write=None,
    )

    service.update_account.assert_awaited_once_with(
        company_id,
        {
            "name": "Acme Corp",
            "domain": "acme.com",
            "website": "https://acme.com",
            "industry": "Software",
            "lifecycle_stage": "customer",
            "company_type": "Private",
            "description": "Account description",
            "number_of_employees": 250,
            "annual_revenue": 50000000.0,
            "phone": "+1-555-0100",
            "linkedin_url": "https://linkedin.com/company/acme",
            "address": "123 Main St",
            "city": "Pune",
            "state": "Maharashtra",
            "postal_code": "411001",
            "country": "IN",
            "timezone": "Asia/Kolkata",
        },
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


@pytest.mark.asyncio
async def test_get_account_detail_returns_compact_contacts_and_summary_placeholder() -> None:
    company_id = uuid4()
    contact_id = uuid4()
    deal_id = uuid4()
    now = datetime.now(UTC)
    service = SimpleNamespace(
        get_account_detail=AsyncMock(
            return_value={
                "company_id": company_id,
                "name": "Acme",
                "domain": "acme.com",
                "website": "https://acme.com",
                "industry": "Software",
                "lifecycle_stage": "customer",
                "owner_name": "Jane Smith",
                "owner": {
                    "id": uuid4(),
                    "name": "Jane Smith",
                    "email": "jane@example.com",
                },
                "number_of_employees": 250,
                "annual_revenue": 50000000.0,
                "phone": "+1-555-0100",
                "country": "US",
                "linkedin_url": "https://linkedin.com/company/acme",
                "is_active": True,
                "contact_count": 1,
                "deal_count": 1,
                "pipeline_value": 10000.0,
                "created_at": now,
                "updated_at": now,
                "description": "Account description",
                "company_type": "Private",
                "address": "123 Main St",
                "city": "Pune",
                "state": "Maharashtra",
                "postal_code": "411001",
                "timezone": "Asia/Kolkata",
                "account_summary": {
                    "status": "not_available",
                    "content": None,
                    "last_researched_at": None,
                },
                "contacts": [
                    {
                        "contact_id": contact_id,
                        "full_name": "Roop Singh",
                        "email": "roop.s@acme.com",
                        "job_title": "CEO",
                        "deal_count": 5,
                    }
                ],
                "deals": [
                    {
                        "deal_id": deal_id,
                        "name": "Acme Expansion",
                        "amount": 10000.0,
                        "currency": "USD",
                        "deal_status": "open",
                        "stage_name": "Negotiation",
                        "owner_name": "Jane Smith",
                    }
                ],
            }
        )
    )

    response = await get_account_detail(
        company_id=company_id,
        response=FastAPIResponse(),
        service=service,
        _rl_read=None,
    )

    service.get_account_detail.assert_awaited_once_with(company_id)
    assert response.status == "SUCCESS"
    assert response.data is not None
    dumped = response.data.model_dump()
    detail_fields = dumped["detail_field_definitions"]
    fields_by_key = {field["key"]: field for field in detail_fields}
    assert [field["order"] for field in detail_fields] == list(range(1, 25))
    assert "contact_count" not in fields_by_key
    assert "deal_count" not in fields_by_key
    assert "pipeline_value" not in fields_by_key
    assert fields_by_key["name"] == {
        "key": "name",
        "label": "Account name",
        "data_type": "string",
        "order": 1,
        "section": "account",
        "filter_control": "text",
        "editable": True,
        "patch_field": "name",
        "edit_control": "text",
        "edit_policy": "always",
        "locked_reason": None,
    }
    assert fields_by_key["owner.id"] == {
        "key": "owner.id",
        "label": "Owner ID",
        "data_type": "uuid",
        "order": 9,
        "section": "owner",
        "filter_control": "link",
        "editable": False,
        "patch_field": None,
        "edit_control": None,
        "edit_policy": "assignment_action",
        "locked_reason": "Use an account owner assignment action.",
    }
    assert fields_by_key["account_summary.content"]["edit_policy"] == "generated"
    assert dumped["account_summary"] == {
        "status": "not_available",
        "content": None,
        "last_researched_at": None,
    }
    assert dumped["contacts"] == [
        {
            "contact_id": contact_id,
            "full_name": "Roop Singh",
            "email": "roop.s@acme.com",
            "job_title": "CEO",
        }
    ]
    assert dumped["deals"] == [
        {
            "deal_id": deal_id,
            "name": "Acme Expansion",
            "amount": 10000.0,
            "currency": "USD",
            "deal_status": "open",
            "stage_name": "Negotiation",
            "owner_name": "Jane Smith",
        }
    ]
