"""Reconcile calendar user-state rows that no longer exist in the provider snapshot.

Dry run is the default. The script fetches a fresh bounded calendar snapshot for
one organisation, compares it with active local user-state rows for each target
calendar integration, and writes an operator-review JSON report. Use --execute
only after reviewing the report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlmodel import col

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.domain.models.organisation_tables.sync import (  # noqa: E402
    IntegrationSyncState,
    IntegrationType,
    KnowledgeIntegration,
)
from src.domain.models.organisation_tables.sync.calendar import (  # noqa: E402
    CalendarEvent,
    CalendarEventUserState,
)
from src.infra.database.sessions import acquire_org_async_session  # noqa: E402
from src.service.integrations.calendar.normalizer import CalendarEventNormalizer  # noqa: E402
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo  # noqa: E402
from src.workers.integrations_worker.providers.calendar import (  # noqa: E402
    google_calendar,
    outlook_calendar,
)

CALENDAR_TYPES = {IntegrationType.GOOGLE_CALENDAR, IntegrationType.OUTLOOK_CALENDAR}
DEFAULT_PAST_DAYS = 90
DEFAULT_FUTURE_DAYS = 365
DEFAULT_SAMPLE_LIMIT = 25


@dataclass(frozen=True)
class LocalCalendarState:
    event_id: UUID
    state_id: UUID
    integration_id: UUID
    user_external_id: str
    series_external_id: str | None
    external_id: str
    occurrence_anchor: str
    title: str | None
    start_at: datetime
    status: str | None


@dataclass(frozen=True)
class ProviderSnapshot:
    user_external_ids: frozenset[str]
    canonical_keys: frozenset[tuple[str, str]]
    series_occurrence_keys: frozenset[tuple[str, str]]
    active_count: int
    cancelled_count: int
    skipped_without_identity: int


@dataclass
class IntegrationReport:
    organisation_id: UUID
    integration_id: UUID
    integration_type: str
    capability: str
    provider_event_count: int
    provider_active_count: int
    local_active_count: int
    candidate_count: int
    applied_count: int = 0


@dataclass(frozen=True)
class TargetCalendarIntegration:
    integration: KnowledgeIntegration
    capability: IntegrationType


def _provider_for_capability(capability: IntegrationType | str) -> str:
    if isinstance(capability, str):
        capability = IntegrationType(capability)
    if capability == IntegrationType.GOOGLE_CALENDAR:
        return "google"
    if capability == IntegrationType.OUTLOOK_CALENDAR:
        return "outlook"
    raise ValueError(f"Unsupported calendar capability: {capability}")


def _integration_type_value(integration_type: IntegrationType | str) -> str:
    if isinstance(integration_type, IntegrationType):
        return integration_type.value
    return integration_type


def _event_is_removed(event_data: dict[str, Any]) -> bool:
    metadata = event_data.get("_sync_metadata")
    return "@removed" in event_data or (
        isinstance(metadata, dict) and metadata.get("sync_action") == "DELETED"
    )


def _series_occurrence_anchor(normalized_start_at: datetime | None) -> str:
    if normalized_start_at is None:
        return ""
    value = normalized_start_at.astimezone(UTC) if normalized_start_at.tzinfo else normalized_start_at
    return value.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


def build_provider_snapshot(events: list[dict[str, Any]], provider: str) -> ProviderSnapshot:
    normalizer = CalendarEventNormalizer()
    user_external_ids: set[str] = set()
    canonical_keys: set[tuple[str, str]] = set()
    series_occurrence_keys: set[tuple[str, str]] = set()
    active_count = 0
    cancelled_count = 0
    skipped_without_identity = 0

    for event_data in events:
        normalized = normalizer.normalize(event_data, provider)
        if _event_is_removed(event_data) or normalized.is_cancelled:
            cancelled_count += 1
            continue

        has_identity = False
        if normalized.user_external_id:
            user_external_ids.add(normalized.user_external_id)
            has_identity = True
        if normalized.identity.external_id:
            canonical_keys.add(
                (
                    normalized.identity.external_id,
                    normalized.identity.occurrence_anchor,
                )
            )
            has_identity = True
        if normalized.series_external_id:
            anchor = normalized.identity.occurrence_anchor or _series_occurrence_anchor(
                normalized.fields.get("start_at")
            )
            series_occurrence_keys.add((normalized.series_external_id, anchor))
            has_identity = True

        if has_identity:
            active_count += 1
        else:
            skipped_without_identity += 1

    return ProviderSnapshot(
        user_external_ids=frozenset(user_external_ids),
        canonical_keys=frozenset(canonical_keys),
        series_occurrence_keys=frozenset(series_occurrence_keys),
        active_count=active_count,
        cancelled_count=cancelled_count,
        skipped_without_identity=skipped_without_identity,
    )


def _local_series_occurrence_key(row: LocalCalendarState) -> tuple[str, str] | None:
    if not row.series_external_id:
        return None
    anchor = row.occurrence_anchor or _series_occurrence_anchor(row.start_at)
    return row.series_external_id, anchor


def find_missing_local_states(
    local_rows: list[LocalCalendarState],
    provider_snapshot: ProviderSnapshot,
) -> list[LocalCalendarState]:
    missing: list[LocalCalendarState] = []
    for row in local_rows:
        if row.user_external_id in provider_snapshot.user_external_ids:
            continue
        if (row.external_id, row.occurrence_anchor) in provider_snapshot.canonical_keys:
            continue
        series_key = _local_series_occurrence_key(row)
        if series_key and series_key in provider_snapshot.series_occurrence_keys:
            continue
        missing.append(row)
    return missing


def format_candidate(candidate: LocalCalendarState) -> dict[str, Any]:
    return {
        "event_id": str(candidate.event_id),
        "state_id": str(candidate.state_id),
        "integration_id": str(candidate.integration_id),
        "user_external_id": candidate.user_external_id,
        "series_external_id": candidate.series_external_id,
        "external_id": candidate.external_id,
        "occurrence_anchor": candidate.occurrence_anchor,
        "title": candidate.title,
        "start_at": candidate.start_at.isoformat(),
        "status": candidate.status,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find and optionally soft-delete calendar user states missing from provider snapshots.",
    )
    parser.add_argument(
        "--org-id",
        action="append",
        required=True,
        help="Organisation UUID to process. Can be passed multiple times.",
    )
    parser.add_argument(
        "--integration-id",
        action="append",
        help="Specific calendar integration UUID. Defaults to all active calendar integrations in the org.",
    )
    parser.add_argument("--past-days", type=int, default=DEFAULT_PAST_DAYS)
    parser.add_argument("--future-days", type=int, default=DEFAULT_FUTURE_DAYS)
    parser.add_argument("--sample-limit", type=int, default=DEFAULT_SAMPLE_LIMIT)
    parser.add_argument(
        "--output-dir",
        default="calendar-reconciliation-reports",
        help="Directory for JSON reports.",
    )
    parser.add_argument("--execute", action="store_true", help="Persist soft-deletes.")
    return parser.parse_args()


async def _load_integrations(
    organisation_id: UUID,
    integration_ids: set[UUID] | None,
) -> list[TargetCalendarIntegration]:
    targets: list[TargetCalendarIntegration] = []
    async for session in acquire_org_async_session(organisation_id):
        target_by_id: dict[UUID, TargetCalendarIntegration] = {}

        conditions = [
            col(KnowledgeIntegration.integration_type).in_(tuple(t.value for t in CALENDAR_TYPES)),
            col(KnowledgeIntegration.is_active).is_(True),
            col(KnowledgeIntegration.connection_id).isnot(None),
        ]
        if integration_ids:
            conditions.append(col(KnowledgeIntegration.integration_id).in_(tuple(integration_ids)))
        stmt = select(KnowledgeIntegration).where(*conditions)
        result = await session.execute(stmt)
        for integration in result.scalars().all():
            integration_type = integration.integration_type
            if isinstance(integration_type, str):
                integration_type = IntegrationType(integration_type)
            target_by_id[integration.integration_id] = TargetCalendarIntegration(
                integration=integration,
                capability=integration_type,
            )

        # Bundled Google/Microsoft connections keep OAuth on provider-parent
        # integrations and enable calendar through IntegrationSyncState.
        parent_conditions = [
            col(KnowledgeIntegration.integration_id) == col(IntegrationSyncState.integration_id),
            col(IntegrationSyncState.capability).in_(tuple(t.value for t in CALENDAR_TYPES)),
            col(IntegrationSyncState.enabled).is_(True),
            col(KnowledgeIntegration.is_active).is_(True),
            col(KnowledgeIntegration.connection_id).isnot(None),
        ]
        if integration_ids:
            parent_conditions.append(
                col(KnowledgeIntegration.integration_id).in_(tuple(integration_ids))
            )
        parent_stmt = select(KnowledgeIntegration, IntegrationSyncState.capability).where(
            *parent_conditions
        )
        parent_result = await session.execute(parent_stmt)
        for integration, capability in parent_result.all():
            if isinstance(capability, str):
                capability = IntegrationType(capability)
            target_by_id[integration.integration_id] = TargetCalendarIntegration(
                integration=integration,
                capability=capability,
            )

        targets = list(target_by_id.values())
        break
    return targets


async def _fetch_provider_events(
    organisation_id: UUID,
    target: TargetCalendarIntegration,
) -> list[dict[str, Any]]:
    integration = target.integration
    integration_type = integration.integration_type
    if isinstance(integration_type, str):
        integration_type = IntegrationType(integration_type)
    if not integration.connection_id:
        raise RuntimeError(f"Integration {integration.integration_id} has no connection_id")

    token_info = IntegrationTokenInfo.from_integration(integration, organisation_id)
    if target.capability == IntegrationType.GOOGLE_CALENDAR:
        result = await google_calendar.fetch_events_with_cursor(
            token_info=token_info,
            connection_id=integration.connection_id,
            is_initial=True,
            last_synced_at=None,
            sync_token=None,
        )
        return result.events
    if target.capability == IntegrationType.OUTLOOK_CALENDAR:
        result = await outlook_calendar.fetch_events(
            token_info=token_info,
            connection_id=integration.connection_id,
            is_initial=True,
            last_synced_at=None,
            delta_link=None,
        )
        return result.events
    raise ValueError(f"Unsupported calendar capability: {target.capability}")


async def _load_local_active_states(
    organisation_id: UUID,
    integration_id: UUID,
    window_start: datetime,
    window_end: datetime,
) -> list[LocalCalendarState]:
    rows: list[LocalCalendarState] = []
    async for session in acquire_org_async_session(organisation_id):
        stmt = (
            select(CalendarEvent, CalendarEventUserState)
            .join(
                CalendarEventUserState,
                col(CalendarEventUserState.event_id) == col(CalendarEvent.event_id),
            )
            .where(
                col(CalendarEvent.organisation_id) == organisation_id,
                col(CalendarEventUserState.integration_id) == integration_id,
                col(CalendarEventUserState.is_deleted).is_(False),
                col(CalendarEvent.start_at) >= window_start,
                col(CalendarEvent.start_at) <= window_end,
            )
            .order_by(col(CalendarEvent.start_at).asc())
        )
        result = await session.execute(stmt)
        for event, state in result.all():
            rows.append(
                LocalCalendarState(
                    event_id=event.event_id,
                    state_id=state.state_id,
                    integration_id=state.integration_id,
                    user_external_id=state.external_id,
                    series_external_id=state.series_external_id,
                    external_id=event.external_id,
                    occurrence_anchor=event.occurrence_anchor,
                    title=event.title,
                    start_at=event.start_at,
                    status=event.status,
                )
            )
        break
    return rows


async def _event_has_active_user_states(session: Any, event_id: UUID) -> bool:
    stmt = select(func.count()).select_from(CalendarEventUserState).where(
        col(CalendarEventUserState.event_id) == event_id,
        col(CalendarEventUserState.is_deleted).is_(False),
    )
    result = await session.execute(stmt)
    return int(result.scalar_one()) > 0


async def _apply_soft_deletes(
    organisation_id: UUID,
    candidates: list[LocalCalendarState],
) -> int:
    if not candidates:
        return 0

    now = datetime.now(UTC)
    applied_count = 0
    async for session in acquire_org_async_session(organisation_id):
        state_ids = [candidate.state_id for candidate in candidates]
        await session.execute(
            update(CalendarEventUserState)
            .where(col(CalendarEventUserState.state_id).in_(state_ids))
            .values(is_deleted=True, updated_at=now)
        )
        await session.flush()

        for event_id in {candidate.event_id for candidate in candidates}:
            if await _event_has_active_user_states(session, event_id):
                continue
            await session.execute(
                update(CalendarEvent)
                .where(col(CalendarEvent.event_id) == event_id)
                .values(status="cancelled", synced_at=now)
            )

        await session.commit()
        applied_count = len(candidates)
        break
    return applied_count


def _write_report(
    output_dir: Path,
    *,
    report: IntegrationReport,
    candidates: list[LocalCalendarState],
    sample_limit: int,
    execute: bool,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = output_dir / (
        f"calendar_deleted_reconciliation_{report.organisation_id}_{report.integration_id}_{timestamp}.json"
    )
    payload = {
        "mode": "execute" if execute else "dry_run",
        "organisation_id": str(report.organisation_id),
        "integration_id": str(report.integration_id),
        "integration_type": report.integration_type,
        "capability": report.capability,
        "provider_event_count": report.provider_event_count,
        "provider_active_count": report.provider_active_count,
        "local_active_count": report.local_active_count,
        "candidate_count": report.candidate_count,
        "applied_count": report.applied_count,
        "sample_limit": sample_limit,
        "candidates_sample": [
            format_candidate(candidate) for candidate in candidates[:sample_limit]
        ],
        "candidate_state_ids": [str(candidate.state_id) for candidate in candidates],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


async def _process_integration(
    organisation_id: UUID,
    target: TargetCalendarIntegration,
    *,
    window_start: datetime,
    window_end: datetime,
    output_dir: Path,
    sample_limit: int,
    execute: bool,
) -> IntegrationReport:
    integration = target.integration
    provider = _provider_for_capability(target.capability)
    provider_events = await _fetch_provider_events(organisation_id, target)
    snapshot = build_provider_snapshot(provider_events, provider)
    local_rows = await _load_local_active_states(
        organisation_id,
        integration.integration_id,
        window_start,
        window_end,
    )
    candidates = find_missing_local_states(local_rows, snapshot)

    report = IntegrationReport(
        organisation_id=organisation_id,
        integration_id=integration.integration_id,
        integration_type=_integration_type_value(integration.integration_type),
        capability=target.capability.value,
        provider_event_count=len(provider_events),
        provider_active_count=snapshot.active_count,
        local_active_count=len(local_rows),
        candidate_count=len(candidates),
    )
    if execute:
        report.applied_count = await _apply_soft_deletes(organisation_id, candidates)

    report_path = _write_report(
        output_dir,
        report=report,
        candidates=candidates,
        sample_limit=sample_limit,
        execute=execute,
    )
    print(
        "  "
        f"{integration.integration_id} [{report.integration_type}/{report.capability}]: "
        f"provider={len(provider_events)} active_provider={snapshot.active_count} "
        f"local_active={len(local_rows)} candidates={len(candidates)} "
        f"applied={report.applied_count} report={report_path}"
    )
    if snapshot.cancelled_count or snapshot.skipped_without_identity:
        print(
            "    "
            f"ignored_cancelled_or_removed={snapshot.cancelled_count} "
            f"skipped_without_identity={snapshot.skipped_without_identity}"
        )
    return report


async def main() -> None:
    args = _parse_args()
    org_ids = [UUID(value) for value in args.org_id]
    integration_ids = (
        {UUID(value) for value in args.integration_id} if args.integration_id else None
    )
    output_dir = Path(args.output_dir)
    now = datetime.now(UTC)
    window_start = now - timedelta(days=args.past_days)
    window_end = now + timedelta(days=args.future_days)
    mode = "EXECUTE" if args.execute else "DRY RUN"

    print("=" * 90)
    print(f"  CALENDAR DELETED USER-STATE RECONCILIATION - {mode}")
    print(f"  Window: {window_start.isoformat()} -> {window_end.isoformat()}")
    print("=" * 90)

    reports: list[IntegrationReport] = []
    for org_id in org_ids:
        print(f"\n--- Org: {org_id} ---")
        integrations = await _load_integrations(org_id, integration_ids)
        if not integrations:
            print("  No matching active calendar integrations.")
            continue
        for target in integrations:
            reports.append(
                await _process_integration(
                    org_id,
                    target,
                    window_start=window_start,
                    window_end=window_end,
                    output_dir=output_dir,
                    sample_limit=args.sample_limit,
                    execute=args.execute,
                )
            )

    print("\n" + "=" * 90)
    print(
        "TOTAL: "
        f"{len(org_ids)} org(s), {len(reports)} integration(s), "
        f"{sum(report.candidate_count for report in reports)} candidate deletion(s), "
        f"{sum(report.applied_count for report in reports)} applied"
    )
    if not args.execute:
        print("DRY RUN - no database rows were changed. Review JSON reports before --execute.")
    print("=" * 90)


if __name__ == "__main__":
    asyncio.run(main())
