#!/usr/bin/env python3
import os
import sys
import argparse

# ── so we can import your app code under `src/` ──
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
from sqlmodel import Session, create_engine
from src.service.organization_management import organization_management_service
from src.infra.utils.database import get_master_engine


def load_env():
    load_dotenv()


def parse_args():
    p = argparse.ArgumentParser(description="Create a new organization with schema, role, and admin user")
    p.add_argument("--name", required=True, help="Organization name")
    p.add_argument("--admin-email", required=True, help="Admin user email")
    p.add_argument("--admin-first-name", required=True, help="Admin user first name")
    p.add_argument("--admin-last-name", required=True, help="Admin user last name")
    p.add_argument("--country", help="Organization country")
    p.add_argument("--sales-team-size", type=int, help="Sales team size")
    p.add_argument("--company-address", help="Company address")
    p.add_argument("--billing-address", help="Billing address")
    p.add_argument("--linked-urls", help="Linked URLs (comma-separated)")
    p.add_argument("--website-urls", help="Website URLs (comma-separated)")
    p.add_argument("--summary", help="Organization summary")
    p.add_argument("--logo-url", help="Logo URL")

    return p.parse_args()


def main():
    load_env()
    args = parse_args()

    # Prepare organization details
    org_details = {}
    if args.country:
        org_details["country"] = args.country
    if args.sales_team_size:
        org_details["sales_team_size"] = args.sales_team_size
    if args.company_address:
        org_details["company_address"] = args.company_address
    if args.billing_address:
        org_details["billing_address"] = args.billing_address
    if args.linked_urls:
        org_details["linked_urls"] = args.linked_urls
    if args.website_urls:
        org_details["website_urls"] = args.website_urls
    if args.summary:
        org_details["summary"] = args.summary
    if args.logo_url:
        org_details["logo_url"] = args.logo_url

    # Get database session
    engine = get_master_engine()
    session = Session(engine)

    try:
        print(f"Creating organization: {args.name}")
        print(f"Admin user: {args.admin_first_name} {args.admin_last_name} ({args.admin_email})")

        # Create organization
        result = organization_management_service.create_organization(
            organisation_name=args.name,
            admin_email=args.admin_email,
            admin_first_name=args.admin_first_name,
            admin_last_name=args.admin_last_name,
            session=session,
            **org_details,
        )

        organization = result["organization"]
        admin_user = result["admin_user"]
        schema_name = result["schema_name"]
        role_name = result["role_name"]
        role_password = result["role_password"]

        print("\n✅ Organization created successfully!")
        print(f"Organization ID: {organization.organisation_id}")
        print(f"Schema Name: {schema_name}")
        print(f"Role Name: {role_name}")
        print(f"Role Password: {role_password}")
        print(f"Admin User ID: {admin_user.user_id}")
        print(f"Admin Email: {admin_user.email}")

        print("\n📝 Next steps:")
        print("1. Store the role password securely (Azure Key Vault)")
        print("2. Create Supabase user for the admin")
        print("3. Update the admin user with supabase_user_id")
        print("4. Send welcome email to admin user")

    except Exception as e:
        print(f"❌ Failed to create organization: {str(e)}")
        session.rollback()
        sys.exit(1)
    finally:
        session.close()


if __name__ == "__main__":
    main()
