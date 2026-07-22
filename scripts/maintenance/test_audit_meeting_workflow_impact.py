"""Contract tests for production meeting-impact recovery classification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from scripts.maintenance.audit_meeting_workflow_impact import classify_integration_recovery
from src.domain.models.organisation_tables.sync import IntegrationType

NOW = datetime(2026, 7, 22, tzinfo=UTC)


def test_outlook_calendar_always_gets_bounded_full_repair() -> None:
    """Unchanged pre-fix events require re-normalization, not a delta-only pull."""
    recovery_class, _reason = classify_integration_recovery(
        capability=IntegrationType.OUTLOOK_CALENDAR,
        enabled=True,
        is_active=True,
        requires_reauth=False,
        last_synced_at=NOW,
        last_sync_status="success",
        consecutive_failures=0,
        subscription_status="active",
        now=NOW,
    )

    assert recovery_class == "full_calendar_repair"


def test_revoked_grant_requires_user_instead_of_automatic_retry() -> None:
    """No recovery job can repair a provider grant that the user revoked."""
    recovery_class, _reason = classify_integration_recovery(
        capability=IntegrationType.GMAIL,
        enabled=True,
        is_active=False,
        requires_reauth=True,
        last_synced_at=NOW - timedelta(days=3),
        last_sync_status="auth_failed",
        consecutive_failures=3,
        subscription_status="expired",
        now=NOW,
    )

    assert recovery_class == "user_reconnection_required"


def test_disabled_capability_is_not_reenabled() -> None:
    """The audit must preserve an intentional disconnect even when state is stale."""
    recovery_class, _reason = classify_integration_recovery(
        capability=IntegrationType.GOOGLE_CALENDAR,
        enabled=False,
        is_active=True,
        requires_reauth=False,
        last_synced_at=None,
        last_sync_status=None,
        consecutive_failures=0,
        subscription_status="missing_or_invalid",
        now=NOW,
    )

    assert recovery_class == "no_action"
