"""Backfill supported meeting join links from stored calendar provider payloads.

Dry-run is the default. Use --apply only after reviewing counts and sample rows.
Recording tasks are queued only after the update transaction commits.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import text
from src.infra.database.sessions import acquire_org_async_session
from src.service.integrations.calendar.normalizer import CalendarEventNormalizer
from src.service.meeting_bot.recording_window import is_within_recording_dispatch_window
from src.workers.deal_worker.client import send_deal_task_async

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackfillCandidate:
    """One calendar row whose raw provider payload contains a supported join URL."""

    event_id: UUID
    title: str | None
    start_at: datetime
    provider: str | None
    conference_link: str
    online_meeting_provider: str


@dataclass(frozen=True)
class BackfillSummary:
    """Operator-facing result for one org backfill run."""

    scanned: int = 0
    matched: int = 0
    updated: int = 0
    queued: int = 0
    queue_failed: int = 0
    failed_event_ids: tuple[str, ...] = ()
    samples: tuple[dict[str, str | None], ...] = ()


@dataclass(frozen=True)
class QueueDispatchResult:
    """Queueing outcome kept separate from DB updates for retry visibility."""

    queued: int = 0
    queue_failed: int = 0
    failed_event_ids: tuple[str, ...] = ()


def extract_join_link_update(
    provider: str | None,
    payload: dict[str, Any],
) -> dict[str, str] | None:
    """Reuse the sync normalizer's supported-link extraction for stored payloads."""

    return CalendarEventNormalizer.extract_join_link_update(provider, payload)


def format_sample(candidate: BackfillCandidate) -> dict[str, str | None]:
    """Return bounded review fields so operators can inspect hosts before apply."""

    parsed = urlparse(candidate.conference_link)
    return {
        "event_id": str(candidate.event_id),
        "title": candidate.title,
        "start_at": candidate.start_at.isoformat(),
        "provider": candidate.provider,
        "conference_host": parsed.hostname,
        "online_meeting_provider": candidate.online_meeting_provider,
    }


def queued_event_ids_for_updated_rows(
    updated_candidates: list[BackfillCandidate],
    now: datetime,
) -> list[UUID]:
    """Select updated rows that are close enough to schedule a recording task."""

    return [
        candidate.event_id
        for candidate in updated_candidates
        if is_within_recording_dispatch_window(candidate.start_at, now)
    ]


async def queue_recording_dispatches(
    organisation_id: UUID,
    event_ids: list[UUID],
    *,
    send_task: Callable[..., Awaitable[str]] = send_deal_task_async,
) -> QueueDispatchResult:
    """Queue post-commit meeting-bot dispatches and report per-event failures."""

    queued = 0
    failed_event_ids: list[str] = []
    for event_id in event_ids:
        try:
            await send_task(
                "deal.send_meeting_recording",
                kwargs={
                    "organisation_id": str(organisation_id),
                    "calendar_event_id": str(event_id),
                },
                queue="deal",
                routing_key="deal",
            )
            queued += 1
        except Exception:
            failed_event_ids.append(str(event_id))
            logger.exception(
                "Failed to queue meeting recording dispatch during join-link backfill",
                extra={
                    "event": "meeting_join_link_backfill_queue_failed",
                    "organisation_id": str(organisation_id),
                    "calendar_event_id": str(event_id),
                },
            )

    return QueueDispatchResult(
        queued=queued,
        queue_failed=len(failed_event_ids),
        failed_event_ids=tuple(failed_event_ids),
    )


async def backfill_org(
    organisation_id: UUID,
    *,
    apply_changes: bool = False,
    queue_recordings: bool = False,
    days_ahead: int = 30,
    limit: int = 500,
    event_id: UUID | None = None,
    sample_limit: int = 10,
) -> BackfillSummary:
    """Backfill supported join links for one org without queueing before commit."""

    now = datetime.now(UTC)
    window_end = now + timedelta(days=days_ahead)
    scanned = 0
    matched_candidates: list[BackfillCandidate] = []
    updated_candidates: list[BackfillCandidate] = []
    queued_event_ids: list[UUID] = []
    samples: tuple[dict[str, str | None], ...] = ()

    async for session in acquire_org_async_session(organisation_id):
        params = {
            "organisation_id": organisation_id,
            "now": now,
            "window_end": window_end,
            "event_id": event_id,
            "limit": limit,
        }
        base_query = """
            select event_id, title, provider, data_payload, start_at
            from calendar_events
            where organisation_id = :organisation_id
              and conference_link is null
              and start_at > :now
              and start_at < :window_end
              and coalesce(status, '') != 'cancelled'
        """
        if event_id:
            base_query += " and event_id = :event_id"
        base_query += " order by start_at asc limit :limit"

        rows = (await session.execute(text(base_query), params)).mappings().all()
        scanned = len(rows)

        for row in rows:
            payload = row["data_payload"] or {}
            if not isinstance(payload, dict):
                continue
            update = extract_join_link_update(row["provider"], payload)
            if update is None:
                continue

            candidate = BackfillCandidate(
                event_id=row["event_id"],
                title=row["title"],
                start_at=row["start_at"],
                provider=row["provider"],
                conference_link=update["conference_link"],
                online_meeting_provider=update["online_meeting_provider"],
            )
            matched_candidates.append(candidate)

            if not apply_changes:
                continue

            result = await session.execute(
                text(
                    """
                    update calendar_events
                    set conference_link = :conference_link,
                        online_meeting_provider = :online_meeting_provider,
                        updated_at = now()
                    where organisation_id = :organisation_id
                      and event_id = :event_id
                      and conference_link is null
                    """
                ),
                {
                    "organisation_id": organisation_id,
                    "event_id": candidate.event_id,
                    "conference_link": candidate.conference_link,
                    "online_meeting_provider": candidate.online_meeting_provider,
                },
            )
            if result.rowcount == 1:
                updated_candidates.append(candidate)

        if apply_changes:
            await session.commit()

        if apply_changes and queue_recordings:
            queued_event_ids = queued_event_ids_for_updated_rows(updated_candidates, now)

        sample_source = updated_candidates if apply_changes else matched_candidates
        samples = tuple(format_sample(candidate) for candidate in sample_source[:sample_limit])
        break

    queue_result = await queue_recording_dispatches(organisation_id, queued_event_ids)

    return BackfillSummary(
        scanned=scanned,
        matched=len(matched_candidates),
        updated=len(updated_candidates),
        queued=queue_result.queued,
        queue_failed=queue_result.queue_failed,
        failed_event_ids=queue_result.failed_event_ids,
        samples=samples,
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--organisation-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--queue-recordings", action="store_true")
    parser.add_argument("--days-ahead", type=int, default=30)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--event-id")
    parser.add_argument("--sample-limit", type=int, default=10)
    args = parser.parse_args()

    summary = await backfill_org(
        UUID(args.organisation_id),
        apply_changes=args.apply,
        queue_recordings=args.queue_recordings,
        days_ahead=args.days_ahead,
        limit=args.limit,
        event_id=UUID(args.event_id) if args.event_id else None,
        sample_limit=args.sample_limit,
    )
    print(json.dumps(asdict(summary), default=str))


if __name__ == "__main__":
    asyncio.run(main())
