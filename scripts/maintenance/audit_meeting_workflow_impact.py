"""Inventory production users who may need meeting-workflow recovery.

This command is read-only. It scans every approved production organisation and
emits integration and Recall-session candidates without exposing credentials,
provider cursors, webhook secrets, or recording URLs. The recovery operation is
deliberately separate so operators can review the affected parties first.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlmodel import col

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.org_admin_session import acquire_org_admin_async_session  # noqa: E402
from src.domain.models.master_tables import (  # noqa: E402
    Organisation,
    OrganisationStatus,
    User,
)
from src.domain.models.organisation_tables.sync import (  # noqa: E402
    CalendarEvent,
    CalendarEventUserState,
    IntegrationSyncState,
    IntegrationType,
    KnowledgeIntegration,
    MeetingRecordingSession,
    MeetingRecordingStatus,
    MeetingTranscriptStatus,
)
from src.infra.database.sessions import acquire_master_async_session  # noqa: E402
from src.service.integrations.webhooks.google.metadata import (  # noqa: E402
    GoogleSubscriptionKind,
)
from src.service.integrations.webhooks.google.metadata import (  # noqa: E402
    get_subscription_metadata as get_google_subscription,
)
from src.service.integrations.webhooks.microsoft.metadata import (  # noqa: E402
    MicrosoftSubscriptionKind,
)
from src.service.integrations.webhooks.microsoft.metadata import (  # noqa: E402
    get_subscription_metadata as get_microsoft_subscription,
)

SUPPORTED_TYPES = {
    IntegrationType.GOOGLE,
    IntegrationType.MICROSOFT,
    IntegrationType.GOOGLE_CALENDAR,
    IntegrationType.OUTLOOK_CALENDAR,
    IntegrationType.GMAIL,
    IntegrationType.OUTLOOK_EMAIL,
}
TERMINAL_RECORDING_STATUSES = {
    MeetingRecordingStatus.COMPLETED.value,
    MeetingRecordingStatus.CANCELLED.value,
    MeetingRecordingStatus.FAILED.value,
}


@dataclass(frozen=True, slots=True)
class MasterTarget:
    """Approved production organisation plus its user-email lookup."""

    organisation_id: UUID
    organisation_name: str
    user_emails: dict[UUID, str]


@dataclass(frozen=True, slots=True)
class IntegrationImpact:
    """Safe, operator-facing health summary for one provider capability."""

    organisation_id: str
    organisation_name: str
    user_id: str
    user_email: str | None
    integration_id: str
    connected_email: str | None
    provider: str
    capability: str
    enabled: bool
    is_active: bool
    requires_reauth: bool
    last_synced_at: str | None
    last_sync_status: str | None
    consecutive_failures: int
    subscription_status: str
    recovery_class: str
    reason: str
    last_sync_error: str | None


@dataclass(frozen=True, slots=True)
class RecordingImpact:
    """Ended Recall session whose stored lifecycle or artifacts need review."""

    organisation_id: str
    organisation_name: str
    user_id: str | None
    user_email: str | None
    calendar_event_id: str
    session_id: str
    meeting_end_at: str
    recording_status: str
    transcript_status: str
    provider_session_present: bool
    recovery_class: str


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize legacy naive sync timestamps as UTC for audit comparisons."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _iso_utc(value: datetime | None) -> str | None:
    normalized = _as_utc(value)
    return normalized.isoformat() if normalized else None


def _subscription_status(
    capability: IntegrationType,
    metadata: dict[str, Any],
    *,
    now: datetime,
) -> str:
    """Classify stored webhook metadata without exporting its secret values."""
    subscription: Any
    if capability == IntegrationType.GOOGLE_CALENDAR:
        subscription = get_google_subscription(metadata, GoogleSubscriptionKind.CALENDAR)
    elif capability == IntegrationType.GMAIL:
        subscription = get_google_subscription(metadata, GoogleSubscriptionKind.GMAIL)
    elif capability == IntegrationType.OUTLOOK_CALENDAR:
        subscription = get_microsoft_subscription(metadata, MicrosoftSubscriptionKind.CALENDAR)
    elif capability == IntegrationType.OUTLOOK_EMAIL:
        subscription = get_microsoft_subscription(metadata, MicrosoftSubscriptionKind.EMAIL)
    else:
        return "unsupported"
    if subscription is None:
        return "missing_or_invalid"
    expires_at = _as_utc(subscription.expires_at)
    return "expired" if expires_at is None or expires_at <= now else "active"


def classify_integration_recovery(
    *,
    capability: IntegrationType,
    enabled: bool,
    is_active: bool,
    requires_reauth: bool,
    last_synced_at: datetime | None,
    last_sync_status: str | None,
    consecutive_failures: int,
    subscription_status: str,
    now: datetime,
) -> tuple[str, str]:
    """Choose the one recovery action an operator should take for a capability."""
    if requires_reauth:
        return "user_reconnection_required", "OAuth grant is revoked or expired"
    if not enabled or not is_active:
        return "no_action", "Integration or capability is intentionally inactive"
    if capability == IntegrationType.OUTLOOK_CALENDAR:
        return "full_calendar_repair", "Existing events may contain pre-fix timezone values"
    if subscription_status != "active":
        return "subscription_repair", f"Webhook subscription is {subscription_status}"

    synced_at = _as_utc(last_synced_at)
    stale = synced_at is None or synced_at <= now - timedelta(hours=72)
    unhealthy = bool(last_sync_status and last_sync_status != "success") or consecutive_failures > 0
    if stale or unhealthy:
        return "automatic_recovery", "One-time cursor-based catch-up should repair missed changes"
    return "no_action", "No stored sync-health indicator requires recovery"


async def _load_master_targets() -> list[MasterTarget]:
    """Load approved organisations and user emails from the shared public schema."""
    async for session in acquire_master_async_session():
        org_result = await session.execute(
            select(Organisation).where(
                col(Organisation.status) == OrganisationStatus.APPROVED,
                col(Organisation.is_active).is_(True),
            )
        )
        organisations = list(org_result.scalars().all())
        user_result = await session.execute(select(User))
        users = list(user_result.scalars().all())
        emails_by_org: dict[UUID, dict[UUID, str]] = {}
        for user in users:
            emails_by_org.setdefault(user.organisation_id, {})[user.user_id] = str(user.email)
        return [
            MasterTarget(
                organisation_id=org.organisation_id,
                organisation_name=org.organisation_name,
                user_emails=emails_by_org.get(org.organisation_id, {}),
            )
            for org in organisations
        ]
    return []


async def _audit_organisation(
    target: MasterTarget,
    *,
    now: datetime,
) -> tuple[list[IntegrationImpact], list[RecordingImpact]]:
    """Read integration and ended-recording health through one tenant boundary."""
    integration_impacts: list[IntegrationImpact] = []
    recording_impacts: list[RecordingImpact] = []
    async with acquire_org_admin_async_session(target.organisation_id) as session:
        integration_result = await session.execute(
            select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_type).in_(
                    tuple(value.value for value in SUPPORTED_TYPES)
                )
            )
        )
        integrations = list(integration_result.scalars().all())
        integration_by_id = {row.integration_id: row for row in integrations}

        state_result = await session.execute(
            select(IntegrationSyncState).where(
                col(IntegrationSyncState.capability).in_(
                    (
                        IntegrationType.GOOGLE_CALENDAR.value,
                        IntegrationType.GMAIL.value,
                        IntegrationType.OUTLOOK_CALENDAR.value,
                        IntegrationType.OUTLOOK_EMAIL.value,
                    )
                )
            )
        )
        states_by_integration: dict[UUID, list[IntegrationSyncState]] = {}
        for state in state_result.scalars().all():
            states_by_integration.setdefault(state.integration_id, []).append(state)

        for integration in integrations:
            integration_type = IntegrationType(_enum_value(integration.integration_type))
            states = states_by_integration.get(integration.integration_id, [])
            if integration_type in {IntegrationType.GOOGLE, IntegrationType.MICROSOFT}:
                if not states:
                    provider = (
                        "google" if integration_type == IntegrationType.GOOGLE else "microsoft"
                    )
                    integration_impacts.append(
                        IntegrationImpact(
                            organisation_id=str(target.organisation_id),
                            organisation_name=target.organisation_name,
                            user_id=str(integration.created_by_user_id),
                            user_email=target.user_emails.get(integration.created_by_user_id),
                            integration_id=str(integration.integration_id),
                            connected_email=integration.integration_email,
                            provider=provider,
                            capability="provider_parent",
                            enabled=True,
                            is_active=integration.is_active,
                            requires_reauth=integration.requires_reauth,
                            last_synced_at=_iso_utc(integration.last_synced_at),
                            last_sync_status=integration.last_sync_status,
                            consecutive_failures=integration.consecutive_sync_failures,
                            subscription_status="unknown",
                            recovery_class="configuration_review",
                            reason="Provider integration has no capability sync-state rows",
                            last_sync_error=(
                                str(integration.last_sync_error)[:250]
                                if integration.last_sync_error
                                else None
                            ),
                        )
                    )
                    continue
                capability_rows = [
                    (
                        IntegrationType(_enum_value(state.capability)),
                        state.enabled,
                        state.last_synced_at,
                        state.last_sync_status,
                        state.last_sync_error,
                        state.consecutive_sync_failures,
                        dict(state.webhook_metadata or {}),
                    )
                    for state in states
                ]
            else:
                capability_rows = [
                    (
                        integration_type,
                        True,
                        integration.last_synced_at,
                        integration.last_sync_status,
                        integration.last_sync_error,
                        integration.consecutive_sync_failures,
                        dict(integration.connection_metadata or {}),
                    )
                ]

            for (
                capability,
                enabled,
                last_synced_at,
                last_sync_status,
                last_sync_error,
                consecutive_failures,
                webhook_metadata,
            ) in capability_rows:
                subscription_status = _subscription_status(
                    capability,
                    webhook_metadata,
                    now=now,
                )
                recovery_class, reason = classify_integration_recovery(
                    capability=capability,
                    enabled=enabled,
                    is_active=integration.is_active,
                    requires_reauth=integration.requires_reauth,
                    last_synced_at=last_synced_at,
                    last_sync_status=last_sync_status,
                    consecutive_failures=consecutive_failures,
                    subscription_status=subscription_status,
                    now=now,
                )
                provider = (
                    "google"
                    if capability
                    in {
                        IntegrationType.GOOGLE_CALENDAR,
                        IntegrationType.GMAIL,
                    }
                    else "microsoft"
                )
                integration_impacts.append(
                    IntegrationImpact(
                        organisation_id=str(target.organisation_id),
                        organisation_name=target.organisation_name,
                        user_id=str(integration.created_by_user_id),
                        user_email=target.user_emails.get(integration.created_by_user_id),
                        integration_id=str(integration.integration_id),
                        connected_email=integration.integration_email,
                        provider=provider,
                        capability=capability.value,
                        enabled=enabled,
                        is_active=integration.is_active,
                        requires_reauth=integration.requires_reauth,
                        last_synced_at=_iso_utc(last_synced_at),
                        last_sync_status=last_sync_status,
                        consecutive_failures=consecutive_failures,
                        subscription_status=subscription_status,
                        recovery_class=recovery_class,
                        reason=reason,
                        last_sync_error=str(last_sync_error)[:250] if last_sync_error else None,
                    )
                )

        # Only ended sessions can be reconciled safely. Future and live meetings
        # remain under the normal bot lifecycle and are excluded from this audit.
        recording_result = await session.execute(
            select(
                MeetingRecordingSession,
                col(CalendarEvent.end_at),
                col(CalendarEventUserState.integration_id),
            )
            .join(
                CalendarEvent,
                and_(
                    col(CalendarEvent.event_id) == MeetingRecordingSession.calendar_event_id,
                    col(CalendarEvent.organisation_id) == target.organisation_id,
                ),
            )
            .outerjoin(
                CalendarEventUserState,
                and_(
                    col(CalendarEventUserState.event_id)
                    == MeetingRecordingSession.calendar_event_id,
                    col(CalendarEventUserState.is_deleted).is_(False),
                ),
            )
            .where(
                col(MeetingRecordingSession.organisation_id) == target.organisation_id,
                col(CalendarEvent.end_at) <= now - timedelta(minutes=15),
                or_(
                    col(MeetingRecordingSession.status).not_in(TERMINAL_RECORDING_STATUSES),
                    and_(
                        col(MeetingRecordingSession.status)
                        == MeetingRecordingStatus.COMPLETED.value,
                        col(MeetingRecordingSession.transcript_status).in_(
                            (
                                MeetingTranscriptStatus.NOT_REQUESTED.value,
                                MeetingTranscriptStatus.PENDING.value,
                            )
                        ),
                    ),
                ),
            )
        )
        seen_sessions: set[UUID] = set()
        for session_row, end_at, integration_id in recording_result.all():
            if session_row.session_id in seen_sessions:
                continue
            normalized_end_at = _as_utc(end_at)
            if normalized_end_at is None:
                continue
            seen_sessions.add(session_row.session_id)
            owner = integration_by_id.get(integration_id)
            user_id = owner.created_by_user_id if owner else None
            recording_impacts.append(
                RecordingImpact(
                    organisation_id=str(target.organisation_id),
                    organisation_name=target.organisation_name,
                    user_id=str(user_id) if user_id else None,
                    user_email=target.user_emails.get(user_id) if user_id else None,
                    calendar_event_id=str(session_row.calendar_event_id),
                    session_id=str(session_row.session_id),
                    meeting_end_at=normalized_end_at.isoformat(),
                    recording_status=_enum_value(session_row.status),
                    transcript_status=_enum_value(session_row.transcript_status),
                    provider_session_present=bool(session_row.provider_session_id),
                    recovery_class=(
                        "recording_reconciliation"
                        if session_row.provider_session_id
                        else "support_review"
                    ),
                )
            )

    return integration_impacts, recording_impacts


def _write_reports(
    output: Path,
    integrations: list[IntegrationImpact],
    recordings: list[RecordingImpact],
    organisation_failures: list[dict[str, str]],
    *,
    generated_at: datetime,
) -> None:
    """Write a complete JSON report plus two spreadsheet-friendly CSV files."""
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "environment": "production",
        "generated_at": generated_at.isoformat(),
        "summary": {
            "integration_capabilities": len(integrations),
            "integration_recovery_candidates": sum(
                row.recovery_class != "no_action" for row in integrations
            ),
            "recording_recovery_candidates": len(recordings),
        },
        "organisation_failures": organisation_failures,
        "integrations": [asdict(row) for row in integrations],
        "recordings": [asdict(row) for row in recordings],
    }
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    for suffix, rows in (("integrations", integrations), ("recordings", recordings)):
        csv_path = output.with_name(f"{output.stem}_{suffix}.csv")
        serialized = [asdict(row) for row in rows]
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            if serialized:
                writer = csv.DictWriter(handle, fieldnames=list(serialized[0]))
                writer.writeheader()
                writer.writerows(serialized)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("meeting_workflow_impact_production.json"),
        help="JSON report path; companion CSV files use the same prefix.",
    )
    args = parser.parse_args()

    # This inventory intentionally excludes dev. Requiring the production ENV
    # prevents a plausible-looking report from being generated against the wrong database.
    if os.getenv("ENV", "dev").lower() not in {"prod", "production"}:
        raise SystemExit("This audit is production-only; set ENV=prod in the approved runtime.")

    generated_at = datetime.now(UTC)
    integrations: list[IntegrationImpact] = []
    recordings: list[RecordingImpact] = []
    failures: list[dict[str, str]] = []
    for target in await _load_master_targets():
        try:
            org_integrations, org_recordings = await _audit_organisation(
                target,
                now=generated_at,
            )
            integrations.extend(org_integrations)
            recordings.extend(org_recordings)
        except Exception as exc:
            # A complete report names inaccessible tenants instead of silently
            # omitting them, while healthy organisations continue to be audited.
            failures.append(
                {
                    "organisation_id": str(target.organisation_id),
                    "organisation_name": target.organisation_name,
                    "error": str(exc)[:250],
                }
            )

    _write_reports(
        args.output,
        integrations,
        recordings,
        failures,
        generated_at=generated_at,
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "integration_capabilities": len(integrations),
                "recording_candidates": len(recordings),
                "organisation_failures": failures,
                "mutations": 0,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
