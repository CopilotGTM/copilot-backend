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

    def _get_org_engine(self, schema_name: str, password: str):
        """Get organization-specific database engine"""
        logger.info(f"Attempting to get organization-specific database engine for schema: {schema_name}")
        host = self.db_config.DATABASE_HOST
        port = self.db_config.DATABASE_PORT
        user = self.db_config.DATABASE_USER
        db_name = self.db_config.DATABASE_NAME

        pw = urllib.parse.quote_plus(password or "")
        url = f"postgresql://{user}:{pw}@{host}:{port}/{db_name}"
        logger.debug(f"Organization database URL constructed for schema {schema_name}: {url}")

        engine = sa_create_engine(
            url,
            echo=False,
            pool_pre_ping=True,
            pool_recycle=self.db_config.POOL_RECYCLE_SEC,
        )
        logger.info(f"Organization-specific database engine created successfully for schema: {schema_name}.")
        return engine

    def create_organization(
        self,
        organisation_name: str,
        admin_email: str,
        admin_first_name: str,
        admin_last_name: str,
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
        logger.info(f"Attempting to create organization: {organisation_name} with admin: {admin_email}")
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
            self._create_organization_tables(schema_name)
            logger.info("Organization tables created.")

            # Create admin user
            logger.info(f"Creating admin user for organization {organization.organisation_id}.")
            admin_user = User(
                supabase_user_id=None,  # Will be set when user signs up with Supabase
                organisation_id=organization.organisation_id,
                first_name=admin_first_name,
                last_name=admin_last_name,
                email=admin_email,
                organisation_role="admin",
                is_admin=True,
                is_active=True,
            )

            session.add(admin_user)
            session.commit()
            session.refresh(admin_user)
            logger.info(f"Admin user created with ID: {admin_user.user_id}")

            # Store password in Azure Key Vault (placeholder)
            logger.info("Attempting to store password in Azure Key Vault.")
            self._store_password_in_key_vault(schema_name, role_password)
            logger.info("Password storage in Key Vault initiated.")

            logger.info(f"Successfully created organization: {organization.organisation_id} with schema: {schema_name}")

            return {
                "organization": organization,
                "admin_user": admin_user,
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
            logger.info(f"Creating role: {role_name}")
            conn.execute(text(f"CREATE ROLE {role_name} LOGIN PASSWORD :pwd NOINHERIT"), {"pwd": password})
            logger.info(f"Role {role_name} created.")

            # Create schema
            logger.info(f"Creating schema: {schema_name}")
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema_name} AUTHORIZATION {role_name}"))
            logger.info(f"Schema {schema_name} created.")

            # Grant usage on schema
            logger.info(f"Granting USAGE on schema {schema_name} to {role_name}.")
            conn.execute(text(f"GRANT USAGE ON SCHEMA {schema_name} TO {role_name}"))
            logger.info("USAGE granted.")

            # Grant all privileges on schema
            logger.info(f"Granting ALL PRIVILEGES on schema {schema_name} to {role_name}.")
            conn.execute(text(f"GRANT ALL PRIVILEGES ON SCHEMA {schema_name} TO {role_name}"))
            logger.info("ALL PRIVILEGES granted.")

            # Grant role to admin user
            admin_user = self.db_config.DATABASE_USER
            logger.info(f"Granting role {role_name} to admin user {admin_user}.")
            conn.execute(text(f"GRANT {role_name} TO {admin_user}"))
            logger.info(f"Role {role_name} granted to {admin_user}.")

        logger.info(f"Successfully created role '{role_name}' and schema '{schema_name}'.")

        # Enable pgvector extension
        logger.debug("Enabling pgvector extension if not exists.")
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
            logger.info("pgvector extension enabled.")

    def _create_organization_tables(self, schema_name: str):
        """Create organization-specific tables"""
        logger.info(f"Attempting to create organization-specific tables for schema: {schema_name}")
        engine = self._get_master_engine()

        # Import models here to avoid circular imports
        logger.debug("Importing models for table creation.")
        from src.domain.models.chat_db_models import ChatSession, ChatMessage
        from src.domain.models.embeddings_db_models import OrganisationDocument
        from sqlmodel import SQLModel

        # Temporarily set schema for table creation
        logger.debug("Temporarily setting schema for table creation.")
        original_schemas = {}
        tables_to_create = [ChatSession, ChatMessage, OrganisationDocument]

        try:
            for table in tables_to_create:
                original_schemas[table] = table.__table__.schema
                table.__table__.schema = schema_name
                logger.debug(f"Set schema for {table.__name__} to {schema_name}.")

            # Create tables
            logger.debug(f"Creating tables in schema {schema_name}.")
            SQLModel.metadata.create_all(
                bind=engine, tables=[table.__table__ for table in tables_to_create], checkfirst=True
            )
            logger.info(f"Successfully created tables in schema '{schema_name}'.")

        except Exception as e:
            logger.error(f"Failed to create tables in schema '{schema_name}': {str(e)}", exc_info=True)
            raise
        finally:
            # Restore original schemas
            logger.debug("Restoring original schemas for tables.")
            for table, original_schema in original_schemas.items():
                table.__table__.schema = original_schema
                logger.debug(f"Restored schema for {table.__name__} to {original_schema}.")

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

    def get_organization_by_id(self, organisation_id: UUID, session: Session) -> Optional[Organisation]:
        """Get organization by ID"""
        logger.info(f"Attempting to get organization by ID: {organisation_id}")
        try:
            stmt = select(Organisation).where(Organisation.organisation_id == organisation_id)
            organization = session.exec(stmt).one_or_none()
            if organization:
                logger.info(f"Successfully retrieved organization with ID: {organisation_id}")
            else:
                logger.info(f"No organization found with ID: {organisation_id}")
            return organization
        except Exception as e:
            logger.error(f"Failed to get organization with ID {organisation_id}: {str(e)}", exc_info=True)
            return None

    def get_organization_by_schema(self, schema_name: str, session: Session) -> Optional[Organisation]:
        """Get organization by schema name"""
        logger.info(f"Attempting to get organization by schema name: {schema_name}")
        try:
            stmt = select(Organisation).where(Organisation.schema_name == schema_name)
            organization = session.exec(stmt).one_or_none()
            if organization:
                logger.info(f"Successfully retrieved organization with schema: {schema_name}")
            else:
                logger.info(f"No organization found with schema: {schema_name}")
            return organization
        except Exception as e:
            logger.error(f"Failed to get organization by schema {schema_name}: {str(e)}", exc_info=True)
            return None

    def list_organizations(self, session: Session) -> List[Organisation]:
        """List all organizations"""
        logger.info("Attempting to list all organizations.")
        try:
            stmt = select(Organisation).order_by(Organisation.created_at.desc())
            organizations = session.exec(stmt).all()
            logger.info(f"Successfully retrieved {len(organizations)} organizations.")
            return organizations
        except Exception as e:
            logger.error(f"Failed to list organizations: {str(e)}", exc_info=True)
            return []

    def update_organization(self, organisation_id: UUID, session: Session, **updates) -> Optional[Organisation]:
        """Update organization details"""
        logger.info(f"Attempting to update organization with ID: {organisation_id}")
        try:
            organization = self.get_organization_by_id(organisation_id, session)
            if not organization:
                logger.warning(f"Organization with ID {organisation_id} not found for update.")
                return None

            logger.debug(f"Updating fields for organization {organisation_id}: {updates}")
            for field, value in updates.items():
                if hasattr(organization, field):
                    setattr(organization, field, value)

            organization.updated_at = datetime.utcnow()
            session.add(organization)
            session.commit()
            session.refresh(organization)
            logger.info(f"Successfully updated organization with ID: {organisation_id}")

            return organization

        except Exception as e:
            logger.error(f"Failed to update organization with ID {organisation_id}: {str(e)}", exc_info=True)
            session.rollback()
            return None

    def delete_organization(self, organisation_id: UUID, session: Session) -> bool:
        """Delete organization and its schema"""
        logger.info(f"Attempting to delete organization with ID: {organisation_id}")
        try:
            organization = self.get_organization_by_id(organisation_id, session)
            if not organization:
                logger.warning(f"Organization with ID {organisation_id} not found for deletion.")
                return False

            schema_name = organization.schema_name
            role_name = organization.role_name
            logger.debug(f"Organization {organisation_id} has schema '{schema_name}' and role '{role_name}'.")

            # Drop schema and role
            if schema_name and role_name:
                logger.debug(f"Attempting to drop schema '{schema_name}' and role '{role_name}'.")
                engine = self._get_master_engine()
                with engine.begin() as conn:
                    # Drop schema (cascade will drop all tables)
                    conn.execute(text(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE"))
                    logger.debug(f"Schema '{schema_name}' dropped.")

                    # Drop role
                    conn.execute(text(f"DROP ROLE IF EXISTS {role_name}"))
                    logger.debug(f"Role '{role_name}' dropped.")

                logger.info(f"Dropped schema '{schema_name}' and role '{role_name}'.")

            # Delete organization record
            logger.debug(f"Deleting organization record for ID: {organisation_id}.")
            session.delete(organization)
            session.commit()
            logger.info(f"Organization record deleted for ID: {organisation_id}.")

            logger.info(f"Successfully deleted organization: {organisation_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to delete organization with ID {organisation_id}: {str(e)}", exc_info=True)
            session.rollback()
            return False

    def run_organization_migrations(self, schema_name: str, target_version: str = "head"):
        """Run migrations for a specific organization"""
        logger.info(f"Attempting to run organization migrations for schema '{schema_name}' to version '{target_version}'.")
        try:
            from alembic.config import Config
            from alembic import command
            from argparse import Namespace

            # Configure Alembic for organization migrations
            logger.info(f"Configuring Alembic for schema '{schema_name}'.")
            cfg = Config("migrations/org_migrations/alembic.ini")
            cfg.cmd_opts = Namespace(x=[f"schema={schema_name}"], sql=False, tag=None)

            # Run migrations
            logger.debug(f"Running migrations for schema '{schema_name}'.")
            command.upgrade(cfg, target_version)

            logger.info(f"Successfully applied migrations to schema '{schema_name}' up to version '{target_version}'.")

        except Exception as e:
            logger.error(f"Failed to run migrations for schema '{schema_name}': {str(e)}", exc_info=True)
            raise

    def run_master_migrations(self, target_version: str = "head"):
        """Run master migrations (organizations, users, etc.)"""
        logger.info(f"Attempting to run master migrations to version '{target_version}'.")
        try:
            from alembic.config import Config
            from alembic import command

            # Configure Alembic for master migrations
            logger.debug("Configuring Alembic for master migrations.")
            cfg = Config("migrations/master_migrations/alembic.ini")

            # Run migrations
            logger.debug("Running master migrations.")
            command.upgrade(cfg, target_version)

            logger.info(f"Successfully applied master migrations up to version '{target_version}'.")

        except Exception as e:
            logger.error(f"Failed to run master migrations: {str(e)}", exc_info=True)
            raise


# Create a global instance
organization_management_service = OrganizationManagementService()
