"""Tests for object activity-feed reads backed by crm_activity_links."""

from __future__ import annotations

import base64
import json
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


def _decode_cursor_payload(cursor: str) -> dict[str, str]:
    return json.loads(base64.urlsafe_b64decode(cursor.encode()))


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
    session.execute = AsyncMock(
        side_effect=[
            _CountResult(),
            _RowsResult([(activity, occurred_at)]),
            _RowsResult([(link,)]),
        ]
    )

    records, cursor, total_count = await ActivityFeedReadService(
        session,
        organisation_id,
    ).list_object_activities(object_type=object_type, object_id=object_id)

    assert len(records) == 1
    assert records[0].activity.activity_id == activity.activity_id
    assert records[0].link.object_type == object_type
    assert records[0].link.object_id == object_id
    assert records[0].occurred_at == occurred_at
    assert records[0].related_links == (link,)
    assert cursor is None
    assert total_count == 3

    statements = [str(call.args[0]) for call in session.execute.await_args_list]
    assert all("crm_activity_links" in statement for statement in statements)
    assert "GROUP BY crm_activity_links.activity_id" in statements[0]
    assert "GROUP BY crm_activity_links.activity_id" in statements[1]
    assert "crm_activities" in statements[0]
    assert "crm_activities" in statements[1]


@pytest.mark.asyncio
async def test_list_deal_activities_uses_cursor_only_for_page_rows_not_total_count() -> None:
    occurred_at = datetime(2026, 6, 23, 9, 30, tzinfo=UTC)
    organisation_id, deal_id, activity_1, link_1 = _activity_link_pair(occurred_at=occurred_at)
    _, _, activity_2, link_2 = _activity_link_pair(occurred_at=occurred_at - timedelta(hours=1))
    link_2.organisation_id = organisation_id
    link_2.object_id = deal_id
    activity_2.organisation_id = organisation_id
    cursor = encode_cursor(occurred_at, activity_1.activity_id)
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _CountResult(),
            _RowsResult([(activity_2, link_2.occurred_at)]),
            _RowsResult([(link_2,)]),
        ]
    )

    records, next_cursor, total_count = await ActivityFeedReadService(
        session,
        organisation_id,
    ).list_deal_activities(deal_id=deal_id, cursor=cursor, limit=1)

    assert len(records) == 1
    assert next_cursor is None
    assert total_count == 3

    count_sql = str(session.execute.await_args_list[0].args[0])
    page_sql = str(session.execute.await_args_list[1].args[0])
    assert "occurred_at <" not in count_sql
    assert "occurred_at <" in page_sql


@pytest.mark.asyncio
async def test_list_deal_activities_returns_one_card_for_multiple_links_to_same_object() -> None:
    occurred_at = datetime(2026, 6, 23, 9, 30, tzinfo=UTC)
    organisation_id, deal_id, activity, primary_link = _activity_link_pair(
        occurred_at=occurred_at,
    )
    related_link = primary_link.model_copy(deep=True)
    related_link.activity_link_id = uuid4()
    related_link.link_role = "RELATED"
    _, _, next_activity, next_link = _activity_link_pair(
        occurred_at=occurred_at - timedelta(hours=1),
    )
    next_activity.organisation_id = organisation_id
    next_link.organisation_id = organisation_id
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _CountResult(),
            _RowsResult(
                [
                    (activity, occurred_at),
                    (next_activity, next_link.occurred_at),
                ]
            ),
            _RowsResult([(primary_link,), (related_link,)]),
        ]
    )

    records, next_cursor, _total_count = await ActivityFeedReadService(
        session,
        organisation_id,
    ).list_deal_activities(deal_id=deal_id, limit=1)

    assert len(records) == 1
    assert records[0].activity.activity_id == activity.activity_id
    assert records[0].related_links == (primary_link, related_link)
    assert next_cursor is not None
    cursor_payload = _decode_cursor_payload(next_cursor)
    assert cursor_payload["activity_id"] == str(activity.activity_id)
    assert "activity_link_id" not in cursor_payload

    page_sql = str(session.execute.await_args_list[1].args[0])
    assert "GROUP BY crm_activity_links.activity_id" in page_sql


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


@pytest.mark.parametrize(
    ("start_date", "end_date", "message"),
    [
        (datetime(2026, 7, 2, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC), "start_date"),
        (datetime(2026, 7, 1), None, "timezone"),
    ],
)
@pytest.mark.asyncio
async def test_list_object_activities_rejects_ambiguous_date_bounds(
    start_date: datetime,
    end_date: datetime | None,
    message: str,
) -> None:
    session = AsyncMock()

    with pytest.raises(ActivityFeedReadServiceError, match=message) as exc_info:
        await ActivityFeedReadService(session, uuid4()).list_object_activities(
            object_type="CONTACT",
            object_id=uuid4(),
            start_date=start_date,
            end_date=end_date,
        )

    assert exc_info.value.status_code == 400
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_list_object_activities_accepts_cursor_from_link_paginated_release() -> None:
    occurred_at = datetime(2026, 6, 23, 9, 30, tzinfo=UTC)
    activity_id = uuid4()
    old_cursor = base64.urlsafe_b64encode(
        json.dumps(
            {
                "ts": occurred_at.isoformat(),
                "activity_id": str(activity_id),
                "activity_link_id": str(uuid4()),
            }
        ).encode()
    ).decode()
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[_CountResult(), _RowsResult([])])

    records, next_cursor, _total_count = await ActivityFeedReadService(
        session,
        uuid4(),
    ).list_object_activities(
        object_type="CONTACT",
        object_id=uuid4(),
        cursor=old_cursor,
    )

    assert records == []
    assert next_cursor is None
    page_sql = str(session.execute.await_args_list[1].args[0])
    assert "scoped_activity_links.occurred_at <" in page_sql
    assert "activity_link_id <" not in page_sql


@pytest.mark.asyncio
async def test_list_object_activities_skips_link_removed_during_page_hydration() -> None:
    occurred_at = datetime(2026, 6, 23, 9, 30, tzinfo=UTC)
    organisation_id, object_id, activity, _link = _activity_link_pair(
        occurred_at=occurred_at,
        object_type="CONTACT",
    )
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _CountResult(),
            _RowsResult([(activity, occurred_at)]),
            _RowsResult([]),
        ]
    )

    records, next_cursor, total_count = await ActivityFeedReadService(
        session,
        organisation_id,
    ).list_object_activities(object_type="CONTACT", object_id=object_id)

    assert records == []
    assert next_cursor is None
    assert total_count == 3
