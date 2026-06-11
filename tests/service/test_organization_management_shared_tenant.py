from __future__ import annotations

from contextlib import contextmanager

import pytest
from src.service.orgnisation import organization_management as org_management_module
from src.service.orgnisation.organization_management import OrganizationManagementService


class RecordingSession:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def begin(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
        return False

    def execute(self, statement):
        self.statements.append(str(statement))


def test_new_org_provisioning_grants_tenant_runtime_access_after_migrations(monkeypatch) -> None:
    calls: list[str] = []
    service = OrganizationManagementService()

    monkeypatch.setattr(
        service,
        "_create_database_role_and_schema",
        lambda *args: calls.append("schema"),
    )
    monkeypatch.setattr(
        service,
        "_run_organization_migrations",
        lambda *args: calls.append("migrations"),
    )
    monkeypatch.setattr(
        service,
        "_grant_tenant_runtime_role_access",
        lambda *args: calls.append("grants"),
    )

    service._provision_tenant_database("org_123", "org_123_role", "password")

    assert calls == ["schema", "migrations", "grants"]


def test_new_org_runtime_grant_skips_when_runtime_role_not_configured(monkeypatch) -> None:
    service = OrganizationManagementService()
    calls: list[str] = []

    monkeypatch.setattr(
        org_management_module.database_config,
        "TENANT_DATABASE_USER",
        "",
        raising=False,
    )
    monkeypatch.setattr(
        org_management_module,
        "TenantRuntimeGrantService",
        lambda *args: calls.append("service"),
        raising=False,
    )

    service._grant_tenant_runtime_role_access("org_123", "org_123_role")

    assert calls == []


def test_new_org_runtime_grants_existing_and_future_schema_objects(monkeypatch) -> None:
    recording_session = RecordingSession()

    @contextmanager
    def fake_master_session_scope():
        yield recording_session

    monkeypatch.setattr(
        org_management_module.database_config,
        "TENANT_DATABASE_USER",
        "app_tenant_runtime",
        raising=False,
    )
    monkeypatch.setattr(
        org_management_module.database_config,
        "TENANT_DATABASE_PASSWORD",
        "",
        raising=False,
    )
    monkeypatch.setattr(
        org_management_module.database_config,
        "TENANT_DATABASE_PASSWORD_SECRET_NAME",
        "shared-tenant-db-password",
        raising=False,
    )
    monkeypatch.setattr(
        org_management_module.database_config,
        "DATABASE_USER",
        "migration_owner",
        raising=False,
    )
    monkeypatch.setattr(org_management_module, "master_session_scope", fake_master_session_scope)

    service = OrganizationManagementService()
    service._grant_tenant_runtime_role_access("org_123", "org_123_role")

    rendered_sql = "\n".join(recording_session.statements)
    assert 'GRANT USAGE ON SCHEMA "org_123" TO "app_tenant_runtime"' in rendered_sql
    assert (
        'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "org_123" '
        'TO "app_tenant_runtime"'
    ) in rendered_sql
    assert (
        'ALTER DEFAULT PRIVILEGES FOR ROLE "migration_owner" IN SCHEMA "org_123" '
        'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "app_tenant_runtime"'
    ) in rendered_sql
    assert (
        'ALTER DEFAULT PRIVILEGES FOR ROLE "org_123_role" IN SCHEMA "org_123" '
        'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "app_tenant_runtime"'
    ) in rendered_sql


def test_new_org_runtime_grant_rejects_master_database_user(monkeypatch) -> None:
    service = OrganizationManagementService()

    monkeypatch.setattr(
        org_management_module.database_config,
        "DATABASE_USER",
        "postgres",
        raising=False,
    )
    monkeypatch.setattr(
        org_management_module.database_config,
        "TENANT_DATABASE_USER",
        "postgres",
        raising=False,
    )
    monkeypatch.setattr(
        org_management_module.database_config,
        "TENANT_DATABASE_PASSWORD",
        "",
        raising=False,
    )
    monkeypatch.setattr(
        org_management_module.database_config,
        "TENANT_DATABASE_PASSWORD_SECRET_NAME",
        "shared-tenant-db-password",
        raising=False,
    )

    with pytest.raises(RuntimeError, match="must not equal DATABASE_USER"):
        service._grant_tenant_runtime_role_access("org_123", "org_123_role")
