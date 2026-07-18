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
    """One unique activity plus its scoped and navigable object links."""

    activity: CRMActivity
    link: CRMActivityLink
    occurred_at: datetime | None = None
    related_links: tuple[CRMActivityLink, ...] = ()


def _table(model: Any) -> Table:
    table = getattr(model, "__table__", None)
    if table is None:
        raise ActivityFeedReadServiceError(f"Model missing table metadata: {model}")
    return cast(Table, table)


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip().upper() for part in value.split(",") if part.strip()]


def _validate_date_bounds(start_date: datetime | None, end_date: datetime | None) -> None:
    """Reject ambiguous or reversed timestamps before issuing database work."""
    for name, value in (("start_date", start_date), ("end_date", end_date)):
        if value is not None and value.utcoffset() is None:
            raise ActivityFeedReadServiceError(f"{name} must include a timezone", 400)
    if start_date and end_date and start_date > end_date:
        raise ActivityFeedReadServiceError(
            "start_date must be before or equal to end_date",
            400,
        )


def _encode_activity_feed_cursor(
    *,
    occurred_at: datetime,
    activity_id: UUID,
) -> str:
    blob = json.dumps(
        {
            "ts": occurred_at.isoformat(),
            "activity_id": str(activity_id),
        }
    ).encode()
    return base64.urlsafe_b64encode(blob).decode()


def _decode_activity_feed_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except Exception:
        return decode_cursor(cursor)

    if "activity_id" not in data:
        return decode_cursor(cursor)

    # Cursors emitted before unique-activity pagination also contain an
    # activity_link_id. It is intentionally ignored so deployed cursors remain valid.
    return datetime.fromisoformat(data["ts"]), UUID(data["activity_id"])


class ActivityFeedReadService:
    """Object-scoped read service for the canonical CRM activity feed."""

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    async def close(self) -> None:
        """Close the organisation-scoped database session owned by this service."""
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
        """Return one card per activity linked to an object, ordered by occurrence time.

        A single activity can have multiple roles for the same object. The scoped link
        subquery collapses those roles before counting and paginating so duplicates do not
        consume page slots; the bounded page is then hydrated with its related object links.
        """
        _validate_date_bounds(start_date, end_date)
        link_t = _table(CRMActivityLink)
        activity_t = _table(CRMActivity)
        type_values = _split_csv(activity_types)
        source_values = _split_csv(source_types)

        scoped_links = (
            select(
                link_t.c.activity_id.label("activity_id"),
                func.max(link_t.c.occurred_at).label("occurred_at"),
            )
            .where(
                link_t.c.organisation_id == self.organisation_id,
                link_t.c.object_type == object_type,
                link_t.c.object_id == object_id,
            )
            .group_by(link_t.c.activity_id)
            .subquery("scoped_activity_links")
        )
        where = [
            activity_t.c.organisation_id == self.organisation_id,
            or_(activity_t.c.is_deleted.is_(False), activity_t.c.is_deleted.is_(None)),
        ]
        if type_values:
            where.append(activity_t.c.activity_type.in_(type_values))
        if source_values:
            where.append(activity_t.c.source_type.in_(source_values))
        if start_date:
            where.append(scoped_links.c.occurred_at >= start_date)
        if end_date:
            where.append(scoped_links.c.occurred_at <= end_date)

        count_where = list(where)

        if cursor:
            try:
                ts, activity_id = _decode_activity_feed_cursor(cursor)
            except Exception as exc:
                raise ActivityFeedReadServiceError("Invalid cursor", 400) from exc
            if sort_order == "desc":
                cursor_predicate = or_(
                    scoped_links.c.occurred_at < ts,
                    and_(
                        scoped_links.c.occurred_at == ts,
                        scoped_links.c.activity_id < activity_id,
                    ),
                )
                where.append(cursor_predicate)
            else:
                cursor_predicate = or_(
                    scoped_links.c.occurred_at > ts,
                    and_(
                        scoped_links.c.occurred_at == ts,
                        scoped_links.c.activity_id > activity_id,
                    ),
                )
                where.append(cursor_predicate)

        base_join = activity_t.join(
            scoped_links,
            activity_t.c.activity_id == scoped_links.c.activity_id,
        )
        count_stmt = select(func.count()).select_from(base_join).where(*count_where)
        total_count = (await self.session.execute(count_stmt)).scalar() or 0

        stmt = select(CRMActivity, scoped_links.c.occurred_at).select_from(base_join).where(*where)
        if sort_order == "desc":
            stmt = stmt.order_by(
                scoped_links.c.occurred_at.desc(),
                scoped_links.c.activity_id.desc(),
            )
        else:
            stmt = stmt.order_by(
                scoped_links.c.occurred_at.asc(),
                scoped_links.c.activity_id.asc(),
            )
        stmt = stmt.limit(limit + 1)

        result = await self.session.execute(stmt)
        page_rows = result.all()

        next_cursor = None
        if len(page_rows) > limit:
            page_rows = page_rows[:limit]
            last_activity, last_occurred_at = page_rows[-1]
            next_cursor = _encode_activity_feed_cursor(
                occurred_at=last_occurred_at,
                activity_id=last_activity.activity_id,
            )

        if not page_rows:
            return [], next_cursor, total_count

        activity_ids = [row[0].activity_id for row in page_rows]
        links_stmt = (
            select(CRMActivityLink)
            .where(
                link_t.c.organisation_id == self.organisation_id,
                link_t.c.activity_id.in_(activity_ids),
            )
            .order_by(
                link_t.c.activity_id,
                link_t.c.object_type,
                link_t.c.object_id,
                link_t.c.link_role,
            )
        )
        links_result = await self.session.execute(links_stmt)
        links_by_activity: dict[UUID, list[CRMActivityLink]] = {}
        for row in links_result.all():
            link = row[0]
            links_by_activity.setdefault(link.activity_id, []).append(link)

        records = []
        for activity, occurred_at in page_rows:
            related_links = links_by_activity.get(activity.activity_id, [])
            scoped_link = next(
                (
                    link
                    for link in related_links
                    if link.object_type == object_type and link.object_id == object_id
                ),
                None,
            )
            if scoped_link is None:
                # Projection workers may replace links between the page and hydration reads.
                # Advancing the opaque cursor while omitting that vanished card avoids a transient 500.
                continue
            records.append(
                ActivityFeedRecord(
                    activity=activity,
                    link=scoped_link,
                    occurred_at=occurred_at,
                    related_links=tuple(related_links),
                )
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
