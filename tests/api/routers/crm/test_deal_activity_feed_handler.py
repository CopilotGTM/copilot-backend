"""Tests for resource activity-feed handler wiring and presentation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import Response as FastAPIResponse
from src.api.routers.crm.accounts_handler import get_account_activity_feed
from src.api.routers.crm.contacts_handler import get_contact_activity_feed
from src.api.routers.crm.deals_handler import get_deal_activity_feed
from src.api.schemas.crm_activity_feed_models import ActivityFeedQueryParams
from src.domain.models.organisation_tables.sync.crm import CRMActivityObjectType


def test_activity_feed_routes_use_existing_resource_routers() -> None:
    app_source = Path("src/api/fastapi_app.py").read_text()
    router_source = Path("src/api/routers/crm/__init__.py").read_text()
    contacts_source = Path("src/api/routers/crm/contacts_handler.py").read_text()
    accounts_source = Path("src/api/routers/crm/accounts_handler.py").read_text()
    deals_source = Path("src/api/routers/crm/deals_handler.py").read_text()

    assert "deal_activity_feed_router" not in router_source
    assert "deal_activity_feed_router" not in app_source
    assert '@contacts_router.get(\n    "/contacts/{contact_id}/activity-feed"' in contacts_source
    assert '@accounts_router.get(\n    "/accounts/{company_id}/activity-feed"' in accounts_source
    assert '@deals_router.get(\n    "/deals/{deal_id}/activity-feed"' in deals_source


@pytest.mark.asyncio
async def test_get_contact_activity_feed_uses_contact_object_type() -> None:
    contact_id = uuid4()
    service = AsyncMock()
    service.list_object_activities = AsyncMock(return_value=([], None, 0))

    result = await get_contact_activity_feed(
        contact_id=contact_id,
        response=FastAPIResponse(),
        service=service,
        query=ActivityFeedQueryParams(),
    )

    assert result.status == "SUCCESS"
    assert result.data is not None
    assert result.data.object_type == CRMActivityObjectType.CONTACT.value
    assert result.data.object_id == contact_id
    service.list_object_activities.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_account_activity_feed_uses_company_object_type() -> None:
    company_id = uuid4()
    service = AsyncMock()
    service.list_object_activities = AsyncMock(return_value=([], None, 0))

    result = await get_account_activity_feed(
        company_id=company_id,
        response=FastAPIResponse(),
        service=service,
        query=ActivityFeedQueryParams(),
    )

    assert result.status == "SUCCESS"
    assert result.data is not None
    assert result.data.object_type == CRMActivityObjectType.COMPANY.value
    assert result.data.object_id == company_id
    service.list_object_activities.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_deal_activity_feed_uses_deal_object_type() -> None:
    """The deal route must query the shared feed using the DEAL link type."""
    deal_id = uuid4()
    service = AsyncMock()
    service.list_object_activities = AsyncMock(return_value=([], None, 0))
    access_service = AsyncMock()
    access_service.get_deal_by_id = AsyncMock(return_value=object())

    result = await get_deal_activity_feed(
        deal_id=deal_id,
        response=FastAPIResponse(),
        service=service,
        access_service=access_service,
        query=ActivityFeedQueryParams(),
    )

    assert result.status == "SUCCESS"
    assert result.data is not None
    assert result.data.object_type == CRMActivityObjectType.DEAL.value
    assert result.data.object_id == deal_id
    service.list_object_activities.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_deal_activity_feed_hides_inaccessible_deal() -> None:
    """A deal UUID must not bypass the existing relationship-based access policy."""
    service = AsyncMock()
    access_service = AsyncMock()
    access_service.get_deal_by_id = AsyncMock(return_value=None)
    response = FastAPIResponse()

    result = await get_deal_activity_feed(
        deal_id=uuid4(),
        response=response,
        service=service,
        access_service=access_service,
        query=ActivityFeedQueryParams(),
    )

    assert result.status == "ERROR"
    assert response.status_code == 404
    service.list_object_activities.assert_not_awaited()
