"""Route wiring tests for CRM read-only email sidebar APIs."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import Response as FastAPIResponse
from src.api.routers.crm.emails_handler import (
    get_account_emails,
    get_contact_emails,
    get_deal_emails,
    list_crm_emails,
)
from src.api.schemas.crm_email_models import CRMEmailPage
from src.service.crm.emails.read_service import CRMEmailScope


def _user():
    return SimpleNamespace(user_id=uuid4(), user_role="MEMBER", user_email="seller@example.com")


@pytest.mark.asyncio
async def test_overall_email_route_passes_optional_integration_filter_to_shared_service():
    integration_id = uuid4()
    service = SimpleNamespace(
        list_emails=AsyncMock(return_value=CRMEmailPage(items=[], cursor=None))
    )

    result = await list_crm_emails(
        response=FastAPIResponse(),
        integration_id=integration_id,
        limit=20,
        cursor=None,
        service=service,
    )

    service.list_emails.assert_awaited_once_with(
        scope=CRMEmailScope.OVERALL,
        object_id=None,
        integration_id=integration_id,
        limit=20,
        cursor=None,
    )
    assert result.data.items == []


@pytest.mark.asyncio
async def test_contact_and_account_routes_select_their_own_scope():
    service = SimpleNamespace(
        list_emails=AsyncMock(return_value=CRMEmailPage(items=[], cursor=None))
    )
    contact_id = uuid4()
    account_id = uuid4()

    await get_contact_emails(contact_id, FastAPIResponse(), 20, None, service)
    await get_account_emails(account_id, FastAPIResponse(), 20, None, service)

    assert service.list_emails.await_args_list[0].kwargs["scope"] is CRMEmailScope.CONTACT
    assert service.list_emails.await_args_list[1].kwargs["scope"] is CRMEmailScope.ACCOUNT


@pytest.mark.asyncio
async def test_deal_email_route_returns_not_found_before_querying_email_service():
    service = SimpleNamespace(list_emails=AsyncMock())
    access_service = SimpleNamespace(get_deal_by_id=AsyncMock(return_value=None))

    result = await get_deal_emails(
        uuid4(),
        FastAPIResponse(),
        20,
        None,
        service,
        access_service,
    )

    assert result.status == "ERROR"
    assert result.message == "Deal not found"
    service.list_emails.assert_not_awaited()
