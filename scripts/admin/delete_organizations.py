#!/usr/bin/env python3
import os
import sys
import argparse

# ── so we can import your app code under `src/` ──
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
from sqlmodel import Session, select
from src.domain.models.db_models import Organisation
from src.service.organization_management import organization_management_service
from src.infra.utils.database import get_master_engine


def load_env():
    load_dotenv()


def parse_args():
    p = argparse.ArgumentParser(description="Delete organizations and their schemas")
    p.add_argument(
        "org_ids",
        metavar="ORG_ID",
        nargs="+",
        help="One or more organization IDs to delete",
    )
    p.add_argument("--force", action="store_true", help="Force deletion without confirmation")
    p.add_argument("--list-orgs", action="store_true", help="List all organizations and exit")

    return p.parse_args()


def list_organizations():
    """List all organizations"""
    engine = get_master_engine()
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
            print(f"{org.organisation_id:<36} {org.organisation_name:<30} {schema:<20} {status:<10}")

        print("-" * 80)

    except Exception as e:
        print(f"❌ Failed to list organizations: {str(e)}")
    finally:
        session.close()


def delete_organizations(org_ids, force=False):
    """Delete organizations and their schemas"""
    engine = get_master_engine()
    session = Session(engine)

    try:
        for org_id in org_ids:
            print(f"\n🗑️  Deleting organization: {org_id}")

            # Get organization
            stmt = select(Organisation).where(Organisation.organisation_id == org_id)
            organization = session.exec(stmt).one_or_none()

            if not organization:
                print(f"❌ Organization not found: {org_id}")
                continue

            print(f"📁 Organization: {organization.organisation_name}")
            print(f"📁 Schema: {organization.schema_name}")
            print(f"👥 Role: {organization.role_name}")

            if not force:
                confirm = input(f"\n⚠️  Are you sure you want to delete '{organization.organisation_name}'? (yes/no): ")
                if confirm.lower() != "yes":
                    print("❌ Deletion cancelled.")
                    continue

            try:
                # Delete organization (this will also drop schema and role)
                success = organization_management_service.delete_organization(org_id, session)

                if success:
                    print(f"✅ Successfully deleted organization: {org_id}")
                else:
                    print(f"❌ Failed to delete organization: {org_id}")

            except Exception as e:
                print(f"❌ Failed to delete organization {org_id}: {str(e)}")

        print("\n🎉 Deletion process completed!")

    except Exception as e:
        print(f"❌ Failed to delete organizations: {str(e)}")
    finally:
        session.close()


def main():
    load_env()
    args = parse_args()

    if args.list_orgs:
        list_organizations()
        return

    print(f"🗑️  Deleting {len(args.org_ids)} organization(s)")
    if args.force:
        print("⚠️  Force mode enabled - no confirmation required")

    delete_organizations(args.org_ids, args.force)


if __name__ == "__main__":
    main()
