"""Tests for object activity-feed reads backed by crm_activity_links."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivity,
    CRMActivityLink,
    CRMActivityObjectType,
)
from src.infra.utils.cursor import encode_cursor
from src.service.crm.activity_feed_read_service import (
    ActivityFeedReadService,
    ActivityFeedReadServiceError,
)


class _CountResult:
    def scalar(self) -> int:
        return 3


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


def _activity_link_pair(*, occurred_at: datetime, object_type: str = "DEAL"):
    organisation_id = uuid4()
    object_id = uuid4()
    activity_id = uuid4()
    activity = CRMActivity(
        activity_id=activity_id,
        organisation_id=organisation_id,
        activity_type="NOTE",
        title="Buyer note",
        preview="Discussed pricing",
        source_type="NOTE",
        source_record_id=str(activity_id),
        source_provider="hubspot",
        display_payload={"body_format": "html"},
    )
    link = CRMActivityLink(
        organisation_id=organisation_id,
        activity_id=activity_id,
        object_type=object_type,
        object_id=object_id,
        link_role="PRIMARY",
        link_source="SYSTEM",
        occurred_at=occurred_at,
    )
    return organisation_id, object_id, activity, link


@pytest.mark.parametrize(
    "object_type",
    [
        CRMActivityObjectType.DEAL.value,
        CRMActivityObjectType.CONTACT.value,
        CRMActivityObjectType.COMPANY.value,
    ],
)
@pytest.mark.asyncio
async def test_list_object_activities_reads_activity_links_joined_to_activity_projection(
    object_type: str,
) -> None:
    occurred_at = datetime(2026, 6, 23, 9, 30, tzinfo=UTC)
    organisation_id, object_id, activity, link = _activity_link_pair(
        occurred_at=occurred_at,
        object_type=object_type,
    )
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[_CountResult(), _RowsResult([(activity, link)])])

    records, cursor, total_count = await ActivityFeedReadService(
        session,
        organisation_id,
    ).list_object_activities(object_type=object_type, object_id=object_id)

    assert len(records) == 1
    assert records[0].activity.activity_id == activity.activity_id
    assert records[0].link.object_type == object_type
    assert records[0].link.object_id == object_id
    assert cursor is None
    assert total_count == 3

    statements = [str(call.args[0]) for call in session.execute.await_args_list]
    assert all("crm_activity_links" in statement for statement in statements)
    assert all("crm_activities" in statement for statement in statements)
    assert all("deal_timeline_events" not in statement for statement in statements)


@pytest.mark.asyncio
async def test_list_deal_activities_uses_cursor_only_for_page_rows_not_total_count() -> None:
    occurred_at = datetime(2026, 6, 23, 9, 30, tzinfo=UTC)
    organisation_id, deal_id, activity_1, link_1 = _activity_link_pair(occurred_at=occurred_at)
    _, _, activity_2, link_2 = _activity_link_pair(occurred_at=occurred_at - timedelta(hours=1))
    link_2.organisation_id = organisation_id
    activity_2.organisation_id = organisation_id
    cursor = encode_cursor(occurred_at, activity_1.activity_id)
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[_CountResult(), _RowsResult([(activity_2, link_2)])])

    records, next_cursor, total_count = await ActivityFeedReadService(
        session,
        organisation_id,
    ).list_deal_activities(deal_id=deal_id, cursor=cursor, limit=1)

    assert len(records) == 1
    assert next_cursor is None
    assert total_count == 3

    count_sql = str(session.execute.await_args_list[0].args[0])
    page_sql = str(session.execute.await_args_list[1].args[0])
    assert "crm_activity_links.occurred_at <" not in count_sql
    assert "crm_activity_links.occurred_at <" in page_sql


@pytest.mark.asyncio
async def test_list_deal_activities_rejects_invalid_cursor() -> None:
    session = AsyncMock()

    with pytest.raises(ActivityFeedReadServiceError) as exc_info:
        await ActivityFeedReadService(session, uuid4()).list_deal_activities(
            deal_id=uuid4(),
            cursor="not-a-cursor",
        )

    assert exc_info.value.status_code == 400
    session.execute.assert_not_called()
