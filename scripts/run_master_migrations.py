#!/usr/bin/env python3
import os
import sys

# ── so we can import your app code under `src/` ──
SCRIPT_DIR   = os.path.dirname(os.path.realpath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
sys.path.insert(0, PROJECT_ROOT)

import argparse
import urllib.parse

from dotenv import load_dotenv
from sqlalchemy import create_engine
from alembic.config import Config
from alembic import command


def load_env():
    load_dotenv()


def get_database_url():
    host = os.getenv("DATABASE_HOST", "localhost")
    port = os.getenv("DATABASE_PORT", "5432")
    name = os.getenv("DATABASE_NAME", "postgres")
    user = os.getenv("DATABASE_USER", "postgres")
    pwd  = os.getenv("DATABASE_PASSWORD", "")
    enc  = urllib.parse.quote_plus(pwd)
    return os.getenv("DATABASE_URL") or f"postgresql://{user}:{enc}@{host}:{port}/{name}"


def parse_args():
    p = argparse.ArgumentParser(
        description="Run master migrations (organizations, users, etc.)"
    )
    p.add_argument(
        "--revision",
        default="head",
        help="Revision to upgrade to (default: head)"
    )
    p.add_argument(
        "--downgrade",
        action="store_true",
        help="Downgrade instead of upgrade"
    )
    return p.parse_args()


def run_master_migrations(revision="head", downgrade=False):
    """Run master migrations using the master_migrations directory."""
    cfg = Config("migrations/master_migrations/alembic.ini")
    
    print(f"→ Running master migrations to revision '{revision}'...")
    
    if downgrade:
        command.downgrade(cfg, revision)
        print(f"✅ Master migrations downgraded to '{revision}'")
    else:
        command.upgrade(cfg, revision)
        print(f"✅ Master migrations upgraded to '{revision}'")


def main():
    load_env()
    args = parse_args()
    
    # Verify database connection
    db_url = get_database_url()
    engine = create_engine(db_url, future=True)
    
    try:
        with engine.connect() as conn:
            conn.execute("SELECT 1")
        print("✅ Database connection successful")
    except Exception as e:
        print(f"❌ Database connection failed: {e}")
        sys.exit(1)
    
    # Run master migrations
    run_master_migrations(args.revision, args.downgrade)
    
    print("🎉 Master migrations completed successfully!")


if __name__ == "__main__":
    main() 