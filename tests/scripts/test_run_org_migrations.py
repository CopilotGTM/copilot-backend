import inspect
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import scripts.run_org_migrations as run_org_migrations
from migrations.migration_manager import ProductionMigrationManager
from sqlalchemy.pool import NullPool

from migrations import migration_manager as migration_manager_module


class _FakeResult:
    def __init__(self, organizations):
        self._organizations = organizations

    def all(self):
        return self._organizations

    def one_or_none(self):
        return self._organizations[0] if self._organizations else None


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


class _FakeSessionWithOrg:
    def __init__(self, _engine):
        self.closed = False

    def exec(self, _stmt):
        return _FakeResult(
            [SimpleNamespace(organisation_id=UUID("6fc45305-2989-44ae-9cc1-a55f39450a9e"))]
        )

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


def test_org_lookup_session_closes_before_alembic_runs(monkeypatch):
    """Concurrent DDL must not wait on the runner's own master snapshot."""

    sessions = []

    class _TrackingSession(_FakeSessionWithOrg):
        def __init__(self, engine):
            super().__init__(engine)
            sessions.append(self)

    def _run_migration(**_kwargs):
        assert sessions[0].closed is True
        return SimpleNamespace(success=True, error=None, duration=0.1)

    monkeypatch.setattr(
        run_org_migrations, "create_engine", lambda *_args, **_kwargs: _FakeEngine()
    )
    monkeypatch.setattr(run_org_migrations, "Session", _TrackingSession)
    monkeypatch.setattr(run_org_migrations, "get_schema_name", lambda _org_id: "org_test")
    monkeypatch.setattr(
        run_org_migrations.migration_manager,
        "run_organization_migration",
        _run_migration,
    )

    assert run_org_migrations.run_migrations_for_orgs(
        org_ids=["6fc45305-2989-44ae-9cc1-a55f39450a9e"],
        target_version="head",
    )


def test_all_org_migrations_skip_hello5_but_explicit_run_includes_it(monkeypatch):
    migrated_orgs = []

    monkeypatch.setattr(
        run_org_migrations, "create_engine", lambda *_args, **_kwargs: _FakeEngine()
    )
    monkeypatch.setattr(run_org_migrations, "Session", _FakeSessionWithOrg)
    monkeypatch.setattr(run_org_migrations, "get_schema_name", lambda _org_id: "org_hello5")
    monkeypatch.setattr(
        run_org_migrations.migration_manager,
        "run_organization_migration",
        lambda **kwargs: migrated_orgs.append(kwargs["schema_name"])
        or SimpleNamespace(success=True, error=None, duration=0.1),
    )

    all_success = run_org_migrations.run_migrations_for_orgs(
        org_ids=[],
        target_version="head",
        migrate_all=True,
    )
    explicit_success = run_org_migrations.run_migrations_for_orgs(
        org_ids=["6fc45305-2989-44ae-9cc1-a55f39450a9e"],
        target_version="head",
        migrate_all=False,
    )

    assert all_success is True
    assert explicit_success is True
    assert migrated_orgs == ["org_hello5"]


def test_drop_crm_provider_columns_uses_existing_association_type_id_column():
    migration_path = (
        Path("migrations/org_migrations/versions") / "3c7f79637742_057_drop_crm_provider_columns.py"
    )
    migration_text = migration_path.read_text(encoding="utf-8")

    assert "association_type_id" in migration_text
    assert "association_type,\n            role_label" not in migration_text


def test_migration_manager_uses_short_lived_master_connections(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        "migrations.migration_manager.create_engine",
        lambda _url, **kwargs: captured.setdefault("kwargs", kwargs) or object(),
    )

    ProductionMigrationManager()

    assert captured["kwargs"]["poolclass"] is NullPool
    assert "pool_size" not in captured["kwargs"]
    assert "max_overflow" not in captured["kwargs"]


def test_migration_manager_passes_only_schema_to_alembic(monkeypatch):
    """Structural migrations never receive organisation row identity."""

    captured: dict[str, object] = {}
    manager = ProductionMigrationManager.__new__(ProductionMigrationManager)

    monkeypatch.setattr(manager, "_validate_schema_exists", lambda _schema: True)
    monkeypatch.setattr(manager, "_get_current_revision", lambda _schema: None)
    monkeypatch.setattr(manager, "_get_head_revision", lambda: "head-revision")
    monkeypatch.setattr(
        migration_manager_module.command,
        "upgrade",
        lambda config, revision: captured.update(
            x_args=config.cmd_opts.x,
            revision=revision,
        ),
    )

    result = manager.run_organization_migration(
        schema_name="org_11111111_1111_4111_8111_111111111111",
    )

    assert result.success is True
    assert captured["x_args"] == ["schema=org_11111111_1111_4111_8111_111111111111"]
    assert "organisation_id" not in inspect.signature(manager.run_organization_migration).parameters


def test_migration_manager_passes_only_schema_for_tenant_data(monkeypatch):
    """The permanent shared schema migration receives only its schema name."""

    captured: dict[str, object] = {}
    manager = ProductionMigrationManager.__new__(ProductionMigrationManager)

    monkeypatch.setattr(manager, "_validate_schema_exists", lambda _schema: True)
    monkeypatch.setattr(manager, "_get_current_revision", lambda _schema: None)
    monkeypatch.setattr(manager, "_get_head_revision", lambda: "head-revision")
    monkeypatch.setattr(
        migration_manager_module.command,
        "upgrade",
        lambda config, revision: captured.update(
            x_args=config.cmd_opts.x,
            revision=revision,
        ),
    )

    result = manager.run_organization_migration(schema_name="tenant_data")

    assert result.success is True
    assert captured["x_args"] == ["schema=tenant_data"]


def test_explicit_schema_path_runs_existing_migration_manager(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        run_org_migrations.migration_manager,
        "run_organization_migration",
        lambda **kwargs: captured.update(kwargs)
        or SimpleNamespace(success=True, error=None, duration=0.1),
    )

    success = run_org_migrations.run_migration_for_schema(
        schema_name="tenant_data",
        target_version="head",
    )

    assert success is True
    assert captured == {
        "schema_name": "tenant_data",
        "target_revision": "head",
        "validate_schema": True,
    }


@pytest.mark.parametrize("schema_name", ["tenant_data_test", "feature_123"])
def test_explicit_schema_path_accepts_safe_test_schema_names(monkeypatch, schema_name):
    captured = {}
    monkeypatch.setattr(
        run_org_migrations.migration_manager,
        "run_organization_migration",
        lambda **kwargs: captured.update(kwargs)
        or SimpleNamespace(success=True, error=None, duration=0.1),
    )

    assert run_org_migrations.run_migration_for_schema(
        schema_name=schema_name,
        target_version="head",
    )
    assert captured["schema_name"] == schema_name
    assert "organisation_id" not in captured


@pytest.mark.parametrize(
    "schema_name",
    ["tenant-data", "tenant_data; DROP SCHEMA public", "public.tenant_data", ""],
)
def test_explicit_schema_path_rejects_unsafe_identifiers(schema_name):
    with pytest.raises(ValueError, match="schema"):
        run_org_migrations.run_migration_for_schema(
            schema_name=schema_name,
            target_version="head",
        )


def test_explicit_schema_cannot_mix_with_legacy_target_selection():
    with pytest.raises(ValueError, match="cannot be combined"):
        run_org_migrations.validate_cli_targets(
            schema_name="tenant_data",
            org_ids=["11111111-1111-4111-8111-111111111111"],
            migrate_all=False,
            list_orgs=False,
        )
