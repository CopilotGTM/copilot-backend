"""Tests for the Gmail master-route backfill planner."""

import importlib.util

from scripts.maintenance.backfill_gmail_master_routes import (
    GmailRoute,
    MasterRoute,
    build_plan,
    normalize_email,
)


def test_backfill_gmail_master_routes_script_exists() -> None:
    """The rollout must include a read-only planner before routing switches."""
    assert importlib.util.find_spec("scripts.maintenance.backfill_gmail_master_routes") is not None


def test_normalize_email_handles_whitespace_and_blank_values() -> None:
    """Normalize valid mailbox keys and reject empty values."""
    assert normalize_email(" Seller@Example.com ") == "seller@example.com"
    assert normalize_email("  ") is None
    assert normalize_email(None) is None


def test_build_plan_classifies_safe_updates_and_blockers() -> None:
    """Separate safe updates from records that require operator judgment."""
    routes = [
        GmailRoute("routable", "org-1", "integration-1", "One@Example.com"),
        GmailRoute("update", "org-2", "integration-2", "Two@Example.com"),
        GmailRoute("blank", "org-3", "integration-3", None),
        GmailRoute("missing", "org-4", "integration-4", "four@example.com"),
        GmailRoute("mismatch", "org-5", "integration-5", "five@example.com"),
    ]
    mappings = {
        "routable": MasterRoute("routable", "org-1", "integration-1", "one@example.com"),
        "update": MasterRoute("update", "org-2", "integration-2", None),
        "blank": MasterRoute("blank", "org-3", "integration-3", None),
        "mismatch": MasterRoute("mismatch", "another-org", "integration-5", None),
    }

    assert [item.status for item in build_plan(routes, mappings)] == [
        "routable",
        "would_update",
        "blank_email",
        "missing_mapping",
        "mismatched_mapping",
    ]
