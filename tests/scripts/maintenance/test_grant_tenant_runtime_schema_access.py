from __future__ import annotations

from contextlib import contextmanager
from uuid import UUID, uuid4

from scripts.maintenance import grant_tenant_runtime_schema_access as grant_module


class ScalarResult:
    def __init__(self, values: list[UUID]) -> None:
        self.values = values

    def all(self) -> list[UUID]:
        return self.values


class ExecuteResult:
    def __init__(self, values: list[UUID]) -> None:
        self.values = values

    def scalars(self) -> ScalarResult:
        return ScalarResult(self.values)


class RecordingSession:
    def __init__(self, values: list[UUID]) -> None:
        self.values = values
        self.statements: list[str] = []
        self.committed = False

    def execute(self, statement):
        self.statements.append(str(statement))
        return ExecuteResult(self.values)

    def commit(self) -> None:
        self.committed = True


def _configure_runtime(monkeypatch) -> None:
    monkeypatch.setattr(grant_module.database_config, "DATABASE_USER", "postgres", raising=False)
    monkeypatch.setattr(
        grant_module.database_config,
        "TENANT_DATABASE_USER",
        "app_tenant_runtime",
        raising=False,
    )
    monkeypatch.setattr(
        grant_module.database_config,
        "TENANT_DATABASE_PASSWORD",
        "",
        raising=False,
    )
    monkeypatch.setattr(
        grant_module.database_config,
        "TENANT_DATABASE_PASSWORD_SECRET_NAME",
        "shared-tenant-db-password",
        raising=False,
    )


def test_all_org_query_has_no_status_filter() -> None:
    org_ids = [uuid4(), uuid4()]
    session = RecordingSession(org_ids)

    resolved = grant_module.resolve_target_org_ids(session, all_orgs=True, org_id=None)

    assert resolved == org_ids
    rendered_sql = "\n".join(session.statements).lower()
    assert "organisations" in rendered_sql
    assert "status" not in rendered_sql


def test_dry_run_does_not_apply_grants(monkeypatch) -> None:
    org_id = uuid4()

    @contextmanager
    def fake_master_session_scope():
        yield RecordingSession([org_id])

    class FakeService:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        def grant_tenant_runtime_role_access(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("dry-run must not mutate")

    monkeypatch.setattr(grant_module, "master_session_scope", fake_master_session_scope)
    monkeypatch.setattr(grant_module, "TenantRuntimeGrantService", FakeService)
    _configure_runtime(monkeypatch)

    count = grant_module.grant_schema_access(all_orgs=True, org_id=None, execute=False)

    assert count == 1


def test_execute_applies_runtime_grants(monkeypatch) -> None:
    org_id = uuid4()
    session = RecordingSession([org_id])
    calls: list[dict[str, str]] = []

    @contextmanager
    def fake_master_session_scope():
        yield session

    class FakeService:
        def __init__(self, recording_session: RecordingSession) -> None:
            assert recording_session is session

        def grant_tenant_runtime_role_access(self, **kwargs) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(grant_module, "master_session_scope", fake_master_session_scope)
    monkeypatch.setattr(grant_module, "TenantRuntimeGrantService", FakeService)
    _configure_runtime(monkeypatch)

    count = grant_module.grant_schema_access(all_orgs=True, org_id=None, execute=True)

    assert count == 1
    assert calls == [
        {
            "schema_name": f"org_{str(org_id).replace('-', '_')}",
            "schema_owner_role": f"org_{str(org_id).replace('-', '_')}_role",
            "migration_owner_role": "postgres",
            "runtime_role": "app_tenant_runtime",
        }
    ]
    assert session.committed
