from os import link
import secrets
from typing import Dict, Any
from uuid import uuid4
from sqlmodel import Session, text
from sqlalchemy.exc import SQLAlchemyError

from src.domain.models.master_db_models import Organisation
from src.infra.utils.logger import get_logger
from src.infra.utils.database import database_previlage_connectin
from src.infra.config.configs import database_config, key_vault_config
from migrations.migration_manager import migration_manager
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

from uuid import UUID

logger = get_logger("organization_management_service")


class OrganizationManagementService:
    """Service for managing organizations with schema-level isolation."""

    def __init__(self):
        """Initializes the service."""
        self.master_engine = database_previlage_connectin()

    def create_organization(
        self,
        admin_id: UUID,
        organisation_name: str,
        session: Session,
        **org_details,
    ) -> Dict[str, Any]:
        """
        Creates a new organization with a dedicated schema, role, and runs migrations.

        Args:
            organisation_name: The name of the organization.
            session: The database session for the master database.
            **org_details: Additional details for the organization.

        Returns:
            A dictionary containing the created organization details.
        
        Raises:
            Exception: If any step in the organization creation process fails.
        """
        logger.info(f"Starting creation process for organization: {organisation_name}")
        
        org_id = str(uuid4()).replace("-", "_")
        schema_name = f"org_{org_id}"
        role_name = f"org_{org_id}_role"
        role_password = secrets.token_urlsafe(16)

        try:
            # 1. Create the organization record in the master database
            sales_team_size = org_details.get("sales_team_size", None)
            country = org_details.get("country", None)
            company_address = org_details.get("company_address", None)
            linked_in_url = org_details.get("linked_in_url", [])
            website_url = org_details.get("website_url", [])
            summary = org_details.get("summary", None)
            logo_url = org_details.get("logo_url", None)

            organization = Organisation(
                organisation_name=organisation_name,
                schema_name=schema_name,
                role_name=role_name,
                sales_team_size=sales_team_size,
                country=country,    
                company_address=company_address,
                linked_in_url=linked_in_url,
                website_url=website_url,
                summary=summary,
                logo_url=logo_url,
                created_by_auth_user_id=str(admin_id),
                approved_by=None
            )
            session.add(organization)
            session.commit()
            session.refresh(organization)
            logger.info(f"Organization record created for '{organisation_name}' with ID: {organization.organisation_id}")

            self._create_database_role_and_schema(schema_name, role_name, role_password)
            self._run_organization_migrations(schema_name)
            self._store_password_in_key_vault(schema_name, role_password)
            logger.info(f"Successfully created and initialized organization: {organisation_name}")
            
            return {
                "organization": organization,
                "schema_name": schema_name,
            }

        except Exception as e:
            logger.error(f"Failed to create organization '{organisation_name}': {e}", exc_info=True)
            # Rollback is handled by the session context manager in the handler
            raise

    def _create_database_role_and_schema(self, schema_name: str, role_name: str, password: str):
        """Creates a new database role and schema for an organization."""
        logger.info(f"Creating database role '{role_name}' and schema '{schema_name}'.")
        try:
            with self.master_engine.begin() as conn:
                conn.execute(text(f"CREATE ROLE {role_name} LOGIN PASSWORD :pwd NOINHERIT"), {"pwd": password})
                conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema_name} AUTHORIZATION {role_name}"))
                conn.execute(text(f"CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA {schema_name}"))
                conn.execute(text(f"GRANT USAGE ON SCHEMA {schema_name} TO {role_name}"))
                conn.execute(text(f"GRANT ALL PRIVILEGES ON SCHEMA {schema_name} TO {role_name}"))
                
                admin_user = database_config.DATABASE_USER
                if admin_user:
                    conn.execute(text(f"GRANT {role_name} TO {admin_user}"))
                
            logger.info(f"Successfully created role '{role_name}' and schema '{schema_name}'.")
        except SQLAlchemyError as e:
            logger.error(f"Database role/schema creation failed for '{schema_name}': {e}", exc_info=True)
            raise

    def _run_organization_migrations(self, schema_name: str, target_version: str = "head"):
        """Runs Alembic migrations for a specific organization's schema."""
        logger.info(f"Running migrations for schema '{schema_name}' to version '{target_version}'.")
        try:
            result = migration_manager.run_organization_migration(
                schema_name=schema_name,
                target_revision=target_version,
                validate_schema=True,
            )
            if not result.success:
                raise Exception(f"Alembic migration failed for schema '{schema_name}': {result.error}")
            
            logger.info(f"Successfully applied migrations to schema '{schema_name}' in {result.duration:.2f}s.")
        except Exception as e:
            logger.error(f"Migration execution failed for schema '{schema_name}': {e}", exc_info=True)
            raise

    def _store_password_in_key_vault(self, schema_name: str, password: str):
        """Store password in Azure Key Vault"""
        logger.info(f"Attempting to store password for schema '{schema_name}' in Azure Key Vault.")
        try:
            key_vault_url = key_vault_config.AZURE_KEY_VAULT_URL
            if not key_vault_url:
                logger.warning("AZURE_KEY_VAULT_URL not set. Skipping Azure Key Vault storage.")
                return
            
            # Azure Key Vault secret names only allow alphanumeric characters and dashes.
            key_vault_secret_name = f"hello-db-password"
            secret_name = key_vault_secret_name
            logger.info(f"Key Vault secret name: {secret_name}")
            
            credential = DefaultAzureCredential()
            client = SecretClient(vault_url=key_vault_url, credential=credential)
            logger.info("Azure Key Vault client created.")
            
            client.set_secret(secret_name, password)
            logger.info(f"Successfully stored password for schema '{schema_name}' in Azure Key Vault as '{secret_name}'.")
        except Exception as e:
            logger.error(f"Failed to store password in Azure Key Vault for schema '{schema_name}': {e}", exc_info=True)

organization_management_service = OrganizationManagementService()
