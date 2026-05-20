"""Tests for HubSpot CRM incremental cursor metadata helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.service.integrations.crm.records.hubspot.incremental_cursor_metadata import (
    get_hubspot_incremental_search_start,
    is_hubspot_incremental_ready,
    set_hubspot_incremental_cursor,
)


def test_get_hubspot_incremental_search_start_accepts_zulu_timestamp() -> None:
    """Regression test for incremental cursor search start accepting zulu timestamp."""

    metadata = {
        "record_sync": {
            "companies": {
                "incremental": {
                    "cursor_source_modified_at": "2026-05-18T10:06:43Z",
                    "cursor_updated_at": "2026-05-18T10:10:00Z",
                }
            }
        }
    }

    search_start = get_hubspot_incremental_search_start(metadata, "companies")

    assert search_start == datetime(2026, 5, 18, 9, 56, 43, tzinfo=UTC)


def test_get_hubspot_incremental_search_start_subtracts_lookback() -> None:
    """Regression test for get hubspot incremental search start subtracts lookback."""

    metadata = {
        "record_sync": {
            "deals": {
                "incremental": {
                    "cursor_source_modified_at": "2026-05-18T10:06:43+00:00",
                    "lookback_minutes": 10,
                }
            }
        }
    }

    search_start = get_hubspot_incremental_search_start(metadata, "deals")

    assert search_start == datetime(2026, 5, 18, 9, 56, 43, tzinfo=UTC)


def test_get_hubspot_incremental_search_start_uses_default_when_cursor_missing() -> None:
    """Regression test for get hubspot incremental search start uses default when cursor missing."""

    default_start = datetime(2026, 5, 18, 9, 0, tzinfo=UTC)

    search_start = get_hubspot_incremental_search_start(
        {},
        "deals",
        default_start=default_start,
    )

    assert search_start is default_start


def test_get_hubspot_incremental_search_start_bootstraps_from_last_synced_at() -> None:
    """Regression test for first incremental run using full-sync start time."""

    metadata = {
        "record_sync": {
            "tasks": {
                "last_synced_at": "2026-05-21T10:00:00+00:00",
            }
        }
    }

    search_start = get_hubspot_incremental_search_start(metadata, "tasks")

    assert search_start == datetime(2026, 5, 21, 9, 50, tzinfo=UTC)


def test_is_hubspot_incremental_ready_accepts_successful_initial_sync() -> None:
    """Regression test for readiness after the ordered initial sync finishes."""

    assert is_hubspot_incremental_ready(
        {"initial_sync_status": "success"},
        ("deals", "companies"),
    )


def test_is_hubspot_incremental_ready_rejects_partial_initial_sync_metadata() -> None:
    """Regression test for readiness rejecting incomplete object cursors."""

    metadata = {
        "initial_sync_status": "running",
        "record_sync": {
            "deals": {"last_synced_at": "2026-05-21T10:00:00+00:00"},
        },
    }

    assert not is_hubspot_incremental_ready(metadata, ("deals", "companies"))


def test_is_hubspot_incremental_ready_accepts_legacy_per_object_cursors() -> None:
    """Regression test for older metadata that predates initial_sync_status."""

    metadata = {
        "record_sync": {
            "deals": {"last_synced_at": "2026-05-21T10:00:00+00:00"},
            "companies": {
                "incremental": {
                    "cursor_source_modified_at": "2026-05-21T10:05:00+00:00",
                }
            },
        },
    }

    assert is_hubspot_incremental_ready(metadata, ("deals", "companies"))


def test_set_hubspot_incremental_cursor_preserves_existing_record_sync_fields() -> None:
    """Regression test for set hubspot incremental cursor preserves existing record sync fields."""

    original = {
        "record_sync": {
            "deals": {
                "status": "success",
                "last_synced_at": "2026-05-18T10:10:00+00:00",
                "deals_updated": 4,
            },
            "companies": {"status": "success"},
        },
        "hubspot_portal_id": "123",
    }
    cursor_source = datetime(2026, 5, 18, 10, 6, 43, tzinfo=UTC)
    cursor_updated = datetime(2026, 5, 18, 10, 10, tzinfo=UTC)

    updated = set_hubspot_incremental_cursor(
        original,
        "deals",
        cursor_source_modified_at=cursor_source,
        cursor_updated_at=cursor_updated,
        seen_count=38,
        written_count=20,
        skipped_stale_count=18,
    )

    assert updated is not original
    assert original["record_sync"]["deals"].get("incremental") is None
    assert updated["hubspot_portal_id"] == "123"
    assert updated["record_sync"]["companies"] == {"status": "success"}
    assert updated["record_sync"]["deals"]["status"] == "success"
    assert updated["record_sync"]["deals"]["deals_updated"] == 4
    assert updated["record_sync"]["deals"]["incremental"] == {
        "cursor_source_modified_at": "2026-05-18T10:06:43+00:00",
        "cursor_updated_at": "2026-05-18T10:10:00+00:00",
        "lookback_minutes": 10,
        "last_run_type": "incremental_search",
        "last_seen_count": 38,
        "last_written_count": 20,
        "last_skipped_stale_count": 18,
    }


def test_set_hubspot_incremental_cursor_does_not_advance_source_cursor_when_empty() -> None:
    """Regression test for set hubspot incremental cursor does not advance source cursor when empty."""

    original = {
        "record_sync": {
            "deals": {
                "incremental": {
                    "cursor_source_modified_at": "2026-05-18T10:06:43+00:00",
                    "cursor_updated_at": "2026-05-18T10:10:00+00:00",
                    "lookback_minutes": 10,
                    "last_seen_count": 38,
                }
            }
        }
    }
    cursor_updated = datetime(2026, 5, 18, 10, 15, tzinfo=UTC)

    updated = set_hubspot_incremental_cursor(
        original,
        "deals",
        cursor_source_modified_at=None,
        cursor_updated_at=cursor_updated,
        seen_count=0,
        written_count=0,
        skipped_stale_count=0,
    )

    incremental = updated["record_sync"]["deals"]["incremental"]
    assert incremental["cursor_source_modified_at"] == "2026-05-18T10:06:43+00:00"
    assert incremental["cursor_updated_at"] == "2026-05-18T10:15:00+00:00"
    assert incremental["last_seen_count"] == 0


def test_set_hubspot_incremental_cursor_uses_custom_lookback() -> None:
    """Regression test for set hubspot incremental cursor uses custom lookback."""

    updated = set_hubspot_incremental_cursor(
        {},
        "contacts",
        cursor_source_modified_at=datetime(2026, 5, 18, 10, tzinfo=UTC),
        cursor_updated_at=datetime(2026, 5, 18, 10, 5, tzinfo=UTC),
        seen_count=2,
        written_count=1,
        skipped_stale_count=1,
        lookback_minutes=15,
    )

    search_start = get_hubspot_incremental_search_start(updated, "contacts")

    assert search_start == datetime(2026, 5, 18, 9, 45, tzinfo=UTC)
    assert search_start == datetime(2026, 5, 18, 10, tzinfo=UTC) - timedelta(minutes=15)
