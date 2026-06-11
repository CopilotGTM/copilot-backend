from __future__ import annotations

import argparse
import secrets

from azure.core.exceptions import ResourceNotFoundError
from azure.keyvault.secrets import SecretClient
from sqlalchemy import text
from sqlalchemy.exc import NotSupportedError
from src.infra.config.configs import database_config
from src.infra.database.sessions import master_session_scope
from src.infra.database.tenant_runtime_config import validate_tenant_runtime_config
from src.infra.secrets.master_key_vault_sync import build_master_secret_client_sync


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def build_secret_client() -> SecretClient:
    return build_master_secret_client_sync(prefer_default_credential=True)


def ensure_existing_secret(client: SecretClient, secret_name: str) -> None:
    try:
        secret = client.get_secret(secret_name)
    except ResourceNotFoundError as exc:
        raise RuntimeError(
            f"Tenant runtime password secret '{secret_name}' missing from Key Vault"
        ) from exc
    if secret is None or not getattr(secret, "value", None):
        raise RuntimeError(f"Tenant runtime password secret '{secret_name}' missing from Key Vault")


def ensure_trusted_extension_schema(session, *, runtime_role: str) -> None:  # noqa: ANN001
    extension_schema_sql = quote_identifier("extensions")
    runtime_role_sql = quote_identifier(runtime_role)
    owner_sql = quote_identifier(database_config.DATABASE_USER)
    session.execute(
        text(f"CREATE SCHEMA IF NOT EXISTS {extension_schema_sql} AUTHORIZATION {owner_sql}")
    )
    session.execute(text(f"REVOKE CREATE ON SCHEMA {extension_schema_sql} FROM PUBLIC"))
    session.execute(text(f"GRANT USAGE ON SCHEMA {extension_schema_sql} TO {runtime_role_sql}"))
    session.execute(text(f"CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA {extension_schema_sql}"))
    try:
        with session.begin_nested():
            session.execute(text(f"ALTER EXTENSION vector SET SCHEMA {extension_schema_sql}"))
    except NotSupportedError:
        print(
            "warning: PostgreSQL does not support ALTER EXTENSION vector SET SCHEMA; "
            "leaving existing vector extension schema unchanged"
        )


def provision_runtime_role(*, execute: bool, rotate_password: bool = False) -> None:
    config = validate_tenant_runtime_config(
        database_user=database_config.DATABASE_USER,
        tenant_database_user=database_config.TENANT_DATABASE_USER,
        tenant_password=database_config.TENANT_DATABASE_PASSWORD,
        tenant_password_secret_name=database_config.TENANT_DATABASE_PASSWORD_SECRET_NAME,
    )
    password = config.password or secrets.token_urlsafe(32)
    role_sql = quote_identifier(config.user)
    database_sql = quote_identifier(database_config.DATABASE_NAME)

    if not execute:
        print(f"dry-run provision tenant runtime role {config.user}")
        return

    client = build_secret_client()
    with master_session_scope() as session:
        role_exists = session.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :role_name"),
            {"role_name": config.user},
        ).scalar_one_or_none()

    should_store_secret = (not role_exists) or rotate_password
    if not should_store_secret and not config.password:
        ensure_existing_secret(client, config.password_secret_name)

    with master_session_scope() as session, session.begin():
        if role_exists:
            if rotate_password:
                session.execute(
                    text(
                        f"ALTER ROLE {role_sql} WITH LOGIN PASSWORD :pwd "
                        "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"
                    ),
                    {"pwd": password},
                )
            else:
                session.execute(
                    text(
                        f"ALTER ROLE {role_sql} WITH LOGIN "
                        "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"
                    )
                )
        else:
            session.execute(
                text(
                    f"CREATE ROLE {role_sql} LOGIN PASSWORD :pwd "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"
                ),
                {"pwd": password},
            )
        session.execute(
            text(f"ALTER ROLE {role_sql} IN DATABASE {database_sql} SET search_path = pg_catalog")
        )
        session.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
        ensure_trusted_extension_schema(session, runtime_role=config.user)
        if should_store_secret:
            client.set_secret(config.password_secret_name, password)

    if not should_store_secret:
        print(f"verified tenant runtime role {config.user}; password unchanged")
        return
    print(f"stored tenant runtime password in {config.password_secret_name}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--rotate-password", action="store_true")
    args = parser.parse_args()
    provision_runtime_role(execute=args.execute, rotate_password=args.rotate_password)


if __name__ == "__main__":
    main()
