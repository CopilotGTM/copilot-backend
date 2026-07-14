"""Tests for the Gmail master-route backfill planner."""

import importlib.util

from scripts.maintenance.backfill_gmail_master_routes import (
    GmailRoute,
    MasterRoute,
    _route_query,
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


def test_all_integration_scope_does_not_require_gmail_state() -> None:
    """All-provider backfills must include active connected parent integrations."""
    sql = str(_route_query(all_integrations=True))

    assert "JOIN integration_sync_states" not in sql
    assert "ki.is_active IS TRUE" in sql
    assert "ki.connection_id IS NOT NULL" in sql


def test_default_scope_remains_gmail_only() -> None:
    """The existing default must retain its Gmail capability guard."""
    sql = str(_route_query(all_integrations=False))

    assert "JOIN integration_sync_states" in sql
    assert "state.capability = 'gmail'" in sql
