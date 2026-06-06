from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import scripts.run_org_migrations as run_org_migrations


class _FakeResult:
    def __init__(self, organizations):
        self._organizations = organizations

    def all(self):
        return self._organizations


class _FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, *_args, **_kwargs):
        return SimpleNamespace(fetchone=lambda: (1,))


class _FakeEngine:
    def connect(self):
        return _FakeConnection()


class _FakeSession:
    def __init__(self, _engine):
        self.closed = False

    def exec(self, _stmt):
        return _FakeResult([SimpleNamespace(organisation_id=uuid4())])

    def close(self):
        self.closed = True


def test_run_org_migrations_returns_false_when_any_org_fails(monkeypatch):
    monkeypatch.setattr(
        run_org_migrations, "create_engine", lambda *_args, **_kwargs: _FakeEngine()
    )
    monkeypatch.setattr(run_org_migrations, "Session", _FakeSession)
    monkeypatch.setattr(run_org_migrations, "get_schema_name", lambda _org_id: "org_test")
    monkeypatch.setattr(
        run_org_migrations.migration_manager,
        "run_organization_migration",
        lambda **_kwargs: SimpleNamespace(success=False, error="boom", duration=0.1),
    )

    success = run_org_migrations.run_migrations_for_orgs(
        org_ids=[],
        target_version="head",
        migrate_all=True,
    )

    assert success is False


def test_drop_crm_provider_columns_uses_existing_association_type_id_column():
    migration_path = (
        Path("migrations/org_migrations/versions") / "3c7f79637742_057_drop_crm_provider_columns.py"
    )
    migration_text = migration_path.read_text(encoding="utf-8")

    assert "association_type_id" in migration_text
    assert "association_type,\n            role_label" not in migration_text
