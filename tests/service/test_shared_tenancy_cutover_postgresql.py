"""Real-PostgreSQL proof for atomic selected-tenant cutover."""

# ruff: noqa: S608

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from migrations.migration_manager import ProductionMigrationManager
from migrations.org_migrations.tenant_policy import install_tenant_rls
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from src.infra.config.configs import database_config
from src.infra.utils.resource_naming import get_role_name, get_schema_name
from src.service.orgnisation.shared_tenancy_cutover_service import (
    CUTOVER_LOCK_NAMESPACE,
    CutoverError,
    SharedTenancyCutoverService,
)

ADMIN_URL = os.getenv("TENANT_RLS_TEST_ADMIN_URL")
pytestmark = pytest.mark.skipif(
    not ADMIN_URL,
    reason="set TENANT_RLS_TEST_ADMIN_URL to a disposable PostgreSQL database",
)


def _create_table_pair(connection, source: str, destination: str) -> None:  # noqa: ANN001
    """Create matching parent/child tables with tenant-safe relational keys."""

    for schema in (source, destination):
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(
            text(
                f"""
                CREATE TABLE "{schema}".parents (
                    organisation_id uuid NOT NULL,
                    parent_id uuid NOT NULL,
                    payload text NOT NULL,
                    PRIMARY KEY (organisation_id, parent_id),
                    UNIQUE (organisation_id, payload)
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
                    payload text NOT NULL,
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
        connection.execute(
            text(f'CREATE TABLE "{schema}".alembic_version (version_num varchar(64) PRIMARY KEY)')
        )
        connection.execute(
            text(f"INSERT INTO \"{schema}\".alembic_version VALUES ('cutover_test_head')")
        )


def _count_for_org(connection, schema: str, table: str, org_id: UUID) -> int:  # noqa: ANN001
    """Count one organisation's rows without relying on runtime RLS."""

    return int(
        connection.execute(
            text(
                f'SELECT count(*) FROM "{schema}"."{table}" '
                "WHERE organisation_id = :organisation_id"
            ),
            {"organisation_id": str(org_id)},
        ).scalar_one()
    )


def test_cutover_prechecks_locking_validation_and_failure_rollback() -> None:
    """Copy succeeds once, while every unsafe or failed attempt leaves no partial rows."""

    assert ADMIN_URL is not None
    engine = create_engine(ADMIN_URL)
    organisation_id = uuid4()
    other_organisation_id = uuid4()
    extra_organisation_id = uuid4()
    source = get_schema_name(organisation_id)
    destination = f"cutover_target_{uuid4().hex[:12]}"
    extra_source = get_schema_name(extra_organisation_id)
    parent_id = uuid4()
    child_id = uuid4()
    sentinel_parent_id = uuid4()

    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO public.organisations (
                        organisation_id,
                        organisation_name,
                        is_active,
                        status
                    ) VALUES (
                        :organisation_id,
                        :organisation_name,
                        true,
                        'PENDING'
                    )
                    """
                ),
                {
                    "organisation_id": str(organisation_id),
                    "organisation_name": f"Cutover test {organisation_id}",
                },
            )
            _create_table_pair(connection, source, destination)
            install_tenant_rls(
                connection,
                schema_name=destination,
                table_names=("parents", "children"),
            )
            connection.execute(
                text(
                    f'INSERT INTO "{source}".parents VALUES '
                    "(:organisation_id, :parent_id, 'source-parent')"
                ),
                {
                    "organisation_id": str(organisation_id),
                    "parent_id": str(parent_id),
                },
            )
            connection.execute(
                text(
                    f'INSERT INTO "{source}".children VALUES '
                    "(:organisation_id, :child_id, :parent_id, 'source-child')"
                ),
                {
                    "organisation_id": str(organisation_id),
                    "child_id": str(child_id),
                    "parent_id": str(parent_id),
                },
            )
            connection.execute(
                text(
                    f'INSERT INTO "{destination}".parents VALUES '
                    "(:organisation_id, :parent_id, 'sentinel')"
                ),
                {
                    "organisation_id": str(other_organisation_id),
                    "parent_id": str(sentinel_parent_id),
                },
            )

        with Session(engine) as session:
            service = SharedTenancyCutoverService(
                session,
                expected_revision="cutover_test_head",
                destination_schema=destination,
            )
            precheck = service.precheck(organisation_id)

        assert precheck["result"] == "ready"
        assert precheck["mutation_performed"] is False
        assert precheck["table_order"] == ["parents", "children"]
        assert precheck["tables"]["parents"]["source_count"] == 1
        assert len(precheck["tables"]["parents"]["source_row_sha256"]) == 64
        assert precheck["tables"]["parents"]["destination_count"] == 0
        with engine.connect() as connection:
            assert _count_for_org(connection, destination, "parents", organisation_id) == 0

        # Other populated organisations remain valid independent future cutover sources.
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{extra_source}"'))
            connection.execute(
                text(
                    f'CREATE TABLE "{extra_source}".widgets ('
                    "organisation_id uuid NOT NULL, widget_id uuid PRIMARY KEY)"
                )
            )
            connection.execute(
                text(f'INSERT INTO "{extra_source}".widgets VALUES (:org, :widget)'),
                {"org": str(extra_organisation_id), "widget": str(uuid4())},
            )
        with Session(engine) as session:
            multi_source_precheck = SharedTenancyCutoverService(
                session,
                expected_revision="cutover_test_head",
                destination_schema=destination,
            ).precheck(organisation_id)
        assert multi_source_precheck["result"] == "ready"
        assert multi_source_precheck["source_schema"] == source
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{extra_source}" CASCADE'))

        # Every source row must carry the UUID encoded by its exact schema name.
        with engine.begin() as connection:
            connection.execute(
                text(
                    f'INSERT INTO "{source}".parents VALUES '
                    "(:organisation_id, :parent_id, 'wrong-tenant-parent')"
                ),
                {
                    "organisation_id": str(other_organisation_id),
                    "parent_id": str(sentinel_parent_id),
                },
            )
        with Session(engine) as session, pytest.raises(CutoverError) as exc_info:
            SharedTenancyCutoverService(
                session,
                expected_revision="cutover_test_head",
                destination_schema=destination,
            ).precheck(organisation_id)
        assert exc_info.value.code == "source_identity_mismatch"
        with engine.begin() as connection:
            connection.execute(
                text(f'DELETE FROM "{source}".parents WHERE organisation_id = :organisation_id'),
                {"organisation_id": str(other_organisation_id)},
            )

        with engine.begin() as connection:
            connection.execute(
                text(f"UPDATE \"{destination}\".alembic_version SET version_num = 'different_head'")
            )
        with Session(engine) as session, pytest.raises(CutoverError) as exc_info:
            SharedTenancyCutoverService(
                session,
                expected_revision="cutover_test_head",
                destination_schema=destination,
            ).precheck(organisation_id)
        assert exc_info.value.code == "migration_revision_mismatch"
        with engine.begin() as connection:
            connection.execute(
                text(
                    f'UPDATE "{destination}".alembic_version '
                    "SET version_num = 'cutover_test_head'"
                )
            )

        # Destination data for another tenant is legal; target-tenant data is not.
        with engine.begin() as connection:
            connection.execute(
                text(
                    f'INSERT INTO "{destination}".parents VALUES '
                    "(:organisation_id, :parent_id, 'target-collision')"
                ),
                {
                    "organisation_id": str(organisation_id),
                    "parent_id": str(uuid4()),
                },
            )
        with Session(engine) as session, pytest.raises(CutoverError) as exc_info:
            SharedTenancyCutoverService(
                session,
                expected_revision="cutover_test_head",
                destination_schema=destination,
            ).precheck(organisation_id)
        assert exc_info.value.code == "destination_not_empty"
        with engine.begin() as connection:
            connection.execute(
                text(
                    f'DELETE FROM "{destination}".parents WHERE organisation_id = :organisation_id'
                ),
                {"organisation_id": str(organisation_id)},
            )

        # The transaction-scoped advisory lock rejects concurrent cutovers.
        with engine.connect() as lock_connection:
            lock_transaction = lock_connection.begin()
            lock_connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": (f"{CUTOVER_LOCK_NAMESPACE}:{organisation_id}")},
            )
            with Session(engine) as session, pytest.raises(CutoverError) as exc_info:
                SharedTenancyCutoverService(
                    session,
                    expected_revision="cutover_test_head",
                    destination_schema=destination,
                ).copy_and_validate(
                    organisation_id,
                    writers_stopped_evidence="deployment/stop-42",
                    backup_reference="backup/42",
                    apply=True,
                )
            assert exc_info.value.code == "concurrent_cutover"
            lock_transaction.rollback()

        # Fail after the parent insert; the parent row must roll back with the child.
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"""
                    CREATE FUNCTION "{destination}".reject_child_copy()
                    RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN
                        RAISE EXCEPTION 'injected child copy failure';
                    END
                    $$
                    """
                )
            )
            connection.execute(
                text(
                    f"""
                    CREATE TRIGGER reject_child_copy
                    BEFORE INSERT ON "{destination}".children
                    FOR EACH ROW EXECUTE FUNCTION
                    "{destination}".reject_child_copy()
                    """
                )
            )
        with Session(engine) as session, pytest.raises(CutoverError) as exc_info:
            SharedTenancyCutoverService(
                session,
                expected_revision="cutover_test_head",
                destination_schema=destination,
            ).copy_and_validate(
                organisation_id,
                writers_stopped_evidence="deployment/stop-42",
                backup_reference="backup/42",
                apply=True,
            )
        assert exc_info.value.code == "copy_failed"
        with engine.connect() as connection:
            assert _count_for_org(connection, destination, "parents", organisation_id) == 0
            assert _count_for_org(connection, destination, "children", organisation_id) == 0
            assert (
                _count_for_org(
                    connection,
                    destination,
                    "parents",
                    other_organisation_id,
                )
                == 1
            )

        with engine.begin() as connection:
            connection.execute(text(f'DROP TRIGGER reject_child_copy ON "{destination}".children'))
            connection.execute(text(f'DROP FUNCTION "{destination}".reject_child_copy()'))

        with Session(engine) as session:
            report = SharedTenancyCutoverService(
                session,
                expected_revision="cutover_test_head",
                destination_schema=destination,
            ).copy_and_validate(
                organisation_id,
                writers_stopped_evidence="deployment/stop-42",
                backup_reference="backup/42",
                apply=True,
            )

        assert report["result"] == "copied_and_validated"
        assert report["table_order"] == ["parents", "children"]
        assert report["validation"] == {
            "counts": True,
            "primary_key_sets": True,
            "checksums": True,
            "foreign_keys": True,
            "uniqueness": True,
            "rls": True,
            "sentinel_unchanged": True,
        }
        assert report["tables"]["parents"]["source_count"] == 1
        assert report["tables"]["children"]["source_count"] == 1
        assert (
            report["tables"]["parents"]["source_primary_key_sha256"]
            == report["tables"]["parents"]["destination_primary_key_sha256"]
        )
        assert (
            report["tables"]["children"]["source_row_sha256"]
            == report["tables"]["children"]["destination_row_sha256"]
        )
        with engine.connect() as connection:
            assert _count_for_org(connection, destination, "parents", organisation_id) == 1
            assert _count_for_org(connection, destination, "children", organisation_id) == 1
            assert (
                _count_for_org(
                    connection,
                    destination,
                    "parents",
                    other_organisation_id,
                )
                == 1
            )
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{source}" CASCADE'))
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{destination}" CASCADE'))
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{extra_source}" CASCADE'))
            connection.execute(
                text("DELETE FROM public.organisations WHERE organisation_id = :organisation_id"),
                {"organisation_id": str(organisation_id)},
            )
        engine.dispose()


def test_cutover_matches_and_validates_the_complete_migrated_schema(monkeypatch) -> None:
    """The catalog-driven copy must handle every table at the real migration head."""

    assert ADMIN_URL is not None
    url = make_url(ADMIN_URL)
    monkeypatch.setenv("DATABASE_HOST", str(url.host))
    monkeypatch.setenv("DATABASE_PORT", str(url.port))
    monkeypatch.setenv("DATABASE_NAME", str(url.database))
    monkeypatch.setenv("DATABASE_USER", str(url.username))
    monkeypatch.setenv("DATABASE_PASSWORD", str(url.password))
    monkeypatch.setattr(database_config, "DATABASE_HOST", str(url.host))
    monkeypatch.setattr(database_config, "DATABASE_PORT", str(url.port))
    monkeypatch.setattr(database_config, "DATABASE_NAME", str(url.database))
    monkeypatch.setattr(database_config, "DATABASE_USER", str(url.username))
    monkeypatch.setattr(database_config, "DATABASE_PASSWORD", str(url.password))

    engine = create_engine(ADMIN_URL)
    organisation_id = uuid4()
    source = get_schema_name(organisation_id)
    dashboard_id = uuid4()
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO public.organisations (
                        organisation_id,
                        organisation_name,
                        is_active,
                        status
                    ) VALUES (
                        :organisation_id,
                        :organisation_name,
                        true,
                        'PENDING'
                    )
                    """
                ),
                {
                    "organisation_id": str(organisation_id),
                    "organisation_name": f"Full cutover test {organisation_id}",
                },
            )
            connection.execute(text(f'CREATE SCHEMA "{source}"'))

        migration = ProductionMigrationManager().run_organization_migration(
            schema_name=source,
            organisation_id=organisation_id,
        )
        assert migration.success, migration.error

        with engine.begin() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM tenant_data.alembic_version")
            ).scalar_one()
            connection.execute(
                text(
                    f"""
                    INSERT INTO "{source}".dashboards (
                        dashboard_id,
                        organisation_id,
                        dashboard_key,
                        dashboard_name,
                        dashboard_type
                    ) VALUES (
                        :dashboard_id,
                        :organisation_id,
                        'pipeline',
                        'Pipeline',
                        'crm'
                    )
                    """
                ),
                {
                    "dashboard_id": str(dashboard_id),
                    "organisation_id": str(organisation_id),
                },
            )

        with Session(engine) as session:
            report = SharedTenancyCutoverService(
                session,
                expected_revision=str(revision),
            ).copy_and_validate(
                organisation_id,
                writers_stopped_evidence="deployment/full-stop-42",
                backup_reference="backup/full-42",
                apply=True,
            )

        assert report["result"] == "copied_and_validated"
        assert len(report["table_order"]) == 74
        assert report["tables"]["dashboards"]["source_count"] == 1
        with engine.connect() as connection:
            copied = connection.execute(
                text(
                    "SELECT dashboard_name FROM tenant_data.dashboards "
                    "WHERE organisation_id = :organisation_id"
                ),
                {"organisation_id": str(organisation_id)},
            ).scalar_one()
        assert copied == "Pipeline"
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM tenant_data.dashboards WHERE organisation_id = :organisation_id"),
                {"organisation_id": str(organisation_id)},
            )
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{source}" CASCADE'))
            connection.execute(
                text("DELETE FROM public.organisations WHERE organisation_id = :organisation_id"),
                {"organisation_id": str(organisation_id)},
            )
        engine.dispose()


