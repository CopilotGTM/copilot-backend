#!/usr/bin/env python3
import os
import sys
import argparse

# ── so we can import your app code under `src/` ──
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
import urllib.parse
from sqlmodel import Session, select
from sqlalchemy import create_engine
from src.domain.models.master_tables import Organisation, OrganisationStatus
from migrations.migration_manager import migration_manager
from src.infra.utils.resource_naming import get_schema_name

load_dotenv()

def get_database_url():
    host = os.getenv("DATABASE_HOST", "localhost")
    port = os.getenv("DATABASE_PORT", "5432")
    name = os.getenv("DATABASE_NAME", "postgres")
    user = os.getenv("DATABASE_USER", "postgres")
    pwd = os.getenv("DATABASE_PASSWORD", "")
    enc = urllib.parse.quote_plus(pwd)
    return os.getenv("DATABASE_URL") or f"postgresql://{user}:{enc}@{host}:{port}/{name}"

def parse_args():

    print("Running organization migrations script...")
    print("This script migrates organization schemas to the latest version.")
    p = argparse.ArgumentParser(description="Run organization-level migrations for specific organizations")
    p.add_argument(
        "org_ids",
        metavar="ORG_ID",
        nargs="*",
        default=[],
        help="One or more organization IDs to migrate. If none are provided, no migrations will run unless --list-orgs is used.",
    )
    p.add_argument("--target-version", default="head", help="Target migration version (default: head)")
    p.add_argument("--list-orgs", action="store_true", help="List all organizations and exit")
    p.add_argument("--all", action="store_true", help="Run migrations for all organizations (overrides explicit IDs if provided)")
    p.add_argument("--precheck", action="store_true", help="Only show which organizations would be migrated and current vs target revision")
    p.add_argument("--fail-fast", action="store_true", help="Abort remaining migrations after first failure")

    return p.parse_args()


def list_organizations():
    """List all organizations and show derived schema + existence."""
    print("Listing all organizations...")
    db_url = get_database_url()
    engine = create_engine(db_url, future=True)
    session = Session(engine)
    try:
        stmt = select(Organisation)
        organizations = session.exec(stmt).all()
        if not organizations:
            print("No organizations found.")
            return []
        print("\n📋 Organizations (derived schemas):")
        print("-" * 140)
        print(f"{'ID':<36} {'Name':<26} {'Schema':<22} {'Exists':<8} {'Status':<12} {'Created':<19}")
        print("-" * 140)
        from sqlalchemy import text as sa_text
        with engine.connect() as conn:
            for org in organizations:
                schema = get_schema_name(org.organisation_id)
                exists = False
                try:
                    res = conn.execute(sa_text("SELECT 1 FROM information_schema.schemata WHERE schema_name=:s"), {"s": schema})
                    exists = res.fetchone() is not None
                except Exception:
                    exists = False
                status = getattr(org.status, 'value', str(org.status))
                created = getattr(org, 'created_at', None)
                created_str = created.strftime('%Y-%m-%d %H:%M:%S') if created else '-'
                print(f"{str(org.organisation_id):<36} {org.organisation_name[:26]:<26} {schema:<22} {str(exists):<8} {status:<12} {created_str:<19}")
        print("-" * 140)
        print("Total organizations:", len(organizations))
        return organizations
    except Exception as e:
        print(f"❌ Failed to list organizations: {e}")
        return []
    finally:
        session.close()


