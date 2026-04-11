"""Clear stale occurrence identity fields that are not supported by payload data.

Why this exists:
- Valid Google recurring rows should have `originalStartTime` in payload.
- Valid Outlook recurring rows should have `originalStart` in payload.
- If a row still has `occurrence_anchor` / `original_start_at` but its payload
  does not expose any recurring-anchor field, those values are stale and can be
  cleared safely.

Dry run is the default. Use `--execute` to persist changes.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import or_, select
from sqlmodel import col

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.domain.models.master_tables import Organisation, OrganisationStatus  # noqa: E402
from src.domain.models.organisation_tables.sync.calendar import CalendarEvent  # noqa: E402
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session  # noqa: E402
from src.infra.utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)


@dataclass
class RunStats:
    orgs_processed: int = 0
    rows_considered: int = 0
    rows_to_clear: int = 0
    rows_updated: int = 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clear stale occurrence identity fields from calendar_events.",
    )
    parser.add_argument(
        "--org-id",
        action="append",
        help="Specific organisation UUID(s). Defaults to all approved organisations.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Persist the cleanup. Default is dry run.",
    )
    return parser.parse_args()


async def _get_target_org_ids(raw_ids: list[str] | None) -> list[UUID]:
    if raw_ids:
        return [UUID(value) for value in raw_ids]

    async for session in acquire_master_async_session():
        stmt = select(col(Organisation.organisation_id)).where(
            col(Organisation.status) == OrganisationStatus.APPROVED,
        )
        result = await session.execute(stmt)
        return [row[0] for row in result.all()]
    return []


def _payload_has_supported_anchor(event: CalendarEvent) -> bool:
    payload = event.data_payload if isinstance(event.data_payload, dict) else None
    if not payload:
        return False

    google_original = payload.get("originalStartTime")
    if isinstance(google_original, dict) and (
        isinstance(google_original.get("dateTime"), str)
        or isinstance(google_original.get("date"), str)
    ):
        return True

    outlook_original = payload.get("originalStart")
    if isinstance(outlook_original, str) and outlook_original:
        return True

    return False


def _should_clear(event: CalendarEvent) -> bool:
    has_anchor = bool(event.occurrence_anchor)
    has_original_start = event.original_start_at is not None
    if not has_anchor and not has_original_start:
        return False
    return not _payload_has_supported_anchor(event)


async def _process_org(organisation_id: UUID, execute: bool, stats: RunStats) -> None:
    async for session in acquire_org_async_session(organisation_id):
        stmt = select(CalendarEvent).where(
            or_(
                col(CalendarEvent.occurrence_anchor) != "",
                col(CalendarEvent.original_start_at).is_not(None),
            )
        )
        result = await session.execute(stmt)
        events = result.scalars().all()

        stats.orgs_processed += 1
        stats.rows_considered += len(events)

        to_clear = [event for event in events if _should_clear(event)]
        stats.rows_to_clear += len(to_clear)

        print(f"\n--- Org: {organisation_id} ---")
        print(f"  candidate rows: {len(events)}")
        print(f"  stale rows to clear: {len(to_clear)}")

        for event in to_clear[:10]:
            print(
                "  sample: "
                f"event_id={event.event_id} provider={event.provider or '<null>'} "
                f"external_id={event.external_id} anchor={event.occurrence_anchor or '<empty>'} "
                f"original_start_at={event.original_start_at}"
            )

        if len(to_clear) > 10:
            print(f"  ... {len(to_clear) - 10} more row(s)")

        if not execute or not to_clear:
            continue

        for event in to_clear:
            event.occurrence_anchor = ""
            event.original_start_at = None
            session.add(event)

        await session.commit()
        stats.rows_updated += len(to_clear)
        print(f"  updated rows: {len(to_clear)}")


async def _main() -> None:
    args = _parse_args()
    org_ids = await _get_target_org_ids(args.org_id)
    stats = RunStats()

    mode = "EXECUTE" if args.execute else "DRY RUN"
    print("=" * 90)
    print(f"  CLEAR NON-GOOGLE CALENDAR OCCURRENCE FIELDS - {mode}")
    print("=" * 90)
    print(f"Processing {len(org_ids)} organisation(s)")

    for org_id in org_ids:
        await _process_org(org_id, args.execute, stats)

    print("\n" + "=" * 90)
    print(
        "TOTAL: "
        f"{stats.orgs_processed} org(s), "
        f"{stats.rows_considered} candidate row(s), "
        f"{stats.rows_to_clear} row(s) to clear, "
        f"{stats.rows_updated} row(s) updated"
    )
    if not args.execute:
        print("DRY RUN - no changes made. Run with --execute to apply.")
    print("=" * 90)


if __name__ == "__main__":
    asyncio.run(_main())