def test_source_deletion_refuses_dependencies_and_active_sessions() -> None:
    """Schema and role deletion is exact, dependency-safe, and writer-safe."""

    assert ADMIN_URL is not None
    engine = create_engine(ADMIN_URL)
    organisation_id = uuid4()
    source_schema = get_schema_name(organisation_id)
    source_role = get_role_name(organisation_id)
    external_schema = f"external_{organisation_id.hex}"
    source_password = "source_role_test_password"

    try:
        with engine.begin() as connection:
            connection.execute(
                text(f'CREATE ROLE "{source_role}" LOGIN PASSWORD :password'),
                {"password": source_password},
            )
            connection.execute(
                text(f'CREATE SCHEMA "{source_schema}" AUTHORIZATION "{source_role}"')
            )
            connection.execute(
                text(f'CREATE TABLE "{source_schema}".parents (parent_id uuid PRIMARY KEY)')
            )
            connection.execute(
                text(f'GRANT SELECT ON "{source_schema}".parents TO "{source_role}"')
            )
            connection.execute(
                text(f'ALTER TABLE "{source_schema}".parents ENABLE ROW LEVEL SECURITY')
            )
            connection.execute(
                text(
                    f'CREATE FUNCTION "{source_schema}".source_policy_check() '
                    "RETURNS boolean LANGUAGE sql IMMUTABLE AS 'SELECT true'"
                )
            )
            connection.execute(
                text(
                    f'CREATE POLICY source_policy ON "{source_schema}".parents '
                    f'USING ("{source_schema}".source_policy_check())'
                )
            )
            connection.execute(text(f'CREATE SCHEMA "{external_schema}"'))
            connection.execute(
                text(
                    f'CREATE TABLE "{external_schema}".children ('
                    "child_id uuid PRIMARY KEY, parent_id uuid NOT NULL, "
                    f"CONSTRAINT external_source_fk FOREIGN KEY (parent_id) "
                    f'REFERENCES "{source_schema}".parents (parent_id))'
                )
            )

        with Session(engine) as session, pytest.raises(CutoverError) as exc_info:
            SharedTenancyCutoverService(
                session,
                expected_revision="unused",
            ).delete_source_database_resources(
                organisation_id,
                source_schema=source_schema,
                source_role=source_role,
                apply=True,
            )
        assert exc_info.value.code == "source_dependencies"

        with engine.begin() as connection:
            assert connection.execute(
                text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :schema)"),
                {"schema": source_schema},
            ).scalar_one()
            connection.execute(
                text(f'ALTER TABLE "{external_schema}".children DROP CONSTRAINT external_source_fk')
            )

        source_url = make_url(ADMIN_URL).set(
            username=source_role,
            password=source_password,
        )
        source_engine = create_engine(source_url)
        with source_engine.connect() as source_connection:
            source_connection.execute(text(f'SELECT count(*) FROM "{source_schema}".parents'))
            with Session(engine) as session, pytest.raises(CutoverError) as exc_info:
                SharedTenancyCutoverService(
                    session,
                    expected_revision="unused",
                ).delete_source_database_resources(
                    organisation_id,
                    source_schema=source_schema,
                    source_role=source_role,
                    apply=True,
                )
            assert exc_info.value.code == "active_source_sessions"
        source_engine.dispose()

        with engine.begin() as connection:
            connection.execute(
                text(f'GRANT SELECT ON "{external_schema}".children TO "{source_role}"')
            )
        with Session(engine) as session, pytest.raises(CutoverError) as exc_info:
            SharedTenancyCutoverService(
                session,
                expected_revision="unused",
            ).delete_source_database_resources(
                organisation_id,
                source_schema=source_schema,
                source_role=source_role,
                apply=True,
            )
        assert exc_info.value.code == "source_dependencies"

        with engine.begin() as connection:
            connection.execute(
                text(f'REVOKE SELECT ON "{external_schema}".children FROM "{source_role}"')
            )
        with Session(engine) as session:
            report = SharedTenancyCutoverService(
                session,
                expected_revision="unused",
            ).delete_source_database_resources(
                organisation_id,
                source_schema=source_schema,
                source_role=source_role,
                apply=True,
            )

        assert report["schema_absent"] is True
        assert report["role_absent"] is True
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :schema)"),
                {"schema": source_schema},
            ).scalar_one()
            assert connection.execute(
                text("SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"),
                {"role": source_role},
            ).scalar_one()
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{external_schema}" CASCADE'))
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{source_schema}" CASCADE'))
            connection.execute(text(f'DROP ROLE IF EXISTS "{source_role}"'))
        engine.dispose()
