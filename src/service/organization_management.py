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

logger = get_logger()


class OrganizationManagementService:
    """Service for managing organizations with schema-level isolation"""
    
    def __init__(self):
        self.db_config = database_config
    
    def _get_master_engine(self):
        """Get master database engine"""
        host = self.db_config.DATABASE_HOST
        port = self.db_config.DATABASE_PORT
        user = self.db_config.DATABASE_USER
        password = self.db_config.DATABASE_PASSWORD
        db_name = self.db_config.DATABASE_NAME
        
        pw = urllib.parse.quote_plus(password or "")
        url = f"postgresql://{user}:{pw}@{host}:{port}/{db_name}"
        
        return sa_create_engine(
            url,
            echo=False,
            pool_pre_ping=True,
            pool_recycle=self.db_config.POOL_RECYCLE_SEC,
        )
    
    def _get_org_engine(self, schema_name: str, password: str):
        """Get organization-specific database engine"""
        host = self.db_config.DATABASE_HOST
        port = self.db_config.DATABASE_PORT
        user = self.db_config.DATABASE_USER
        db_name = self.db_config.DATABASE_NAME
        
        pw = urllib.parse.quote_plus(password or "")
        url = f"postgresql://{user}:{pw}@{host}:{port}/{db_name}"
        
        return sa_create_engine(
            url,
            echo=False,
            pool_pre_ping=True,
            pool_recycle=self.db_config.POOL_RECYCLE_SEC,
        )
    
    def create_organization(
        self,
        organisation_name: str,
        admin_email: str,
        admin_first_name: str,
        admin_last_name: str,
        session: Session,
        **org_details
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
        try:
            # Generate unique schema and role names
            org_id = str(uuid4()).replace('-', '_')
            schema_name = f"org_{org_id}"
            role_name = f"org_{org_id}_role"
            
            # Generate password for the role
            role_password = secrets.token_urlsafe(16)
            
            # Create organization record
            organization = Organisation(
                organisation_name=organisation_name,
                schema_name=schema_name,
                role_name=role_name,
                **org_details
            )
            
            session.add(organization)
            session.commit()
            session.refresh(organization)
            
            # Create database role and schema
            self._create_database_role_and_schema(schema_name, role_name, role_password)
            
            # Create organization tables
            self._create_organization_tables(schema_name)
            
            # Create admin user
            admin_user = User(
                supabase_user_id=None,  # Will be set when user signs up with Supabase
                organisation_id=organization.organisation_id,
                first_name=admin_first_name,
                last_name=admin_last_name,
                email=admin_email,
                organisation_role="admin",
                is_admin=True,
                is_active=True
            )
            
            session.add(admin_user)
            session.commit()
            session.refresh(admin_user)
            
            # Store password in Azure Key Vault (placeholder)
            self._store_password_in_key_vault(schema_name, role_password)
            
            logger.info(f"Created organization: {organization.organisation_id} with schema: {schema_name}")
            
            return {
                "organization": organization,
                "admin_user": admin_user,
                "schema_name": schema_name,
                "role_name": role_name,
                "role_password": role_password  # Only return for initial setup
            }
            
        except Exception as e:
            logger.error(f"Failed to create organization: {str(e)}")
            session.rollback()
            raise
    
    def _create_database_role_and_schema(self, schema_name: str, role_name: str, password: str):
        """Create database role and schema"""
        engine = self._get_master_engine()
        
        with engine.begin() as conn:
            # Create role
            conn.execute(
                text(f"CREATE ROLE {role_name} LOGIN PASSWORD :pwd NOINHERIT"),
                {"pwd": password}
            )
            
            # Create schema
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema_name} AUTHORIZATION {role_name}"))
            
            # Grant usage on schema
            conn.execute(text(f"GRANT USAGE ON SCHEMA {schema_name} TO {role_name}"))
            
            # Grant all privileges on schema
            conn.execute(text(f"GRANT ALL PRIVILEGES ON SCHEMA {schema_name} TO {role_name}"))
            
            # Grant role to admin user
            admin_user = self.db_config.DATABASE_USER
            conn.execute(text(f"GRANT {role_name} TO {admin_user}"))
        
        logger.info(f"Created role '{role_name}' and schema '{schema_name}'")
    
    def _create_organization_tables(self, schema_name: str):
        """Create organization-specific tables"""
        engine = self._get_master_engine()
        
        # Import models here to avoid circular imports
        from src.domain.models.chat_db_models import ChatSession, ChatMessage
        from src.domain.models.embeddings_db_models import OrganisationDocument
        from sqlmodel import SQLModel
        
        # Temporarily set schema for table creation
        original_schemas = {}
        tables_to_create = [ChatSession, ChatMessage, OrganisationDocument]
        
        try:
            for table in tables_to_create:
                original_schemas[table] = table.__table__.schema
                table.__table__.schema = schema_name
            
            # Create tables
            SQLModel.metadata.create_all(
                bind=engine,
                tables=[table.__table__ for table in tables_to_create],
                checkfirst=True
            )
            
        finally:
            # Restore original schemas
            for table, original_schema in original_schemas.items():
                table.__table__.schema = original_schema
        
        logger.info(f"Created tables in schema '{schema_name}'")
    
    def _store_password_in_key_vault(self, schema_name: str, password: str):
        """Store password in Azure Key Vault"""
        try:
            key_vault_url = os.getenv("AZURE_KEY_VAULT_URL")
            if not key_vault_url:
                logger.warning("AZURE_KEY_VAULT_URL not set. Skipping Azure Key Vault storage.")
                return
            secret_name = f"{schema_name}-db-password"
            credential = DefaultAzureCredential()
            client = SecretClient(vault_url=key_vault_url, credential=credential)
            client.set_secret(secret_name, password)
            logger.info(f"Stored password for schema '{schema_name}' in Azure Key Vault as '{secret_name}'")
        except Exception as e:
            logger.error(f"Failed to store password in Azure Key Vault: {e}")
    
    def get_organization_by_id(self, organisation_id: UUID, session: Session) -> Optional[Organisation]:
        """Get organization by ID"""
        try:
            stmt = select(Organisation).where(Organisation.organisation_id == organisation_id)
            return session.exec(stmt).one_or_none()
        except Exception as e:
            logger.error(f"Failed to get organization: {str(e)}")
            return None
    
    def get_organization_by_schema(self, schema_name: str, session: Session) -> Optional[Organisation]:
        """Get organization by schema name"""
        try:
            stmt = select(Organisation).where(Organisation.schema_name == schema_name)
            return session.exec(stmt).one_or_none()
        except Exception as e:
            logger.error(f"Failed to get organization by schema: {str(e)}")
            return None
    
    def list_organizations(self, session: Session) -> List[Organisation]:
        """List all organizations"""
        try:
            stmt = select(Organisation).order_by(Organisation.created_at.desc())
            return session.exec(stmt).all()
        except Exception as e:
            logger.error(f"Failed to list organizations: {str(e)}")
            return []
    
    def update_organization(
        self,
        organisation_id: UUID,
        session: Session,
        **updates
    ) -> Optional[Organisation]:
        """Update organization details"""
        try:
            organization = self.get_organization_by_id(organisation_id, session)
            if not organization:
                return None
            
            for field, value in updates.items():
                if hasattr(organization, field):
                    setattr(organization, field, value)
            
            organization.updated_at = datetime.utcnow()
            session.add(organization)
            session.commit()
            session.refresh(organization)
            
            return organization
            
        except Exception as e:
            logger.error(f"Failed to update organization: {str(e)}")
            session.rollback()
            return None
    
    def delete_organization(self, organisation_id: UUID, session: Session) -> bool:
        """Delete organization and its schema"""
        try:
            organization = self.get_organization_by_id(organisation_id, session)
            if not organization:
                return False
            
            schema_name = organization.schema_name
            role_name = organization.role_name
            
            # Drop schema and role
            if schema_name and role_name:
                engine = self._get_master_engine()
                with engine.begin() as conn:
                    # Drop schema (cascade will drop all tables)
                    conn.execute(text(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE"))
                    
                    # Drop role
                    conn.execute(text(f"DROP ROLE IF EXISTS {role_name}"))
                
                logger.info(f"Dropped schema '{schema_name}' and role '{role_name}'")
            
            # Delete organization record
            session.delete(organization)
            session.commit()
            
            logger.info(f"Deleted organization: {organisation_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to delete organization: {str(e)}")
            session.rollback()
            return False
    
    def run_organization_migrations(self, schema_name: str, target_version: str = "head"):
        """Run migrations for a specific organization"""
        try:
            from alembic.config import Config
            from alembic import command
            from argparse import Namespace
            
            # Configure Alembic for organization migrations
            cfg = Config("migrations/org_migrations/alembic.ini")
            cfg.cmd_opts = Namespace(x=[f"schema={schema_name}"], sql=False, tag=None)
            
            # Run migrations
            command.upgrade(cfg, target_version)
            
            logger.info(f"Applied migrations to schema '{schema_name}' up to version '{target_version}'")
            
        except Exception as e:
            logger.error(f"Failed to run migrations for schema '{schema_name}': {str(e)}")
            raise
    
    def run_master_migrations(self, target_version: str = "head"):
        """Run master migrations (organizations, users, etc.)"""
        try:
            from alembic.config import Config
            from alembic import command
            
            # Configure Alembic for master migrations
            cfg = Config("migrations/master_migrations/alembic.ini")
            
            # Run migrations
            command.upgrade(cfg, target_version)
            
            logger.info(f"Applied master migrations up to version '{target_version}'")
            
        except Exception as e:
            logger.error(f"Failed to run master migrations: {str(e)}")
            raise


# Create a global instance
organization_management_service = OrganizationManagementService() 