def run_migrations_for_orgs(org_ids, target_version, migrate_all=False, precheck=False, fail_fast=False):
    """Run migrations for specified or all organizations.

    We derive schema names from organisation_id via get_schema_name; no schema column exists on master table.
    Only organizations not in FAILED state are migrated unless explicitly specified by ID.
    """
    db_url = get_database_url()
    engine = create_engine(db_url, future=True)
    session = Session(engine)

    migrated = []
    failed = []

    try:
        target_orgs = []
        if migrate_all:
            print("Fetching all organizations (deriving schemas)...")
            stmt_all = select(Organisation).where(Organisation.status != OrganisationStatus.FAILED)
            target_orgs = session.exec(stmt_all).all()
            if not target_orgs:
                print("No organizations found.")
                return
        else:
            # Fetch each specified org
            for org_id in org_ids:
                stmt = select(Organisation).where(Organisation.organisation_id == org_id)
                org = session.exec(stmt).one_or_none()
                if not org:
                    print(f"❌ Organization not found: {org_id}")
                else:
                    target_orgs.append(org)

        if not target_orgs:
            print("No target organizations resolved. Exiting.")
            return

        print(f"Resolved {len(target_orgs)} organization(s) to migrate.")

        # Precheck: gather current revisions
        if precheck:
            print("\n🔍 Precheck (no changes will be made):")
            from sqlalchemy import text as sa_text
            with engine.connect() as conn:
                for organization in target_orgs:
                    schema = get_schema_name(organization.organisation_id)
                    current = None
                    try:
                        res = conn.execute(sa_text(f"SELECT version_num FROM {schema}.alembic_version LIMIT 1"))
                        row = res.fetchone()
                        current = row[0] if row else None
                    except Exception as e:
                        current = f"error: {e}"  # show error but continue
                    print(f" - {schema}: current={current} target={target_version}")
            print("\n(No migrations executed due to --precheck flag.)")
            return

        from sqlalchemy import text as sa_text
        for organization in target_orgs:
            org_id = organization.organisation_id
            print(f"\n🔄 Migrating organization: {org_id}")
            schema_name = get_schema_name(org_id)
            # Verify schema exists (skip if missing)
            schema_exists = False
            try:
                with engine.connect() as conn:
                    res = conn.execute(sa_text("SELECT 1 FROM information_schema.schemata WHERE schema_name=:s"), {"s": schema_name})
                    schema_exists = res.fetchone() is not None
            except Exception as e:
                print(f"⚠️ Could not verify schema existence for {schema_name}: {e}")

            if not schema_exists:
                print(f"❌ Schema '{schema_name}' does not exist (skipping)")
                failed.append(org_id)
                if fail_fast:
                    print("⛔ Fail-fast enabled: stopping further migrations.")
                    break
                continue

            print(f"📁 Schema: {schema_name}")
            print(f"🎯 Target version: {target_version}")
            try:
                result = migration_manager.run_organization_migration(
                    schema_name=schema_name,
                    target_revision=target_version,
                    validate_schema=True,
                )
                if result.success:
                    print(f"✅ Successfully migrated organization: {org_id} in {result.duration:.2f}s")
                    migrated.append(org_id)
                else:
                    print(f"❌ Migration failed for organization {org_id}: {result.error}")
                    failed.append(org_id)
                    if fail_fast:
                        print("⛔ Fail-fast enabled: stopping further migrations.")
                        break
            except Exception as e:
                print(f"❌ Exception during migration for organization {org_id}: {str(e)}")
                failed.append(org_id)
                if fail_fast:
                    print("⛔ Fail-fast enabled: stopping further migrations.")
                    break

        print("\n🎉 Migration process completed!")
        print(f"✅ Success: {len(migrated)} | ❌ Failed: {len(failed)}")
        if migrated:
            print("Succeeded:", ", ".join(str(i) for i in migrated))
        if failed:
            print("Failed:   ", ", ".join(str(i) for i in failed))
    except Exception as e:
        print(f"❌ Failed to run migrations: {str(e)}")
    finally:
        session.close()


def main():
    print("Starting organization migrations script...")
    args = parse_args()

    if args.list_orgs:
        list_organizations()
        return

    migrate_all = args.all
    if not args.org_ids and not migrate_all:
        print("No organization IDs provided. Use --list-orgs to see available organizations or --all to migrate all.")
        return

    if migrate_all:
        print("🚀 Running organization migrations for ALL organizations")
    else:
        print(f"🚀 Running organization migrations for {len(args.org_ids)} organization(s)")
    print(f"🎯 Target version: {args.target_version}")

    run_migrations_for_orgs(
        args.org_ids,
        args.target_version,
        migrate_all=migrate_all,
        precheck=args.precheck,
        fail_fast=args.fail_fast,
    )


if __name__ == "__main__":
    main()
