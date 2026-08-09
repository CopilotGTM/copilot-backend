"""Contract checks for agent-owned output delivery persistence."""

from pathlib import Path

MIGRATION = Path(
    "migrations/org_migrations/versions/3c7f79637711_088_agent_output_notifications.py"
)


def test_migration_adds_agent_owned_delivery_without_trigger_compatibility() -> None:
    """Keep output delivery on agents and runs without restoring trigger state."""

    source = MIGRATION.read_text(encoding="utf-8")

    assert 'down_revision: str | None = "3c7f79637712"' in source
    assert '"user_agents",\n        sa.Column(\n            "output_delivery_config"' in source
    assert '"user_agents",\n        sa.Column(\n            "output_delivery_updated_at"' in source
    assert '"agent_runs",\n        sa.Column(\n            "output_delivery_config"' in source
    assert '"delivery_config"' not in source
    assert 'op.add_column(\n            "notification_deliveries"' in source
    assert "ix_notification_delivery_recovery" in source
    assert "ix_notification_delivery_stale_claim" in source
    assert "agent_run_deliveries" not in source
    assert 'op.drop_table("automation_triggers")' not in source
    assert 'op.drop_table("agent_runs")' not in source
    assert "status IN ('pending', 'running', 'succeeded', 'failed', " in source


def test_downgrade_accepts_the_previously_applied_088_shape() -> None:
    """Allow Dev's old 088 schema to roll back before the edited revision is replayed."""

    source = MIGRATION.read_text(encoding="utf-8")

    for table_name, column_name in (
        ("agent_runs", "output_delivery_config"),
        ("user_agents", "output_delivery_updated_at"),
        ("user_agents", "output_delivery_config"),
    ):
        assert (
            f'op.drop_column("{table_name}", "{column_name}", if_exists=True)'
            in source
        )
