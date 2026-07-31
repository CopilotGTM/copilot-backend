"""Real provider proof for permanent shared-schema tenant routing."""

# ruff: noqa: S608

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
import src.infra.database.provider_async as provider_async
import src.infra.database.provider_sync as provider_sync
from migrations.org_migrations.tenant_policy import install_tenant_rls
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ADMIN_URL = os.getenv("TENANT_RLS_TEST_ADMIN_URL")
RUNTIME_URL = os.getenv("TENANT_RLS_TEST_RUNTIME_URL")
pytestmark = pytest.mark.skipif(
    not ADMIN_URL or not RUNTIME_URL,
    reason="set TENANT_RLS_TEST_ADMIN_URL and TENANT_RLS_TEST_RUNTIME_URL",
)

ORG_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ORG_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


def _configure_provider(monkeypatch) -> None:
    assert ADMIN_URL is not None and RUNTIME_URL is not None
    admin_url = make_url(ADMIN_URL)
    runtime_url = make_url(RUNTIME_URL)
    monkeypatch.setattr(provider_sync, "_POOL_SIZE", 1)
    monkeypatch.setattr(provider_sync, "_MAX_OVERFLOW", 0)
    monkeypatch.setattr(provider_async, "_POOL_SIZE", 1)
    monkeypatch.setattr(provider_async, "_MAX_OVERFLOW", 0)
    for module in (provider_sync, provider_async):
        monkeypatch.setattr(module.database_config, "DATABASE_HOST", runtime_url.host)
        monkeypatch.setattr(
            module.database_config,
            "DATABASE_PORT",
            str(runtime_url.port),
        )
        monkeypatch.setattr(module.database_config, "DATABASE_NAME", runtime_url.database)
        monkeypatch.setattr(
            module.database_config,
            "DATABASE_USER",
            admin_url.username,
        )
        monkeypatch.setattr(
            module.database_config,
            "DATABASE_PASSWORD",
            admin_url.password,
        )
        monkeypatch.setattr(
            module.database_config,
            "TENANT_DATABASE_USER",
            runtime_url.username,
        )
        monkeypatch.setattr(
            module.database_config,
            "TENANT_DATABASE_PASSWORD",
            runtime_url.password,
        )
        monkeypatch.setattr(
            module.database_config,
            "TENANT_DATABASE_PASSWORD_SECRET_NAME",
            "",
        )
    monkeypatch.setattr(provider_sync.database_config, "DATABASE_SSL_MODE", "disable")


def _create_probe_table(table_name: str) -> None:
    assert ADMIN_URL is not None
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as connection:
            connection.execute(
                text(
                    f"""
                    CREATE TABLE tenant_data."{table_name}" (
                        organisation_id uuid NOT NULL,
                        probe_id uuid NOT NULL,
                        payload text NOT NULL,
                        PRIMARY KEY (organisation_id, probe_id)
                    )
                    """
                )
            )
            install_tenant_rls(
                connection,
                schema_name="tenant_data",
                table_names=(table_name,),
            )
            connection.execute(
                text(
                    f'INSERT INTO tenant_data."{table_name}" '
                    "(organisation_id, probe_id, payload) VALUES "
                    "(:org_a, :probe_a, 'A'), (:org_b, :probe_b, 'B')"
                ),
                {
                    "org_a": str(ORG_A),
                    "probe_a": str(uuid4()),
                    "org_b": str(ORG_B),
                    "probe_b": str(uuid4()),
                },
            )
    finally:
        admin.dispose()


def _drop_probe_table(table_name: str) -> None:
    assert ADMIN_URL is not None
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as connection:
            connection.execute(text(f'DROP TABLE IF EXISTS tenant_data."{table_name}"'))
    finally:
        admin.dispose()


def test_sync_provider_rebinds_tenant_on_one_pooled_backend(monkeypatch) -> None:
    """A reused sync connection must switch UUID context without leaking rows."""

    table_name = f"task8_sync_{uuid4().hex[:10]}"
    _configure_provider(monkeypatch)
    _create_probe_table(table_name)
    provider = provider_sync.SyncSqlAlchemyProvider()

    try:
        with provider.tenant_session(ORG_A) as session:
            backend_pid = session.execute(text("SELECT pg_backend_pid()")).scalar_one()
            assert session.execute(text("SHOW search_path")).scalar_one() == (
                "tenant_data, extensions"
            )
            assert session.execute(
                text("SELECT current_setting('app.current_organisation_id')")
            ).scalar_one() == str(ORG_A)
            assert session.execute(text(f'SELECT payload FROM "{table_name}"')).scalars().all() == [
                "A"
            ]
            session.commit()
            assert session.execute(
                text("SELECT current_setting('app.current_organisation_id')")
            ).scalar_one() == str(ORG_A)

        with provider.tenant_session(ORG_B) as session:
            assert session.execute(text("SELECT pg_backend_pid()")).scalar_one() == backend_pid
            assert session.execute(
                text("SELECT current_setting('app.current_organisation_id')")
            ).scalar_one() == str(ORG_B)
            assert session.execute(text(f'SELECT payload FROM "{table_name}"')).scalars().all() == [
                "B"
            ]

        with provider.master_session() as session:
            assert session.execute(text("SELECT current_schema()")).scalar_one() == "public"
            assert (
                session.execute(
                    text("SELECT current_setting('app.current_organisation_id', true)")
                ).scalar_one_or_none()
                is None
            )
    finally:
        provider.invalidate_tenant(ORG_A)
        _drop_probe_table(table_name)


@pytest.mark.asyncio
async def test_async_provider_rebinds_tenant_on_one_pooled_backend(monkeypatch) -> None:
    """The async provider must enforce the identical pooled-connection boundary."""

    table_name = f"task8_async_{uuid4().hex[:10]}"
    _configure_provider(monkeypatch)
    _create_probe_table(table_name)
    provider = provider_async.AsyncSqlAlchemyProvider()

    try:
        async with provider.tenant_session(ORG_A) as session:
            backend_pid = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            assert (await session.execute(text("SHOW search_path"))).scalar_one() == (
                "tenant_data, extensions"
            )
            assert (
                await session.execute(text("SELECT current_setting('app.current_organisation_id')"))
            ).scalar_one() == str(ORG_A)
            assert (
                await session.execute(text(f'SELECT payload FROM "{table_name}"'))
            ).scalars().all() == ["A"]
            await session.commit()
            assert (
                await session.execute(text("SELECT current_setting('app.current_organisation_id')"))
            ).scalar_one() == str(ORG_A)

        async with provider.tenant_session(ORG_B) as session:
            assert (
                await session.execute(text("SELECT pg_backend_pid()"))
            ).scalar_one() == backend_pid
            assert (
                await session.execute(text("SELECT current_setting('app.current_organisation_id')"))
            ).scalar_one() == str(ORG_B)
            assert (
                await session.execute(text(f'SELECT payload FROM "{table_name}"'))
            ).scalars().all() == ["B"]

        async with provider.master_session() as session:
            assert (await session.execute(text("SELECT current_schema()"))).scalar_one() == "public"
            assert (
                await session.execute(
                    text("SELECT current_setting('app.current_organisation_id', true)")
                )
            ).scalar_one_or_none() is None
    finally:
        await provider.invalidate_tenant(ORG_A)
        _drop_probe_table(table_name)
