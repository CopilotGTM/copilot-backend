from __future__ import annotations

from pathlib import Path


def test_org_migrations_keep_pgvector_extension_schema_visible() -> None:
    env_py = Path("migrations/org_migrations/env.py").read_text(encoding="utf-8")

    assert "SET search_path TO {target_schema}, extensions, public" in env_py
