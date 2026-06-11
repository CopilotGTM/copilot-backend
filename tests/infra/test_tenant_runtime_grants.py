from __future__ import annotations

from src.infra.database.tenant_runtime_grants import TenantRuntimeGrantService


class RecordingSession:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement):
        self.statements.append(str(statement))


def test_grant_service_emits_least_privilege_schema_sql() -> None:
    session = RecordingSession()
    service = TenantRuntimeGrantService(session)

    service.grant_tenant_runtime_role_access(
        schema_name="org_123",
        schema_owner_role="org_123_role",
        migration_owner_role="postgres",
        runtime_role="app_tenant_runtime",
    )

    rendered_sql = "\n".join(session.statements)
    assert 'GRANT USAGE ON SCHEMA "org_123" TO "app_tenant_runtime"' in rendered_sql
    assert (
        'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "org_123" '
        'TO "app_tenant_runtime"'
    ) in rendered_sql
    assert (
        'GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA "org_123" TO "app_tenant_runtime"'
    ) in rendered_sql
    assert (
        'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA "org_123" TO "app_tenant_runtime"' in rendered_sql
    )
    assert 'GRANT USAGE ON SCHEMA "extensions" TO "org_123_role"' in rendered_sql
    assert (
        'ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "org_123" '
        'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "app_tenant_runtime"'
    ) in rendered_sql
    assert (
        'ALTER DEFAULT PRIVILEGES FOR ROLE "org_123_role" IN SCHEMA "org_123" '
        'GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO "app_tenant_runtime"'
    ) in rendered_sql
    assert "OWNER TO" not in rendered_sql
    assert "ON SCHEMA public" not in rendered_sql
