"""Real-PostgreSQL proof for the forced tenant RLS convention.

The test runs only with explicit disposable admin/runtime URLs. It exercises
the real non-owner runtime role and one reused pooled connection, so SQL-shape
unit tests cannot accidentally stand in for PostgreSQL enforcement.
"""

# ruff: noqa: S608

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from migrations.org_migrations.tenant_policy import (
    install_tenant_rls,
    protect_tenant_table,
)
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import QueuePool
from src.infra.database.tenant_runtime_readiness import (
    TenantRuntimeReadinessError,
    validate_tenant_runtime_catalog,
)

ADMIN_URL = os.getenv("TENANT_RLS_TEST_ADMIN_URL")
RUNTIME_URL = os.getenv("TENANT_RLS_TEST_RUNTIME_URL")
pytestmark = pytest.mark.skipif(
    not ADMIN_URL or not RUNTIME_URL,
    reason="set TENANT_RLS_TEST_ADMIN_URL and TENANT_RLS_TEST_RUNTIME_URL",
)

ORG_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ORG_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
PARENT_A = UUID("11111111-1111-4111-8111-111111111111")
PARENT_B = UUID("22222222-2222-4222-8222-222222222222")


def _set_tenant(connection, organisation_id: UUID) -> None:
    connection.execute(
        text(
            "SELECT set_config("
            "'app.current_organisation_id', :organisation_id, true)"
        ),
        {"organisation_id": str(organisation_id)},
    )


