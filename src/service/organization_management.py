import secrets
from typing import Dict, Any
from uuid import uuid4
from sqlmodel import Session, text
from sqlalchemy.exc import SQLAlchemyError

from src.domain.models.master_db_models import Organisation
from src.infra.utils.logger import get_logger
from src.infra.utils.database import get_master_engine
from src.infra.config.configs import database_config
from migrations.migration_manager import migration_manager

logger = get_logger("organization_management_service")


class OrganizationManagementService:
    """Service for managing organizations with schema-level isolation."""

    def __init__(self):
        """Initializes the service."""
        self.master_engine = get_master_engine(echo=False)

    def create_organization(
        self,
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
            organization = Organisation(
                organisation_name=organisation_name,
                schema_name=schema_name,
                role_name=role_name,
                **org_details,
            )
            session.add(organization)
            session.commit()
            session.refresh(organization)
            logger.info(f"Organization record created for '{organisation_name}' with ID: {organization.organisation_id}")

            # 2. Create the database role and schema
            self._create_database_role_and_schema(schema_name, role_name, role_password)

            # 3. Run migrations for the new organization's schema
            self._run_organization_migrations(schema_name)

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


organization_management_service = OrganizationManagementService()
