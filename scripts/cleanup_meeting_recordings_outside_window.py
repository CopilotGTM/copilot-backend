"""Cancel and remove meeting-recording rows outside the supported bot window.

The default mode is a read-only dry run. `--apply` first deletes the matching
Recall bot, then deletes the local org-scoped row only when Recall cancellation
succeeds or the bot is already absent.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from dotenv import load_dotenv
from sqlalchemy import delete, select
from sqlmodel import col
from src.domain.models.organisation_tables.sync.meeting_recording import (
    MeetingRecordingProvider,
    MeetingRecordingSession,
)
from src.infra.config.configs import recall_config
from src.infra.database.sessions import acquire_org_async_session
from src.service.integrations.meeting_recording.providers import (
    RecallMeetingRecordingProvider,
    RecallProviderError,
)
from src.service.meeting_bot.recording_window import recording_window_end

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("cleanup_meeting_recordings_outside_window")


@dataclass(frozen=True)
class CleanupCandidate:
    """One Recall-backed local row that is safe to consider for cleanup."""

    session_id: UUID
    organisation_id: UUID
    calendar_event_id: UUID
    provider_session_id: str
    join_at: datetime
    status: str | None


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser with dry-run as the default safety mode."""
    parser = argparse.ArgumentParser(
        description="Cancel Recall bots and delete local rows outside the 3-day window.",
    )
    parser.add_argument("--organisation-id", required=True, type=UUID)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--limit", type=int, default=None)
    return parser


def should_delete_after_recall_cancel(cancel_status: str) -> bool:
    """Return whether local deletion can follow the Recall cancellation result."""
    return cancel_status in {"cancelled", "already_missing"}


async def load_candidates(
    organisation_id: UUID,
    *,
    limit: int | None,
) -> list[CleanupCandidate]:
    """Load local rows outside the dispatch window without touching Recall."""
    cutoff = recording_window_end(datetime.now(UTC))
    async for session in acquire_org_async_session(organisation_id):
        stmt = (
            select(MeetingRecordingSession)
            .where(
                col(MeetingRecordingSession.organisation_id) == organisation_id,
                col(MeetingRecordingSession.provider) == MeetingRecordingProvider.RECALL_AI,
                col(MeetingRecordingSession.join_at) >= cutoff,
                col(MeetingRecordingSession.provider_session_id).is_not(None),
                col(MeetingRecordingSession.sent_to_provider_at).is_not(None),
            )
            .order_by(col(MeetingRecordingSession.join_at), col(MeetingRecordingSession.session_id))
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = (await session.execute(stmt)).scalars().all()

    return [
        CleanupCandidate(
            session_id=row.session_id,
            organisation_id=row.organisation_id,
            calendar_event_id=row.calendar_event_id,
            provider_session_id=str(row.provider_session_id),
            join_at=row.join_at,
            status=getattr(row.status, "value", str(row.status)) if row.status else None,
        )
        for row in rows
        if row.provider_session_id
    ]


async def build_provider() -> RecallMeetingRecordingProvider:
    """Create the existing Recall provider client from deployed configuration."""
    api_key = await recall_config.get_api_key()
    if not api_key:
        raise RuntimeError("Recall API key is not configured")
    return RecallMeetingRecordingProvider(
        api_key=api_key,
        region=recall_config.RECALL_REGION,
        timeout=recall_config.RECALL_TIMEOUT_SECONDS,
    )


async def cancel_recall_bot(
    provider: RecallMeetingRecordingProvider,
    candidate: CleanupCandidate,
) -> str:
    """Cancel one Recall bot and map provider outcomes to cleanup decisions."""
    try:
        await provider.cancel_session(candidate.provider_session_id)
    except RecallProviderError as exc:
        if exc.status_code == 404:
            return "already_missing"
        logger.error(
            "Recall cancellation failed",
            extra={
                "organisation_id": str(candidate.organisation_id),
                "session_id": str(candidate.session_id),
                "provider_session_id": candidate.provider_session_id,
                "status_code": exc.status_code,
                "detail": exc.detail,
            },
        )
        return "failed"
    return "cancelled"


async def delete_local_rows(
    organisation_id: UUID,
    candidates: list[CleanupCandidate],
    *,
    batch_size: int,
) -> int:
    """Delete only rows whose provider sessions were already handled in Recall."""
    deleted = 0
    for start in range(0, len(candidates), batch_size):
        batch = candidates[start : start + batch_size]
        async for session in acquire_org_async_session(organisation_id):
            stmt = delete(MeetingRecordingSession).where(
                col(MeetingRecordingSession.organisation_id) == organisation_id,
                col(MeetingRecordingSession.session_id).in_(
                    [candidate.session_id for candidate in batch]
                ),
                col(MeetingRecordingSession.provider_session_id).in_(
                    [candidate.provider_session_id for candidate in batch]
                ),
            )
            result = await session.execute(stmt)
            await session.commit()
            deleted += getattr(result, "rowcount", 0) or 0
    return deleted


def print_candidates(candidates: list[CleanupCandidate]) -> None:
    """Print a bounded preview so dry runs are reviewable in terminals and pod logs."""
    print(f"Found {len(candidates)} recording session(s) outside the dispatch window.")
    for candidate in candidates[:20]:
        print(
            "  "
            f"join_at={candidate.join_at} "
            f"session_id={candidate.session_id} "
            f"event_id={candidate.calendar_event_id} "
            f"provider_session_id={candidate.provider_session_id} "
            f"status={candidate.status or '(unknown)'}"
        )
    if len(candidates) > 20:
        print(f"  ... {len(candidates) - 20} more")


async def run(args: argparse.Namespace) -> dict[str, int]:
    """Execute dry-run or apply mode for one org-scoped cleanup pass."""
    candidates = await load_candidates(args.organisation_id, limit=args.limit)
    print_candidates(candidates)
    if not args.apply or not candidates:
        if not args.apply:
            print("Dry run only. Re-run with --apply to cancel Recall bots and delete local rows.")
        return {"matched": len(candidates), "cancelled": 0, "already_missing": 0, "failed": 0, "deleted": 0}

    provider = await build_provider()
    safe_to_delete: list[CleanupCandidate] = []
    counts = {"matched": len(candidates), "cancelled": 0, "already_missing": 0, "failed": 0}
    for candidate in candidates:
        cancel_status = await cancel_recall_bot(provider, candidate)
        counts[cancel_status] += 1
        if should_delete_after_recall_cancel(cancel_status):
            safe_to_delete.append(candidate)

    deleted = await delete_local_rows(
        args.organisation_id,
        safe_to_delete,
        batch_size=args.batch_size,
    )
    return {**counts, "deleted": deleted}


def main() -> None:
    args = build_parser().parse_args()
    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be greater than zero")
    result = asyncio.run(run(args))
    print(
        "Result: "
        f"matched={result['matched']} "
        f"cancelled={result['cancelled']} "
        f"already_missing={result['already_missing']} "
        f"failed={result['failed']} "
        f"deleted={result['deleted']}"
    )


if __name__ == "__main__":
    main()