def test_runtime_role_is_fail_closed_and_pool_safe() -> None:
    """Prove tenant reads, writes, relationships, roles, and pooling fail closed."""

    assert ADMIN_URL is not None
    assert RUNTIME_URL is not None
    schema = f"rls_isolation_{uuid4().hex[:12]}"
    admin = create_engine(ADMIN_URL)
    runtime = create_engine(
        RUNTIME_URL,
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
    )

    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(
                text(
                    f"""
                    CREATE TABLE "{schema}".parents (
                        organisation_id uuid NOT NULL,
                        parent_id uuid NOT NULL,
                        payload text NOT NULL,
                        PRIMARY KEY (organisation_id, parent_id)
                    )
                    """
                )
            )
            connection.execute(
                text(
                    f"""
                    CREATE TABLE "{schema}".children (
                        organisation_id uuid NOT NULL,
                        child_id uuid NOT NULL,
                        parent_id uuid NOT NULL,
                        PRIMARY KEY (organisation_id, child_id),
                        FOREIGN KEY (organisation_id, parent_id)
                            REFERENCES "{schema}".parents (
                                organisation_id,
                                parent_id
                            )
                    )
                    """
                )
            )
            install_tenant_rls(
                connection,
                schema_name=schema,
                runtime_role="app_tenant_runtime",
            )
            connection.execute(
                text(
                    f'INSERT INTO "{schema}".parents '
                    "(organisation_id, parent_id, payload) VALUES "
                    "(:org_a, :parent_a, 'A'), (:org_b, :parent_b, 'B')"
                ),
                {
                    "org_a": str(ORG_A),
                    "parent_a": str(PARENT_A),
                    "org_b": str(ORG_B),
                    "parent_b": str(PARENT_B),
                },
            )

        with runtime.begin() as connection:
            backend_pid = connection.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()
            assert connection.execute(
                text(f'SELECT count(*) FROM "{schema}".parents')
            ).scalar_one() == 0

        with pytest.raises(DBAPIError), runtime.begin() as connection:
            connection.execute(
                text(
                    f'INSERT INTO "{schema}".parents '
                    "(organisation_id, parent_id, payload) "
                    "VALUES (:organisation_id, :parent_id, 'missing-context')"
                ),
                {
                    "organisation_id": str(ORG_A),
                    "parent_id": str(uuid4()),
                },
            )

        with runtime.begin() as connection:
            _set_tenant(connection, ORG_A)
            assert connection.execute(
                text(f'SELECT payload FROM "{schema}".parents')
            ).scalars().all() == ["A"]
            assert (
                connection.execute(
                    text(
                        f'UPDATE "{schema}".parents SET payload = \'hidden\' '
                        "WHERE organisation_id = :organisation_id"
                    ),
                    {"organisation_id": str(ORG_B)},
                ).rowcount
                == 0
            )
            assert (
                connection.execute(
                    text(
                        f'DELETE FROM "{schema}".parents '
                        "WHERE organisation_id = :organisation_id"
                    ),
                    {"organisation_id": str(ORG_B)},
                ).rowcount
                == 0
            )

        with pytest.raises(DBAPIError), runtime.begin() as connection:
            _set_tenant(connection, ORG_A)
            connection.execute(
                text(
                    f'INSERT INTO "{schema}".parents '
                    "(organisation_id, parent_id, payload) "
                    "VALUES (:organisation_id, :parent_id, 'B-via-A')"
                ),
                {
                    "organisation_id": str(ORG_B),
                    "parent_id": str(uuid4()),
                },
            )

        with pytest.raises(DBAPIError), runtime.begin() as connection:
            _set_tenant(connection, ORG_A)
            connection.execute(
                text(
                    f'UPDATE "{schema}".parents '
                    "SET organisation_id = :org_b "
                    "WHERE organisation_id = :org_a"
                ),
                {"org_a": str(ORG_A), "org_b": str(ORG_B)},
            )

        with pytest.raises(DBAPIError), runtime.begin() as connection:
            _set_tenant(connection, ORG_A)
            connection.execute(
                text(
                    f'INSERT INTO "{schema}".children '
                    "(organisation_id, child_id, parent_id) "
                    "VALUES (:org_a, :child_id, :parent_b)"
                ),
                {
                    "org_a": str(ORG_A),
                    "child_id": str(uuid4()),
                    "parent_b": str(PARENT_B),
                },
            )

        with runtime.begin() as connection:
            assert connection.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one() == backend_pid
            assert connection.execute(
                text(f'SELECT count(*) FROM "{schema}".parents')
            ).scalar_one() == 0

        with admin.connect() as connection:
            role = (
                connection.execute(
                    text(
                        """
                        SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole
                          FROM pg_roles
                         WHERE rolname = 'app_tenant_runtime'
                        """
                    )
                )
                .mappings()
                .one()
            )
            assert not any(role.values())
            assert connection.execute(
                text(
                    """
                    SELECT count(*)
                      FROM pg_class class_record
                      JOIN pg_namespace namespace_record
                        ON namespace_record.oid = class_record.relnamespace
                     WHERE namespace_record.nspname = :schema_name
                       AND pg_get_userbyid(class_record.relowner)
                           = 'app_tenant_runtime'
                    """
                ),
                {"schema_name": schema},
            ).scalar_one() == 0

        with admin.connect() as connection, connection.begin():
            validate_tenant_runtime_catalog(
                connection,  # type: ignore[arg-type]
                runtime_role="app_tenant_runtime",
                schema=schema,
            )

    finally:
        runtime.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def test_startup_gate_rejects_each_unsafe_catalog_state() -> None:
    """Prove every named startup invariant fails against real PostgreSQL."""

    assert ADMIN_URL is not None
    schema = f"rls_startup_{uuid4().hex[:12]}"
    table = f'"{schema}".widgets'
    admin = create_engine(ADMIN_URL)

    def assert_rejected(match: str) -> None:
        with (
            pytest.raises(TenantRuntimeReadinessError, match=match),
            admin.connect() as connection,
            connection.begin(),
        ):
            validate_tenant_runtime_catalog(
                connection,  # type: ignore[arg-type]
                runtime_role="app_tenant_runtime",
                schema=schema,
            )

    try:
        with (
            pytest.raises(TenantRuntimeReadinessError, match="missing tenant_data"),
            admin.connect() as connection,
            connection.begin(),
        ):
            validate_tenant_runtime_catalog(
                connection,  # type: ignore[arg-type]
                runtime_role="app_tenant_runtime",
                schema=f"{schema}_missing",
            )

        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(
                text(
                    f"""
                    CREATE TABLE {table} (
                        organisation_id uuid NOT NULL,
                        widget_id uuid NOT NULL,
                        PRIMARY KEY (organisation_id, widget_id)
                    )
                    """
                )
            )
            install_tenant_rls(
                connection,
                schema_name=schema,
                runtime_role="app_tenant_runtime",
            )

        with admin.begin() as connection:
            connection.execute(text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
        assert_rejected("RLS disabled")
        with admin.begin() as connection:
            connection.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))

        with admin.begin() as connection:
            connection.execute(text(f'DROP POLICY "tenant_access" ON {table}'))
        assert_rejected("invalid tenant policies")
        with admin.begin() as connection:
            protect_tenant_table(
                connection,
                schema_name=schema,
                table_name="widgets",
            )

        with admin.begin() as connection:
            connection.execute(
                text(f"ALTER TABLE {table} OWNER TO app_tenant_runtime")
            )
        assert_rejected("runtime can assume owner")
        with admin.begin() as connection:
            connection.execute(text(f"ALTER TABLE {table} OWNER TO postgres"))
            connection.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} "
                    "TO app_tenant_runtime"
                )
            )

        with admin.begin() as connection:
            connection.execute(
                text(f"REVOKE SELECT ON {table} FROM app_tenant_runtime")
            )
        assert_rejected("missing DML grants")
        with admin.begin() as connection:
            connection.execute(
                text(f"GRANT SELECT ON {table} TO app_tenant_runtime")
            )

        with admin.begin() as connection:
            connection.execute(
                text(
                    f'GRANT CREATE ON SCHEMA "{schema}" TO app_tenant_runtime'
                )
            )
        assert_rejected("runtime has CREATE")
        with admin.begin() as connection:
            connection.execute(
                text(
                    f'REVOKE CREATE ON SCHEMA "{schema}" FROM app_tenant_runtime'
                )
            )

        with admin.begin() as connection:
            connection.execute(text("ALTER ROLE app_tenant_runtime BYPASSRLS"))
        assert_rejected("unsafe attributes")
        with admin.begin() as connection:
            connection.execute(text("ALTER ROLE app_tenant_runtime NOBYPASSRLS"))

        with admin.begin() as connection:
            connection.execute(
                text(
                    f'GRANT EXECUTE ON FUNCTION "{schema}".'
                    "current_organisation_id() TO PUBLIC"
                )
            )
        assert_rejected("context function")
    finally:
        with admin.begin() as connection:
            connection.execute(text("ALTER ROLE app_tenant_runtime NOBYPASSRLS"))
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()
