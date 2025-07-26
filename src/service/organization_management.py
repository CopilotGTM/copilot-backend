import secrets
import urllib.parse
from typing import Optional, Dict, Any, List
from uuid import UUID, uuid4
from sqlmodel import Session, select, create_engine, text
from sqlalchemy import create_engine as sa_create_engine
from datetime import datetime
import os

from src.domain.models.db_models import Organisation, User
from src.infra.utils.logger import get_logger
from src.infra.config.configs import database_config
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

logger = get_logger("organization_management_service")


class OrganizationManagementService:
    """Service for managing organizations with schema-level isolation"""

    def __init__(self):
        self.db_config = database_config

    def _get_master_engine(self):
        """Get master database engine"""
        logger.info("Attempting to get master database engine.")
        host = self.db_config.DATABASE_HOST
        port = self.db_config.DATABASE_PORT
        user = self.db_config.DATABASE_USER
        password = self.db_config.DATABASE_PASSWORD
        db_name = self.db_config.DATABASE_NAME

        pw = urllib.parse.quote_plus(password or "")
        url = f"postgresql://{user}:{pw}@{host}:{port}/{db_name}"
        logger.debug(f"Master database URL constructed: {url}")

        engine = sa_create_engine(
            url,
            echo=False,
            pool_pre_ping=True,
            pool_recycle=self.db_config.POOL_RECYCLE_SEC,
        )
        logger.info("Master database engine created successfully.")
        return engine

    def create_organization(
        self,
        organisation_name: str,
        session: Session,
        **org_details,
    ) -> Dict[str, Any]:
        """
        Create a new organization with schema, role, and admin user.

        Args:
            organisation_name: Name of the organization
            admin_email: Email of the admin user
            admin_first_name: First name of the admin user
            admin_last_name: Last name of the admin user
            session: Database session
            **org_details: Additional organization details

        Returns:
            Dict containing organization and admin user details
        """
        logger.info(f"Attempting to create organization: {organisation_name}")
        try:
            # Generate unique schema and role names
            logger.info("Generating unique schema and role names.")
            org_id = str(uuid4()).replace("-", "_")
            schema_name = f"org_{org_id}"
            role_name = f"org_{org_id}_role"
            logger.info(f"Generated schema: {schema_name}, role: {role_name}")

            # Generate password for the role
            role_password = secrets.token_urlsafe(16)
            logger.info("Generated role password.")

            # Create organization record
            logger.info(f"Creating organization record for {organisation_name}.")
            organization = Organisation(
                organisation_name=organisation_name,
                schema_name=schema_name,
                role_name=role_name,
                **org_details
            )

            session.add(organization)
            session.commit()
            session.refresh(organization)
            logger.info(f"Organization record created with ID: {organization.organisation_id}")

            # Create database role and schema
            logger.info(f"Creating database role and schema for {schema_name}.")
            self._create_database_role_and_schema(schema_name, role_name, role_password)
            logger.info("Database role and schema created.")

            # Create organization tables
            logger.info(f"Creating organization tables for schema: {schema_name}.")
            try:
                self.run_organization_migrations(schema_name)
                logger.info("Organization tables created.")
            except Exception as e:
                logger.error(f"Failed to run migrations for schema '{schema_name}': {str(e)}", exc_info=True)
                raise e

            # # Store password in Azure Key Vault (placeholder)
            # logger.info("Attempting to store password in Azure Key Vault.")
            # self._store_password_in_key_vault(schema_name, role_password)
            # logger.info("Password storage in Key Vault initiated.")

            logger.info(f"Successfully created organization: {organization.organisation_id} with schema: {schema_name}")

            return {
                "organization": organization,
                "schema_name": schema_name,
                "role_name": role_name,
                "role_password": role_password,  # Only return for initial setup
            }

        except Exception as e:
            logger.error(f"Failed to create organization: {str(e)}", exc_info=True)
            session.rollback()
            raise

    def _create_database_role_and_schema(self, schema_name: str, role_name: str, password: str):
        """Create database role and schema"""
        logger.info(f"Attempting to create database role '{role_name}' and schema '{schema_name}'.")
        engine = self._get_master_engine()

        with engine.begin() as conn:
            # Create role
            conn.execute(text(f"CREATE ROLE {role_name} LOGIN PASSWORD :pwd NOINHERIT"), {"pwd": password})
            logger.info(f"Role {role_name} created.")

            # Create schema and set authorization
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema_name} AUTHORIZATION {role_name}"))
            logger.info(f"Schema {schema_name} created.")

            # Enable pgvector extension within the new schema
            conn.execute(text(f"CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA {schema_name}"))
            logger.info(f"pgvector extension enabled in schema {schema_name}.")

            # Grant usage on schema
            conn.execute(text(f"GRANT USAGE ON SCHEMA {schema_name} TO {role_name}"))
            logger.info("USAGE granted.")

            # Grant all privileges on schema
            conn.execute(text(f"GRANT ALL PRIVILEGES ON SCHEMA {schema_name} TO {role_name}"))
            logger.info("ALL PRIVILEGES granted.")

            # Grant role to admin user
            admin_user = self.db_config.DATABASE_USER
            conn.execute(text(f"GRANT {role_name} TO {admin_user}"))
            logger.info(f"Role {role_name} granted to {admin_user}.")

        logger.info(f"Successfully created role '{role_name}' and schema '{schema_name}'.")


    def _store_password_in_key_vault(self, schema_name: str, password: str):
        """Store password in Azure Key Vault"""
        logger.info(f"Attempting to store password for schema '{schema_name}' in Azure Key Vault.")
        try:
            key_vault_url = os.getenv("AZURE_KEY_VAULT_URL")
            if not key_vault_url:
                logger.warning("AZURE_KEY_VAULT_URL not set. Skipping Azure Key Vault storage.")
                return
            
            # Azure Key Vault secret names only allow alphanumeric characters and dashes.
            # Replace underscores in schema_name with dashes for the secret name.
            key_vault_secret_name = f"hello-db-password"
            secret_name = key_vault_secret_name
            logger.debug(f"Key Vault secret name: {secret_name}")
            
            credential = DefaultAzureCredential()
            client = SecretClient(vault_url=key_vault_url, credential=credential)
            logger.debug("Azure Key Vault client created.")
            
            client.set_secret(secret_name, password)
            logger.info(f"Successfully stored password for schema '{schema_name}' in Azure Key Vault as '{secret_name}'.")
        except Exception as e:
            logger.error(f"Failed to store password in Azure Key Vault for schema '{schema_name}': {e}", exc_info=True)

    def run_organization_migrations(self, schema_name: str, target_version: str = "head"):
        """Run migrations for a specific organization using the production migration manager"""
        logger.info(f"Attempting to run organization migrations for schema '{schema_name}' to version '{target_version}'.")
        try:
            from migrations.migration_manager import migration_manager
            
            # Use the production migration manager
            result = migration_manager.run_organization_migration(
                schema_name=schema_name,
                target_revision=target_version,
                validate_schema=True
            )
            
            if not result.success:
                raise Exception(f"Migration failed: {result.error}")
            
            logger.info(f"Successfully applied migrations to schema '{schema_name}' in {result.duration:.2f}s")

        except Exception as e:
            logger.error(f"Failed to run migrations for schema '{schema_name}': {str(e)}", exc_info=True)
            raise


# Create a global instance
organization_management_service = OrganizationManagementService()
