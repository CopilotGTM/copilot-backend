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
from src.domain.models.master_tables import Organisation
from src.service.organization_management import organization_management_service

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

    return p.parse_args()


def list_organizations():
    """List all organizations"""
    print("Listing all organizations...")
    db_url = get_database_url()
    engine = create_engine(db_url, future=True)
    session = Session(engine)

    try:
        organizations = organization_management_service.list_organizations(session)

        if not organizations:
            print("No organizations found.")
            return

        print("\n📋 Organizations:")
        print("-" * 80)
        print(f"{'ID':<36} {'Name':<30} {'Schema':<20} {'Status':<10}")
        print("-" * 80)

        for org in organizations:
            status = "Active" if org.is_active else "Inactive"
            schema = org.schema_name or "Not set"
            print(f"{str(org.organisation_id):<36} {org.organisation_name:<30} {schema:<20} {status:<10}")

        print("\nTotal organizations:", len(organizations))

        print("-" * 80)
        return organizations
    except Exception as e:
        print(f"❌ Failed to list organizations: {str(e)}")
    finally:
        session.close()


def run_migrations_for_orgs(org_ids, target_version):
    """Run migrations for specific organizations"""
    db_url = get_database_url()
    engine = create_engine(db_url, future=True)
    session = Session(engine)

    try:
        for org_id in org_ids:
            print(f"\n🔄 Migrating organization: {org_id}")

            # Get organization
            stmt = select(Organisation).where(Organisation.organisation_id == org_id)
            organization = session.exec(stmt).one_or_none()

            if not organization:
                print(f"❌ Organization not found: {org_id}")
                continue

            if not organization.schema_name:
                print(f"❌ Organization has no schema: {org_id}")
                continue

            print(f"📁 Schema: {organization.schema_name}")
            print(f"🎯 Target version: {target_version}")

            try:
                # Run migrations
                organization_management_service.run_organization_migrations(organization.schema_name, target_version)
                print(f"✅ Successfully migrated organization: {org_id}")

            except Exception as e:
                print(f"❌ Failed to migrate organization {org_id}: {str(e)}")

        print("\n🎉 Migration process completed!")

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

    if not args.org_ids:
        print("No organization IDs provided. Use --list-orgs to see available organizations.")
        return

    print(f"🚀 Running organization migrations for {len(args.org_ids)} organization(s)")
    print(f"🎯 Target version: {args.target_version}")

    run_migrations_for_orgs(args.org_ids, args.target_version)


if __name__ == "__main__":
    main()
