"""
Production Migration Manager for Multi-Tenant Schema Architecture
Handles both master and organization-specific migrations safely.
"""
import logging
import sys
from typing import List, Dict, Any, Optional
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum
import time

from alembic.config import Config
from alembic import command
from alembic.script import ScriptDirectory
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text, inspect
from sqlmodel import Session, select

from src.domain.models.master_tables import Organisation
from src.infra.config.configs import database_config
from src.infra.utils.logger import get_logger

logger = get_logger("migration_manager")


class MigrationType(Enum):
    MASTER = "master"
    ORGANIZATION = "organization"


@dataclass
class MigrationResult:
    schema_name: str
    success: bool
    error: Optional[str] = None
    duration: float = 0.0
    applied_migrations: List[str] = None


class ProductionMigrationManager:
    """
    Production-ready migration manager for multi-tenant architecture.
    Handles rollbacks, validation, and safe concurrent migrations.
    """
    
    def __init__(self):
        self.db_config = database_config
        self.master_engine = self._create_master_engine()
        
    def _create_master_engine(self):
        """Create master database engine with production settings"""
        import urllib.parse
        
        host = self.db_config.DATABASE_HOST
        port = self.db_config.DATABASE_PORT
        user = self.db_config.DATABASE_USER
        password = self.db_config.DATABASE_PASSWORD
        db_name = self.db_config.DATABASE_NAME
        
        pw = urllib.parse.quote_plus(password or "")
        url = f"postgresql://{user}:{pw}@{host}:{port}/{db_name}"
        
        return create_engine(
            url,
            echo=False,
            pool_pre_ping=True,
            pool_recycle=self.db_config.POOL_RECYCLE_SEC,
            pool_size=10,
            max_overflow=20,
            connect_args={
                "connect_timeout": 30,
                "application_name": "migration_manager"
            }
        )
    
    def run_master_migrations(self, target_revision: str = "head") -> MigrationResult:
        """Run master database migrations"""
        logger.info(f"Starting master migrations to revision: {target_revision}")
        start_time = time.time()
        
        try:
            cfg = Config("migrations/master_migrations/alembic.ini")
            
            # Validate migration path exists
            if not self._validate_migration_environment("master"):
                raise Exception("Master migration environment validation failed")
            
            # Run migrations
            command.upgrade(cfg, target_revision)
            
            duration = time.time() - start_time
            logger.info(f"Master migrations completed successfully in {duration:.2f}s")
            
            return MigrationResult(
                schema_name="master",
                success=True,
                duration=duration
            )
            
        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"Master migration failed: {str(e)}")
            return MigrationResult(
                schema_name="master",
                success=False,
                error=str(e),
                duration=duration
            )
    
    def run_organization_migration(
        self, 
        schema_name: str, 
        target_revision: str = "head",
        validate_schema: bool = True
    ) -> MigrationResult:
        """Run migrations for a specific organization schema"""
        logger.info(f"Starting migration for schema: {schema_name}")
        start_time = time.time()
        
        try:
            if validate_schema and not self._validate_schema_exists(schema_name):
                raise Exception(f"Schema {schema_name} does not exist")
            
            cfg = Config("migrations/org_migrations/alembic.ini")
            cfg.cmd_opts = type('Args', (), {
                'x': [f"schema={schema_name}"],
                'sql': False,
                'tag': None
            })()
            
            # Get current revision before migration
            current_rev = self._get_current_revision(schema_name)
            logger.info(f"Current revision for {schema_name}: {current_rev}")
            
            # Run migration
            logger.info(f"Running migration upgrade for {schema_name} to {target_revision}")
            command.upgrade(cfg, target_revision)
            logger.info(f"Migration upgrade completed for {schema_name}")
            
            # Don't try to get the new revision immediately as it might interfere with the transaction
            # The migration success is determined by the fact that command.upgrade didn't throw an exception
            
            duration = time.time() - start_time
            logger.info(f"Migration completed for {schema_name} in {duration:.2f}s")
            
            return MigrationResult(
                schema_name=schema_name,
                success=True,
                duration=duration,
                applied_migrations=[current_rev, target_revision]
            )
            
        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"Migration failed for {schema_name}: {str(e)}", exc_info=True)
            return MigrationResult(
                schema_name=schema_name,
                success=False,
                error=str(e),
                duration=duration
            )
    
    def run_all_organization_migrations(
        self, 
        target_revision: str = "head",
        max_workers: int = 5,
        fail_fast: bool = False
    ) -> List[MigrationResult]:
        """Run migrations for all organization schemas concurrently"""
        logger.info("Starting migrations for all organization schemas")
        
        # Get all organization schemas
        schemas = self._get_all_organization_schemas()
        if not schemas:
            logger.info("No organization schemas found")
            return []
        
        logger.info(f"Found {len(schemas)} organization schemas to migrate")
        
        results = []
        failed_schemas = []
        
        # Use ThreadPoolExecutor for concurrent migrations
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all migration tasks
            future_to_schema = {
                executor.submit(
                    self.run_organization_migration, 
                    schema, 
                    target_revision
                ): schema for schema in schemas
            }
            
            # Process completed migrations
            for future in as_completed(future_to_schema):
                schema = future_to_schema[future]
                try:
                    result = future.result()
                    results.append(result)
                    
                    if not result.success:
                        failed_schemas.append(schema)
                        logger.error(f"Migration failed for schema: {schema}")
                        
                        if fail_fast:
                            logger.error("Fail-fast enabled, stopping remaining migrations")
                            # Cancel remaining futures
                            for remaining_future in future_to_schema:
                                if not remaining_future.done():
                                    remaining_future.cancel()
                            break
                    else:
                        logger.info(f"Migration successful for schema: {schema}")
                        
                except Exception as e:
                    logger.error(f"Unexpected error for schema {schema}: {str(e)}")
                    results.append(MigrationResult(
                        schema_name=schema,
                        success=False,
                        error=str(e)
                    ))
        
        # Summary
        successful = len([r for r in results if r.success])
        failed = len([r for r in results if not r.success])
        
        logger.info(f"Migration summary: {successful} successful, {failed} failed")
        
        if failed_schemas:
            logger.warning(f"Failed schemas: {', '.join(failed_schemas)}")
        
        return results
    
    def rollback_organization_migration(
        self, 
        schema_name: str, 
        target_revision: str
    ) -> MigrationResult:
        """Rollback organization schema to specific revision"""
        logger.info(f"Rolling back schema {schema_name} to revision: {target_revision}")
        start_time = time.time()
        
        try:
            cfg = Config("migrations/org_migrations/alembic.ini")
            cfg.cmd_opts = type('Args', (), {
                'x': [f"schema={schema_name}"],
                'sql': False,
                'tag': None
            })()
            
            # Validate target revision exists
            if not self._validate_revision_exists(target_revision, "org"):
                raise Exception(f"Target revision {target_revision} does not exist")
            
            command.downgrade(cfg, target_revision)
            
            duration = time.time() - start_time
            logger.info(f"Rollback completed for {schema_name} in {duration:.2f}s")
            
            return MigrationResult(
                schema_name=schema_name,
                success=True,
                duration=duration
            )
            
        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"Rollback failed for {schema_name}: {str(e)}")
            return MigrationResult(
                schema_name=schema_name,
                success=False,
                error=str(e),
                duration=duration
            )
    
    def validate_migration_consistency(self) -> Dict[str, Any]:
        """Validate that all organization schemas have consistent migrations"""
        logger.info("Validating migration consistency across all schemas")
        
        schemas = self._get_all_organization_schemas()
        if not schemas:
            return {"consistent": True, "schemas": []}
        
        # Get current revision for each schema
        schema_revisions = {}
        for schema in schemas:
            try:
                revision = self._get_current_revision(schema)
                schema_revisions[schema] = revision
            except Exception as e:
                logger.error(f"Failed to get revision for {schema}: {str(e)}")
                schema_revisions[schema] = "ERROR"
        
        # Check consistency
        revisions = list(schema_revisions.values())
        consistent = len(set(revisions)) <= 1 and "ERROR" not in revisions
        
        return {
            "consistent": consistent,
            "schemas": schema_revisions,
            "total_schemas": len(schemas),
            "unique_revisions": list(set(revisions))
        }
    
    def _get_all_organization_schemas(self) -> List[str]:
        """Get all organization schema names from the database"""
        try:
            with Session(self.master_engine) as session:
                stmt = select(Organisation.schema_name).where(
                    Organisation.schema_name.isnot(None)
                )
                schemas = session.exec(stmt).all()
                return [schema for schema in schemas if schema]
        except Exception as e:
            logger.error(f"Failed to get organization schemas: {str(e)}")
            return []
    
    def _validate_schema_exists(self, schema_name: str) -> bool:
        """Validate that a schema exists in the database"""
        try:
            with self.master_engine.connect() as conn:
                result = conn.execute(text(
                    "SELECT schema_name FROM information_schema.schemata WHERE schema_name = :schema"
                ), {"schema": schema_name})
                return result.fetchone() is not None
        except Exception as e:
            logger.error(f"Failed to validate schema {schema_name}: {str(e)}")
            return False
    
    def _validate_migration_environment(self, migration_type: str) -> bool:
        """Validate migration environment setup"""
        try:
            migration_path = f"migrations/{migration_type}_migrations"
            alembic_ini = Path(migration_path) / "alembic.ini"
            env_py = Path(migration_path) / "env.py"
            
            return alembic_ini.exists() and env_py.exists()
        except Exception as e:
            logger.error(f"Migration environment validation failed: {str(e)}")
            return False
    
    def _get_current_revision(self, schema_name: str) -> Optional[str]:
        """Get current migration revision for a schema"""
        try:
            with self.master_engine.connect() as conn:
                # Query the alembic_version table directly
                result = conn.execute(text(
                    f"SELECT version_num FROM {schema_name}.alembic_version LIMIT 1"
                ))
                row = result.fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.debug(f"No current revision found for {schema_name}: {str(e)}")
            return None
    
    def _validate_revision_exists(self, revision: str, migration_type: str) -> bool:
        """Validate that a revision exists in migration history"""
        try:
            migration_path = f"migrations/{migration_type}_migrations"
            cfg = Config(f"{migration_path}/alembic.ini")
            script = ScriptDirectory.from_config(cfg)
            
            # Check if revision exists
            try:
                script.get_revision(revision)
                return True
            except:
                return False
        except Exception as e:
            logger.error(f"Failed to validate revision {revision}: {str(e)}")
            return False


# Global instance
migration_manager = ProductionMigrationManager()