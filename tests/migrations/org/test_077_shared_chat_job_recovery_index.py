"""Regression contract for the shared chat-job recovery index migration."""

from pathlib import Path

MIGRATION_PATH = Path(
    "migrations/org_migrations/versions/"
    "3c7f79637722_077_shared_chat_job_recovery_index.py"
)


def test_recovery_index_uses_the_org_migration_managed_transaction() -> None:
    """Keep index replacement compatible with the outer transaction in env.py."""
    migration = MIGRATION_PATH.read_text(encoding="utf-8")

    # Org migrations run inside connection.begin(), so Alembic has no internal
    # transaction for autocommit_block() to commit during a startup deployment.
    assert "with op.get_context().autocommit_block()" not in migration
    assert "concurrently" not in migration.lower()
    assert "create index if not exists ix_chat_message_jobs_recovery" in migration.lower()
    assert "drop index if exists ix_chat_message_jobs_whatsapp_recovery" in migration.lower()
