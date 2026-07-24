"""Migration contract for durable automation-event activation dispatches."""

from pathlib import Path

MIGRATION_PATH = Path(
    "migrations/org_migrations/versions/3c7f79637719_080_automation_event_dispatches.py"
)


def test_080_adds_durable_automation_event_dispatches() -> None:
    """Keep activation retry state separate from immutable source events."""

    source = MIGRATION_PATH.read_text(encoding="utf-8").lower()

    assert 'revision: str = "3c7f79637719"' in source
    assert 'down_revision: str | none = "3c7f79637720"' in source
    assert '"automation_event_dispatches"' in source
    assert '"uq_automation_event_dispatch_source"' in source
    assert '"ix_automation_event_dispatch_pending"' in source
    assert '"processed_at"' in source
    assert '"failed_at"' in source
    assert '"attempt_count"' in source
