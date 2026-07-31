from __future__ import annotations

from pathlib import Path


def test_org_migrations_keep_pgvector_extension_schema_visible() -> None:
    env_py = Path("migrations/org_migrations/env.py").read_text(encoding="utf-8")

    assert "SET search_path TO {target_schema}, extensions, public" in env_py


def test_org_migrations_use_alembic_managed_transactions() -> None:
    """Later online index revisions require Alembic's autocommit boundary."""

    env_py = Path("migrations/org_migrations/env.py").read_text(encoding="utf-8")

    assert "with connectable.connect() as connection, connection.begin():" not in env_py
    assert "with context.begin_transaction():" in env_py
    assert "connection.commit()" in env_py
