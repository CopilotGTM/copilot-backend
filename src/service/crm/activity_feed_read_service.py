"""Read CRM object activity-feed rows from crm_activity_links and crm_activities."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.schema import Table
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivity,
    CRMActivityLink,
    CRMActivityObjectType,
)
from src.infra.utils.cursor import decode_cursor


class ActivityFeedReadServiceError(Exception):
    """Raised when activity-feed reads cannot be completed."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True)
class ActivityFeedRecord:
    """Joined projection row plus the object-link row that made it visible."""

    activity: CRMActivity
    link: CRMActivityLink


def _table(model: Any) -> Table:
    table = getattr(model, "__table__", None)
    if table is None:
        raise ActivityFeedReadServiceError(f"Model missing table metadata: {model}")
    return cast(Table, table)


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip().upper() for part in value.split(",") if part.strip()]


def _encode_activity_feed_cursor(
    *,
    occurred_at: datetime,
    activity_id: UUID,
    activity_link_id: UUID,
) -> str:
    blob = json.dumps(
        {
            "ts": occurred_at.isoformat(),
            "activity_id": str(activity_id),
            "activity_link_id": str(activity_link_id),
        }
    ).encode()
    return base64.urlsafe_b64encode(blob).decode()


def _decode_activity_feed_cursor(cursor: str) -> tuple[datetime, UUID, UUID | None]:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except Exception:
        ts, activity_id = decode_cursor(cursor)
        return ts, activity_id, None

    if "activity_id" not in data or "activity_link_id" not in data:
        ts, activity_id = decode_cursor(cursor)
        return ts, activity_id, None

    return (
        datetime.fromisoformat(data["ts"]),
        UUID(data["activity_id"]),
        UUID(data["activity_link_id"]),
    )


class ActivityFeedReadService:
    """Object-scoped read service for the canonical CRM activity feed."""

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    async def close(self) -> None:
        await self.session.close()

    async def list_object_activities(
        self,
        *,
        object_type: str,
        object_id: UUID,
        limit: int = 20,
        cursor: str | None = None,
        activity_types: str | None = None,
        source_types: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        sort_order: str = "desc",
    ) -> tuple[list[ActivityFeedRecord], str | None, int]:
        link_t = _table(CRMActivityLink)
        activity_t = _table(CRMActivity)
        type_values = _split_csv(activity_types)
        source_values = _split_csv(source_types)

        where = [
            link_t.c.organisation_id == self.organisation_id,
            link_t.c.object_type == object_type,
            link_t.c.object_id == object_id,
            activity_t.c.organisation_id == self.organisation_id,
            or_(activity_t.c.is_deleted.is_(False), activity_t.c.is_deleted.is_(None)),
        ]
        if type_values:
            where.append(activity_t.c.activity_type.in_(type_values))
        if source_values:
            where.append(activity_t.c.source_type.in_(source_values))
        if start_date:
            where.append(link_t.c.occurred_at >= start_date)
        if end_date:
            where.append(link_t.c.occurred_at <= end_date)

        count_where = list(where)

        if cursor:
            try:
                ts, activity_id, activity_link_id = _decode_activity_feed_cursor(cursor)
            except Exception as exc:
                raise ActivityFeedReadServiceError("Invalid cursor", 400) from exc
            if sort_order == "desc":
                cursor_predicate = or_(
                    link_t.c.occurred_at < ts,
                    and_(
                        link_t.c.occurred_at == ts,
                        link_t.c.activity_id < activity_id,
                    ),
                )
                if activity_link_id is not None:
                    cursor_predicate = or_(
                        cursor_predicate,
                        and_(
                            link_t.c.occurred_at == ts,
                            link_t.c.activity_id == activity_id,
                            link_t.c.activity_link_id < activity_link_id,
                        ),
                    )
                where.append(cursor_predicate)
            else:
                cursor_predicate = or_(
                    link_t.c.occurred_at > ts,
                    and_(
                        link_t.c.occurred_at == ts,
                        link_t.c.activity_id > activity_id,
                    ),
                )
                if activity_link_id is not None:
                    cursor_predicate = or_(
                        cursor_predicate,
                        and_(
                            link_t.c.occurred_at == ts,
                            link_t.c.activity_id == activity_id,
                            link_t.c.activity_link_id > activity_link_id,
                        ),
                    )
                where.append(cursor_predicate)

        base_join = link_t.join(
            activity_t,
            (activity_t.c.organisation_id == link_t.c.organisation_id)
            & (activity_t.c.activity_id == link_t.c.activity_id),
        )
        count_stmt = select(func.count()).select_from(base_join).where(*count_where)
        total_count = (await self.session.execute(count_stmt)).scalar() or 0

        stmt = select(CRMActivity, CRMActivityLink).select_from(base_join).where(*where)
        if sort_order == "desc":
            stmt = stmt.order_by(
                link_t.c.occurred_at.desc(),
                link_t.c.activity_id.desc(),
                link_t.c.activity_link_id.desc(),
            )
        else:
            stmt = stmt.order_by(
                link_t.c.occurred_at.asc(),
                link_t.c.activity_id.asc(),
                link_t.c.activity_link_id.asc(),
            )
        stmt = stmt.limit(limit + 1)

        result = await self.session.execute(stmt)
        records = [ActivityFeedRecord(activity=row[0], link=row[1]) for row in result.all()]

        next_cursor = None
        if len(records) > limit:
            records = records[:limit]
            last = records[-1].link
            next_cursor = _encode_activity_feed_cursor(
                occurred_at=last.occurred_at,
                activity_id=last.activity_id,
                activity_link_id=last.activity_link_id,
            )

        return records, next_cursor, total_count

    async def list_deal_activities(
        self,
        *,
        deal_id: UUID,
        limit: int = 20,
        cursor: str | None = None,
        activity_types: str | None = None,
        source_types: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        sort_order: str = "desc",
    ) -> tuple[list[ActivityFeedRecord], str | None, int]:
        """Compatibility wrapper for deal activity-feed reads."""
        return await self.list_object_activities(
            object_type=CRMActivityObjectType.DEAL.value,
            object_id=deal_id,
            limit=limit,
            cursor=cursor,
            activity_types=activity_types,
            source_types=source_types,
            start_date=start_date,
            end_date=end_date,
            sort_order=sort_order,
        )
