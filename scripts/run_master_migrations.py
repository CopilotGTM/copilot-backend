#!/usr/bin/env python3
import os
import sys

# ── so we can import your app code under `src/` ──
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
sys.path.insert(0, PROJECT_ROOT)

import argparse
import secrets

from sqlalchemy import create_engine, text
from alembic.config import Config
from alembic import command
from azure.identity import (
    WorkloadIdentityCredential,
    ManagedIdentityCredential,
    DefaultAzureCredential,
)
from azure.keyvault.secrets import SecretClient
from src.infra.utils.logger import get_logger
from src.infra.utils.get_master_db_url import get_master_db_url

logger = get_logger("run-master-migrations")


def parse_args():
    p = argparse.ArgumentParser(
        description="Run master migrations (organizations, users, etc.)"
    )
    p.add_argument(
        "--revision", default="head", help="Revision to upgrade to (default: head)"
    )
    p.add_argument(
        "--downgrade", action="store_true", help="Downgrade instead of upgrade"
    )
    p.add_argument(
        "--skip-role", action="store_true", help="Skip creation of master_copilot_role"
    )
    p.add_argument(
        "--skip-migrations", action="store_true", help="Skip running Alembic migrations"
    )
    p.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    return p.parse_args()


def run_master_migrations(revision="head", downgrade=False):
    """Run master migrations using the master_migrations directory."""
    cfg = Config("migrations/master_migrations/alembic.ini")

    print(f"-> Running master migrations to revision '{revision}'...")

    if downgrade:
        command.downgrade(cfg, revision)
        print(f"[OK] Master migrations downgraded to '{revision}'")
    else:
        command.upgrade(cfg, revision)
        print(f"[OK] Master migrations upgraded to '{revision}'")


def _store_password_in_key_vault(secret_name: str, password: str):
    """Store password in Azure Key Vault"""
    print(f"Attempting to store password for '{secret_name}' in Azure Key Vault.")
    try:
        key_vault_url = os.getenv("AZURE_KEY_VAULT_URL")
        if not key_vault_url:
            print("AZURE_KEY_VAULT_URL not set. Skipping Azure Key Vault storage.")
            print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            print(f"IMPORTANT: Store this password manually: {password}")
            print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            return

        credential = None
        # Use DefaultAzureCredential for local development for convenience
        if os.getenv("ENV") == "local":
            print("Auth strategy (migrations): DefaultAzureCredential (local development).")
            credential = DefaultAzureCredential()
        else:
            # Production credential order
            tenant_id = os.getenv("AZURE_TENANT_ID")
            client_id = os.getenv("AZURE_CLIENT_ID")

            if tenant_id and client_id:
                print("Auth strategy (migrations): Workload Identity (federated token)")
                credential = WorkloadIdentityCredential(
                    tenant_id=tenant_id,
                    client_id=client_id,
                )
            elif client_id and not tenant_id:
                print("[migrations] WARNING: Workload identity client id present but AZURE_TENANT_ID missing; using Managed Identity fallback.")
            
            if not credential:
                print("Auth strategy (migrations): Managed Identity fallback")
                credential = ManagedIdentityCredential()

        client = SecretClient(vault_url=key_vault_url, credential=credential)
        client.set_secret(secret_name, password)
        print(f"Successfully stored password in Azure Key Vault as '{secret_name}'.")
    except Exception as e:
        print(f"Failed to store password in Azure Key Vault: {e}")


def create_master_copilot_role(engine):
    """Creates the master-copilot-role if it doesn't exist."""
    role_name = "master_copilot_role"
    password = secrets.token_urlsafe(16)
    secret_name = "master-copilot-role-password"

    with engine.connect() as conn:
        # Use a transaction to ensure all commands succeed or fail together
        with conn.begin():
            # Check if the role already exists
            role_exists = conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role_name"),
                {"role_name": role_name},
            ).scalar_one_or_none()

            if not role_exists:
                print(f"-> Creating role '{role_name}'...")
                conn.execute(
                    text(f"CREATE ROLE {role_name} LOGIN PASSWORD :password"),
                    {"password": password},
                )
                print(f"[OK] Role '{role_name}' created.")
                # Store the password in Key Vault
                _store_password_in_key_vault(secret_name, password)

            else:
                print(f"-> Role '{role_name}' already exists. Skipping creation.")

            # Grant full access to the public schema
            print(f"-> Granting full access on public schema to '{role_name}'...")
            conn.execute(text(f"GRANT ALL PRIVILEGES ON SCHEMA public TO {role_name}"))
            conn.execute(
                text(
                    f"GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO {role_name}"
                )
            )
            conn.execute(
                text(
                    f"GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO {role_name}"
                )
            )
            print("[OK] Privileges granted.")


def main():
    args = parse_args()

    # Verify database connection
    db_url = get_master_db_url()
    engine = create_engine(db_url, future=True)

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("[OK] Database connection successful")
    except Exception as e:
        logger.error(f"[ERROR] Database connection failed: {e}")
        sys.exit(1)

    # Create the master_copilot_role unless skipped
    if not args.skip_role:
        create_master_copilot_role(engine)
    else:
        logger.info("Skipping master_copilot_role creation (--skip-role)")

    # Run master migrations unless skipped
    if not args.skip_migrations:
        run_master_migrations(args.revision, args.downgrade)
    else:
        logger.info("Skipping Alembic migrations (--skip-migrations)")

    logger.info("[DONE] Master migrations script completed successfully!")


if __name__ == "__main__":
    main()
