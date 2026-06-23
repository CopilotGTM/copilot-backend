"""Tests for resource activity-feed handler wiring and presentation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import Response as FastAPIResponse
from src.api.routers.crm.accounts_handler import get_account_activity_feed
from src.api.routers.crm.contacts_handler import get_contact_activity_feed
from src.api.routers.crm.deal_timeline_handler import get_deal_activity_feed
from src.api.schemas.crm_activity_feed_models import ActivityFeedQueryParams
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivity,
    CRMActivityLink,
    CRMActivityObjectType,
)
from src.service.crm.activity_feed_read_service import ActivityFeedRecord


def test_activity_feed_routes_use_existing_resource_routers() -> None:
    app_source = Path("src/api/fastapi_app.py").read_text()
    router_source = Path("src/api/routers/crm/__init__.py").read_text()
    timeline_source = Path("src/api/routers/crm/deal_timeline_handler.py").read_text()
    contacts_source = Path("src/api/routers/crm/contacts_handler.py").read_text()
    accounts_source = Path("src/api/routers/crm/accounts_handler.py").read_text()

    assert "deal_activity_feed_router" not in router_source
    assert "deal_activity_feed_router" not in app_source
    assert '@deal_timeline_router.get(\n    "/{deal_id}/activity-feed"' in timeline_source
    assert '@contacts_router.get(\n    "/contacts/{contact_id}/activity-feed"' in contacts_source
    assert '@accounts_router.get(\n    "/accounts/{company_id}/activity-feed"' in accounts_source


@pytest.mark.asyncio
async def test_get_deal_activity_feed_returns_projection_items() -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    activity_id = uuid4()
    occurred_at = datetime(2026, 6, 23, 10, 0, tzinfo=UTC)
    activity = CRMActivity(
        activity_id=activity_id,
        organisation_id=organisation_id,
        activity_type="TASK",
        source_type="TASK",
        source_record_id=str(activity_id),
        title="Follow up",
        preview="Call buyer",
        actor_name="Alex Morgan",
        display_payload={"status": "NOT_STARTED"},
    )
    link = CRMActivityLink(
        organisation_id=organisation_id,
        activity_id=activity_id,
        object_type="DEAL",
        object_id=deal_id,
        link_role="PRIMARY",
        link_source="SYSTEM",
        occurred_at=occurred_at,
    )
    service = AsyncMock()
    service.list_object_activities = AsyncMock(
        return_value=([ActivityFeedRecord(activity=activity, link=link)], None, 1)
    )

    result = await get_deal_activity_feed(
        deal_id=deal_id,
        response=FastAPIResponse(),
        service=service,
        query=ActivityFeedQueryParams(),
    )

    assert result.status == "SUCCESS"
    assert result.data is not None
    assert result.data.object_type == CRMActivityObjectType.DEAL.value
    assert result.data.object_id == deal_id
    assert result.data.total_count == 1
    assert result.data.items[0].activity_id == activity_id
    assert result.data.items[0].occurred_at == occurred_at
    assert result.data.items[0].display_payload == {"status": "NOT_STARTED"}
    service.list_object_activities.assert_awaited_once_with(
        object_type=CRMActivityObjectType.DEAL.value,
        object_id=deal_id,
        limit=20,
        cursor=None,
        activity_types=None,
        source_types=None,
        start_date=None,
        end_date=None,
        sort_order="desc",
    )


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
