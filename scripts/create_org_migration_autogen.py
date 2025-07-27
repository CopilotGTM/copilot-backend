#!/usr/bin/env python3
"""
Create organization migration using autogenerate
This script creates a temporary schema, runs autogenerate, then cleans up
"""
import sys
import os
import subprocess
import time
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
os.chdir(project_root)

from src.infra.utils.logger import get_logger
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
import urllib.parse

# Load environment variables
load_dotenv()

logger = get_logger("create_org_migration")


def get_database_engine():
    """Get database engine using the same approach as env.py"""
    DATABASE_HOST = os.getenv("DATABASE_HOST", "localhost")
    DATABASE_PORT = os.getenv("DATABASE_PORT", "5432")
    DATABASE_NAME = os.getenv("DATABASE_NAME", "postgres")
    DATABASE_USER = os.getenv("DATABASE_USER", "postgres")
    DATABASE_PASSWORD = os.getenv("DATABASE_PASSWORD", "")
    
    encoded_password = urllib.parse.quote_plus(DATABASE_PASSWORD)
    db_url = f"postgresql://{DATABASE_USER}:{encoded_password}@{DATABASE_HOST}:{DATABASE_PORT}/{DATABASE_NAME}"
    
    return create_engine(db_url, echo=False, pool_pre_ping=True)


def create_temp_schema_for_migration(schema_name: str):
    """Create a temporary schema for migration generation"""
    logger.info("Creating temporary schema for migration generation")
    
    temp_schema = schema_name
    
    try:
        engine = get_database_engine()
        
        with engine.begin() as conn:
            # Clean up if exists
            conn.execute(text(f"DROP SCHEMA IF EXISTS {temp_schema} CASCADE"))
            
            # Create schema
            conn.execute(text(f"CREATE SCHEMA {temp_schema}"))
        
        logger.info(f"Temporary schema '{temp_schema}' created successfully")
        return temp_schema
        
    except Exception as e:
        logger.error(f"Failed to create temporary schema: {str(e)}")
        raise


def cleanup_temp_schema(schema_name: str):
    """Clean up the temporary schema"""
    logger.info(f"Cleaning up temporary schema: {schema_name}")
    
    try:
        engine = get_database_engine()
        
        with engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE"))
        
        logger.info("Temporary schema cleaned up successfully")
        
    except Exception as e:
        logger.error(f"Failed to clean up temporary schema: {str(e)}")


def run_alembic_autogenerate(schema_name: str, message: str):
    """Run alembic autogenerate command"""
    logger.info(f"Running alembic autogenerate for schema: {schema_name}")
    
    try:
        # Construct the alembic command
        cmd = [
            "python", "-m", "alembic",
            "-c", "migrations/org_migrations/alembic.ini",
            "-x", f"schema={schema_name}",
            "revision",
            "--autogenerate",
            "-m", message
        ]
        
        logger.info(f"Executing command: {' '.join(cmd)}")
        
        # Run the command
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=project_root
        )
        
        if result.returncode == 0:
            logger.info("Alembic autogenerate completed successfully")
            logger.info(f"STDOUT: {result.stdout}")
            return True
        else:
            logger.error(f"Alembic autogenerate failed with return code: {result.returncode}")
            logger.error(f"STDOUT: {result.stdout}")
            logger.error(f"STDERR: {result.stderr}")
            return False
            
    except Exception as e:
        logger.error(f"Failed to run alembic autogenerate: {str(e)}")
        return False


def run_alembic_upgrade(schema_name: str):
    """Run alembic upgrade head for the given schema."""
    logger.info(f"Running alembic upgrade for schema: {schema_name}")
    
    try:
        # Construct the alembic command
        cmd = [
            "python", "-m", "alembic",
            "-c", "migrations/org_migrations/alembic.ini",
            "-x", f"schema={schema_name}",
            "upgrade",
            "head"
        ]
        
        logger.info(f"Executing command: {' '.join(cmd)}")
        
        # Run the command
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=project_root,
            check=False
        )
        
        if result.returncode == 0:
            logger.info("Alembic upgrade completed successfully")
            logger.info(f"STDOUT: {result.stdout}")
            return True
        else:
            logger.error(f"Alembic upgrade failed with return code: {result.returncode}")
            logger.error(f"STDOUT: {result.stdout}")
            logger.error(f"STDERR: {result.stderr}")
            return False
            
    except Exception as e:
        logger.error(f"Failed to run alembic upgrade: {str(e)}")
        return False
