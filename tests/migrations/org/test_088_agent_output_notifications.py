"""Contract checks for extending the shared notification delivery ledger."""

from pathlib import Path

MIGRATION = Path(
    "migrations/org_migrations/versions/3c7f79637711_088_agent_output_notifications.py"
)


def test_migration_extends_notification_deliveries_without_creating_feature_table() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'down_revision: str | None = "3c7f79637712"' in source
    assert 'op.add_column(\n            "notification_deliveries"' in source
    assert "ix_notification_delivery_recovery" in source
    assert "ix_notification_delivery_stale_claim" in source
    assert "agent_run_deliveries" not in source
    assert 'op.drop_table("automation_triggers")' not in source
    assert 'op.drop_table("agent_runs")' not in source
    assert "status IN ('pending', 'running', 'succeeded', 'failed', " in source
