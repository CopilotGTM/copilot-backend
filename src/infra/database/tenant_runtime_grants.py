from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session
from src.infra.database.tenant_search_path import TRUSTED_EXTENSION_SCHEMA


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


class TenantRuntimeGrantService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def grant_tenant_runtime_role_access(
        self,
        *,
        schema_name: str,
        schema_owner_role: str,
        migration_owner_role: str,
        runtime_role: str,
    ) -> None:
        schema_sql = quote_identifier(schema_name)
        runtime_sql = quote_identifier(runtime_role)
        extension_schema_sql = quote_identifier(TRUSTED_EXTENSION_SCHEMA)
        schema_owner_sql = quote_identifier(schema_owner_role)
        owner_roles = [migration_owner_role]
        if schema_owner_role != migration_owner_role:
            owner_roles.append(schema_owner_role)

        statements = [
            f"GRANT USAGE ON SCHEMA {schema_sql} TO {runtime_sql}",
            (
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema_sql} "
                f"TO {runtime_sql}"
            ),
            (
                f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA {schema_sql} "
                f"TO {runtime_sql}"
            ),
            f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA {schema_sql} TO {runtime_sql}",
            f"GRANT USAGE ON SCHEMA {extension_schema_sql} TO {schema_owner_sql}",
        ]

        for owner_role in owner_roles:
            owner_sql = quote_identifier(owner_role)
            statements.extend(
                [
                    (
                        f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_sql} "
                        f"IN SCHEMA {schema_sql} GRANT SELECT, INSERT, UPDATE, DELETE "
                        f"ON TABLES TO {runtime_sql}"
                    ),
                    (
                        f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_sql} "
                        f"IN SCHEMA {schema_sql} GRANT USAGE, SELECT, UPDATE "
                        f"ON SEQUENCES TO {runtime_sql}"
                    ),
                    (
                        f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_sql} "
                        f"IN SCHEMA {schema_sql} GRANT EXECUTE ON FUNCTIONS TO {runtime_sql}"
                    ),
                ]
            )

        for statement in statements:
            self.session.execute(text(statement))
