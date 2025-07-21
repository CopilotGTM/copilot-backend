#!/usr/bin/env python3
"""
Smart deployment script that detects when migrations need to be run.
"""

import os
import sys
import subprocess
import argparse
from pathlib import Path
from typing import List, Tuple, Optional

# Add project root to path
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from alembic.config import Config
from alembic import command
from sqlalchemy import create_engine, text
from dotenv import load_dotenv


class MigrationDetector:
    """Detects when migrations need to be run based on code changes and database state."""

    def __init__(self):
        self.project_root = PROJECT_ROOT
        self.migrations_dir = self.project_root / "migrations"
        self.master_migrations_dir = self.migrations_dir / "master_migrations"
        self.org_migrations_dir = self.migrations_dir / "org_migrations"

    def get_git_changes(self, since_commit: str = "HEAD~1") -> List[str]:
        """Get list of changed files since the specified commit."""
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", since_commit], capture_output=True, text=True, cwd=self.project_root
            )
            if result.returncode == 0:
                return [line.strip() for line in result.stdout.splitlines() if line.strip()]
            return []
        except Exception as e:
            print(f"⚠️  Warning: Could not get git changes: {e}")
            return []

    def has_migration_changes(self, changed_files: List[str]) -> bool:
        """Check if any migration files have changed."""
        migration_patterns = [
            "migrations/master_migrations/versions/",
            "migrations/org_migrations/versions/",
            "src/domain/models/",  # Model changes might need migrations
        ]

        for file_path in changed_files:
            for pattern in migration_patterns:
                if pattern in file_path:
                    return True
        return False

    def get_current_db_version(self, config_path: str) -> Optional[str]:
        """Get current database migration version."""
        try:
            cfg = Config(config_path)
            result = subprocess.run(
                ["alembic", "-c", config_path, "current"], capture_output=True, text=True, cwd=self.project_root
            )
            if result.returncode == 0:
                # Parse the current version from output
                lines = result.stdout.strip().split("\n")
                for line in lines:
                    if line.strip():
                        return line.strip().split()[0]
            return None
        except Exception as e:
            print(f"⚠️  Warning: Could not get current DB version: {e}")
            return None

    def get_latest_migration_version(self, config_path: str) -> Optional[str]:
        """Get the latest migration version available."""
        try:
            cfg = Config(config_path)
            result = subprocess.run(
                ["alembic", "-c", config_path, "heads"], capture_output=True, text=True, cwd=self.project_root
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split("\n")
                for line in lines:
                    if line.strip():
                        return line.strip().split()[0]
            return None
        except Exception as e:
            print(f"⚠️  Warning: Could not get latest migration version: {e}")
            return None

    def needs_master_migrations(self) -> bool:
        """Check if master migrations need to be run."""
        current_version = self.get_current_db_version(str(self.master_migrations_dir / "alembic.ini"))
        latest_version = self.get_latest_migration_version(str(self.master_migrations_dir / "alembic.ini"))

        if current_version is None or latest_version is None:
            print("⚠️  Could not determine migration versions - assuming migrations needed")
            return True

        return current_version != latest_version

    def run_master_migrations(self) -> bool:
        """Run master migrations."""
        try:
            print("📊 Running master migrations...")
            config_path = str(self.master_migrations_dir / "alembic.ini")
            command.upgrade(Config(config_path), "head")
            print("✅ Master migrations completed successfully")
            return True
        except Exception as e:
            print(f"❌ Master migrations failed: {e}")
            return False

    def run_organization_migrations(self, org_ids: List[str] = None) -> bool:
        """Run organization migrations."""
        try:
            if org_ids:
                print(f"🏢 Running organization migrations for: {', '.join(org_ids)}")
                # Use the organization management service
                from src.service.organization_management import organization_management_service
                from src.infra.utils.database import get_master_session
                from sqlmodel import Session

                engine = get_master_session()
                session = Session(engine)

                for org_id in org_ids:
                    try:
                        # Get organization by ID or schema name
                        from src.domain.models.db_models import Organisation
                        from sqlmodel import select

                        stmt = select(Organisation).where(Organisation.organisation_id == org_id)
                        org = session.exec(stmt).one_or_none()

                        if org and org.schema_name:
                            organization_management_service.run_organization_migrations(org.schema_name, "head")
                            print(f"✅ Migrations completed for organization: {org_id}")
                        else:
                            print(f"⚠️  Organization not found or no schema: {org_id}")
                    except Exception as e:
                        print(f"❌ Failed to migrate organization {org_id}: {e}")

                session.close()
            else:
                print("🏢 Running organization migrations for all organizations...")
                # Run for all organizations
                subprocess.run(
                    ["python", "scripts/run_org_migrations.py", "--all-organizations"],
                    cwd=self.project_root,
                    check=True,
                )

            print("✅ Organization migrations completed successfully")
            return True
        except Exception as e:
            print(f"❌ Organization migrations failed: {e}")
            return False


def main():
    parser = argparse.ArgumentParser(description="Smart deployment with migration detection")
    parser.add_argument("--since", default="HEAD~1", help="Git commit to compare against")
    parser.add_argument("--force-migrations", action="store_true", help="Force run migrations")
    parser.add_argument("--skip-migrations", action="store_true", help="Skip migrations entirely")
    parser.add_argument("--orgs", nargs="+", help="Specific organizations to migrate")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without running")

    args = parser.parse_args()

    # Load environment
    load_dotenv()

    detector = MigrationDetector()

    print("🔍 Analyzing deployment requirements...")

    # Check for migration changes
    changed_files = detector.get_git_changes(args.since)
    has_migration_changes = detector.has_migration_changes(changed_files)

    print(f"📝 Changed files: {len(changed_files)}")
    if has_migration_changes:
        print("🔄 Migration files changed detected")
    else:
        print("📄 No migration changes detected")

    # Determine if migrations are needed
    needs_master = detector.needs_master_migrations()

    print(f"📊 Master migrations needed: {needs_master}")

    # Decision logic
    should_run_migrations = (
        args.force_migrations or has_migration_changes or needs_master
    ) and not args.skip_migrations

    if args.dry_run:
        print("\n🔍 DRY RUN - What would be done:")
        print(f"   - Run master migrations: {should_run_migrations}")
        print(f"   - Run org migrations: {should_run_migrations}")
        print(f"   - Target orgs: {args.orgs or 'all'}")
        return

    if should_run_migrations:
        print("\n🚀 Running migrations...")

        # Run master migrations first
        if not detector.run_master_migrations():
            print("❌ Deployment failed - master migrations failed")
            sys.exit(1)

        # Run organization migrations
        if not detector.run_organization_migrations(args.orgs):
            print("❌ Deployment failed - organization migrations failed")
            sys.exit(1)

        print("✅ All migrations completed successfully")
    else:
        print("⏭️  Skipping migrations - no changes detected")

    print("🎉 Deployment ready to proceed!")


if __name__ == "__main__":
    main()