def validate_generated_migration():
    """Validate the generated migration file"""
    logger.info("Validating generated migration file")
    
    try:
        versions_dir = project_root / "migrations" / "org_migrations" / "versions"
        migration_files = sorted(versions_dir.glob("*.py"))
        
        if not migration_files:
            logger.error("No migration files found")
            return False
        
        # Get the latest migration file
        latest_migration = migration_files[-1]
        logger.info(f"Latest migration file: {latest_migration.name}")
        
        # Read and validate the content
        content = latest_migration.read_text()
        
        # Check for required functions
        if "def upgrade() -> None:" not in content:
            logger.error("Migration file missing upgrade() function")
            return False
        
        if "def downgrade() -> None:" not in content:
            logger.error("Migration file missing downgrade() function")
            return False
        
        logger.info("✅ Migration file validation passed")
        return True
        
    except Exception as e:
        logger.error(f"Migration file validation failed: {str(e)}")
        return False


def show_generated_migration_info():
    """Show information about the generated migration"""
    logger.info("Generated migration information:")
    
    try:
        versions_dir = project_root / "migrations" / "org_migrations" / "versions"
        migration_files = sorted(versions_dir.glob("*.py"))
        
        if migration_files:
            latest_migration = migration_files[-1]
            
            # Try to import and get info
            import importlib.util
            spec = importlib.util.spec_from_file_location("migration", latest_migration)
            migration_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration_module)
            
            revision = getattr(migration_module, 'revision', 'Unknown')
            down_revision = getattr(migration_module, 'down_revision', 'Unknown')
            
            logger.info(f"📄 File: {latest_migration.name}")
            logger.info(f"🔢 Revision: {revision}")
            logger.info(f"⬇️  Down revision: {down_revision}")
            
            # Show first few lines of docstring
            if migration_module.__doc__:
                doc_lines = migration_module.__doc__.strip().split('\n')[:3]
                logger.info(f"📝 Description: {doc_lines[0]}")
        
    except Exception as e:
        logger.error(f"Failed to show migration info: {str(e)}")


def main():
    """Main function"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Create organization migration using autogenerate")
    parser.add_argument('message', nargs='?', default="Initial organization tables", help='Migration message')
    parser.add_argument('--keep-temp-schema', action='store_true', help='Keep temporary schema for debugging')
    parser.add_argument('--schema-name', default='test', help='Temporary schema name to use')
    
    args = parser.parse_args()
    
    temp_schema = args.schema_name
    
    try:
        logger.info(f"Creating organization migration: {args.message}")
        
        # Step 1: Create temporary schema
        create_temp_schema_for_migration(temp_schema)
        
        # Step 2: Upgrade schema to head
        if not run_alembic_upgrade(temp_schema):
            logger.error("Failed to upgrade temporary schema")
            return False

        # Step 3: Run alembic autogenerate
        if not run_alembic_autogenerate(temp_schema, args.message):
            logger.error("Failed to generate migration")
            return False
        
        # Step 4: Validate generated migration
        if not validate_generated_migration():
            logger.error("Generated migration validation failed")
            return False
        
        # Step 5: Show migration info
        show_generated_migration_info()
        
        logger.info("🎉 Migration created successfully!")
        logger.info("You can now use this migration to create organization schemas.")
        logger.info("")
        logger.info("Next steps:")
        logger.info("1. Review the generated migration file")
        logger.info("2. Test it by creating an organization via the API")
        logger.info("3. The migration will be automatically applied to new organization schemas")
        
        return True
        
    except Exception as e:
        logger.error(f"Migration creation failed: {str(e)}")
        return False
        
    finally:
        # Cleanup (unless debugging)
        if not args.keep_temp_schema:
            cleanup_temp_schema(temp_schema)
        elif args.keep_temp_schema:
            logger.info(f"Keeping temporary schema '{temp_schema}' for debugging")


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)