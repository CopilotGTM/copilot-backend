#!/usr/bin/env python3
import os
import argparse
import urllib.parse

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

def load_env():
    load_dotenv()

def get_database_url():
    host = os.getenv("DATABASE_HOST", "localhost")
    port = os.getenv("DATABASE_PORT", "5432")
    dbname = os.getenv("DATABASE_NAME", "postgres")
    user = os.getenv("DATABASE_USER", "postgres")
    pwd  = os.getenv("DATABASE_PASSWORD", "")
    enc  = urllib.parse.quote_plus(pwd)
    return os.getenv("DATABASE_URL") or f"postgresql://{user}:{enc}@{host}:{port}/{dbname}"

def parse_args():
    p = argparse.ArgumentParser(
        description="Drop tenant schema, role, and remove record from tenants table"
    )
    p.add_argument(
        "org_ids",
        metavar="ORG_ID",
        nargs="+",
        help="One or more organization IDs to delete",
    )
    return p.parse_args()

def main():
    load_env()
    args = parse_args()
    org_ids = args.org_ids

    db_url = get_database_url()
    engine = create_engine(db_url, future=True)

    for org in org_ids:
        schema = org
        role   = f"{org}_role"

        # 1) Drop schema (cascade)
        with engine.begin() as conn:
            conn.execute(
                text(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            )
        print(f"✅ Schema '{schema}' dropped (if it existed).")

        # 2) Drop role
        with engine.begin() as conn:
            conn.execute(
                text(f"DROP ROLE IF EXISTS {role}")
            )
        print(f"✅ Role '{role}' dropped (if it existed).")

        # 3) Remove from tenants table
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM public.tenants WHERE org_id = :org"),
                {"org": org}
            )
        print(f"✅ Tenant record for '{org}' removed from public.tenants.")

    print("🎉 All specified tenants have been cleaned up.")

if __name__ == "__main__":
    main()
