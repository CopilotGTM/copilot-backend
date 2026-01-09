#!/usr/bin/env python3
"""Test script for purge_calendar_data.py

This script tests the purge functionality without actually deleting data.
It verifies:
1. Database connection
2. Organisation ID retrieval from master database
3. Schema name calculation
4. Schema existence checks
5. Table existence in tenant schemas
6. Export functionality (export-only mode)

Usage:
    python scripts/maintenance/test_purge_calendar_data.py
    python scripts/maintenance/test_purge_calendar_data.py --org-id <UUID>
    python scripts/maintenance/test_purge_calendar_data.py --verbose
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.infra.utils.resource_naming import get_schema_name  # noqa: E402


def load_database_url() -> str:
    """Build the database URL from environment variables."""
    load_dotenv()
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        return db_url

    host = os.getenv("DATABASE_HOST", "localhost")
    port = os.getenv("DATABASE_PORT", "5432")
    name = os.getenv("DATABASE_NAME", "postgres")
    user = os.getenv("DATABASE_USER", "postgres")
    pwd = os.getenv("DATABASE_PASSWORD", "")

    user_encoded = quote_plus(user)
    pwd_encoded = quote_plus(pwd)

    return f"postgresql://{user_encoded}:{pwd_encoded}@{host}:{port}/{name}"


def test_database_connection(engine: Engine) -> bool:
    """Test basic database connectivity."""
    print("\n[TEST] Database Connection")
    print("-" * 40)
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1"))
            row = result.fetchone()
            if row and row[0] == 1:
                print("  OK: Database connection successful")
                return True
    except Exception as e:
        print(f"  FAILED: {e}")
    return False


def test_organisations_table(engine: Engine) -> list[uuid.UUID]:
    """Test querying organisations from master database."""
    print("\n[TEST] Organisations Table (public.organisations)")
    print("-" * 40)
    try:
        with engine.connect() as conn:
            # Check if table exists
            result = conn.execute(text("""
                SELECT COUNT(*) FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'organisations'
            """))
            if result.fetchone()[0] == 0:
                print("  FAILED: Table 'public.organisations' does not exist")
                return []

            # Get organisations
            result = conn.execute(text("SELECT organisation_id, name FROM public.organisations LIMIT 10"))
            rows = result.fetchall()

            print(f"  OK: Found {len(rows)} organisations (showing up to 10)")
            org_ids = []
            for row in rows:
                org_id = uuid.UUID(str(row[0]))
                name = row[1] if len(row) > 1 else "N/A"
                schema = get_schema_name(org_id)
                print(f"      - {str(org_id)[:8]}... ({name}) -> schema: {schema}")
                org_ids.append(org_id)

            return org_ids
    except Exception as e:
        print(f"  FAILED: {e}")
        return []


def test_schema_exists(engine: Engine, schema: str) -> bool:
    """Test if a schema exists."""
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :schema"),
                {"schema": schema}
            )
            return result.fetchone() is not None
    except Exception:
        return False


def test_table_exists(engine: Engine, schema: str, table: str) -> bool:
    """Test if a table exists in a schema."""
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("""
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = :schema AND table_name = :table
                """),
                {"schema": schema, "table": table}
            )
            return result.fetchone() is not None
    except Exception:
        return False


def test_table_count(engine: Engine, schema: str, table: str) -> int:
    """Get row count for a table."""
    try:
        with engine.connect() as conn:
            result = conn.execute(text(f"SELECT COUNT(*) FROM {schema}.{table}"))  # noqa: S608
            return result.fetchone()[0]
    except Exception:
        return -1


def test_tenant_schema(engine: Engine, org_id: uuid.UUID, verbose: bool = False) -> dict:
    """Test a single tenant schema for calendar tables."""
    schema = get_schema_name(org_id)
    results = {
        "org_id": str(org_id),
        "schema": schema,
        "schema_exists": False,
        "tables": {},
    }

    # Check schema exists
    if not test_schema_exists(engine, schema):
        results["error"] = "Schema does not exist"
        return results

    results["schema_exists"] = True

    # Calendar tables to check
    calendar_tables = [
        "calendar_events",
        "calendar_event_attendees",
        "calendar_event_user_states",
        "calendar_event_deal_associations",
        "integration_sync_history",
        "knowledge_integrations",
    ]

    for table in calendar_tables:
        exists = test_table_exists(engine, schema, table)
        count = test_table_count(engine, schema, table) if exists else -1
        results["tables"][table] = {"exists": exists, "count": count}

    return results


def test_all_tenant_schemas(engine: Engine, org_ids: list[uuid.UUID], verbose: bool = False) -> None:
    """Test all tenant schemas for calendar tables."""
    print("\n[TEST] Tenant Schemas & Calendar Tables")
    print("-" * 40)

    total_calendar_events = 0
    schemas_with_data = 0

    for org_id in org_ids:
        result = test_tenant_schema(engine, org_id, verbose)
        schema = result["schema"]

        if not result["schema_exists"]:
            print(f"  {str(org_id)[:8]}: Schema '{schema}' does not exist")
            continue

        calendar_count = result["tables"].get("calendar_events", {}).get("count", 0)
        if calendar_count > 0:
            schemas_with_data += 1
            total_calendar_events += calendar_count

        if verbose or calendar_count > 0:
            print(f"  {str(org_id)[:8]} ({schema}):")
            for table, info in result["tables"].items():
                status = f"{info['count']} rows" if info["exists"] else "NOT FOUND"
                print(f"      {table}: {status}")

    print(f"\n  Summary:")
    print(f"    - Schemas checked: {len(org_ids)}")
    print(f"    - Schemas with calendar data: {schemas_with_data}")
    print(f"    - Total calendar events: {total_calendar_events}")


def test_single_org(engine: Engine, org_id: uuid.UUID) -> None:
    """Test a single organisation in detail."""
    print(f"\n[TEST] Single Organisation: {org_id}")
    print("-" * 40)

    result = test_tenant_schema(engine, org_id, verbose=True)
    schema = result["schema"]

    print(f"  Organisation ID: {org_id}")
    print(f"  Schema Name: {schema}")
    print(f"  Schema Exists: {result['schema_exists']}")

    if not result["schema_exists"]:
        print(f"\n  ERROR: Schema '{schema}' does not exist in the database")
        return

    print(f"\n  Tables:")
    for table, info in result["tables"].items():
        status = f"{info['count']} rows" if info["exists"] else "TABLE NOT FOUND"
        print(f"    {table}: {status}")

    # Test calendar-specific queries
    print(f"\n  Calendar Integration Details:")
    try:
        with engine.connect() as conn:
            # Check for calendar integrations
            result = conn.execute(
                text(f"""
                    SELECT integration_type, COUNT(*)
                    FROM {schema}.knowledge_integrations
                    WHERE integration_type IN ('google_calendar', 'outlook_calendar')
                    GROUP BY integration_type
                """),  # noqa: S608
            )
            rows = result.fetchall()
            if rows:
                for row in rows:
                    print(f"    {row[0]}: {row[1]} integrations")
            else:
                print("    No calendar integrations found")
    except Exception as e:
        print(f"    Error querying integrations: {e}")


def test_export_only_simulation(engine: Engine, org_ids: list[uuid.UUID]) -> None:
    """Simulate export-only mode to verify queries work."""
    print("\n[TEST] Export Query Simulation")
    print("-" * 40)

    for org_id in org_ids[:3]:  # Test first 3 orgs only
        schema = get_schema_name(org_id)

        if not test_schema_exists(engine, schema):
            continue

        print(f"\n  Testing queries for {str(org_id)[:8]} ({schema}):")

        queries = [
            ("calendar_events", f"SELECT COUNT(*) FROM {schema}.calendar_events WHERE organisation_id = :org_id"),
            ("attendees (join)", f"""
                SELECT COUNT(*) FROM {schema}.calendar_event_attendees a
                JOIN {schema}.calendar_events e ON a.event_id = e.event_id
                WHERE e.organisation_id = :org_id
            """),
            ("user_states (join)", f"""
                SELECT COUNT(*) FROM {schema}.calendar_event_user_states s
                JOIN {schema}.calendar_events e ON s.event_id = e.event_id
                WHERE e.organisation_id = :org_id
            """),
            ("deal_associations", f"SELECT COUNT(*) FROM {schema}.calendar_event_deal_associations WHERE organisation_id = :org_id"),
            ("sync_history (calendar)", f"""
                SELECT COUNT(*) FROM {schema}.integration_sync_history h
                JOIN {schema}.knowledge_integrations ki ON h.integration_id = ki.integration_id
                WHERE ki.organisation_id = :org_id
                  AND ki.integration_type IN ('google_calendar', 'outlook_calendar')
            """),
        ]

        for name, query in queries:
            try:
                with engine.connect() as conn:
                    result = conn.execute(text(query), {"org_id": str(org_id)})  # noqa: S608
                    count = result.fetchone()[0]
                    print(f"    {name}: {count} rows")
            except Exception as e:
                print(f"    {name}: ERROR - {e}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test purge_calendar_data.py functionality")
    parser.add_argument(
        "--org-id",
        type=lambda value: uuid.UUID(value),
        help="Test a specific organisation UUID",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed output for all organisations",
    )
    parser.add_argument(
        "--skip-queries",
        action="store_true",
        help="Skip export query simulation",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    print("=" * 60)
    print("PURGE CALENDAR DATA - TEST SCRIPT")
    print("=" * 60)

    # Load database URL
    db_url = load_database_url()
    print(f"\nDatabase: {db_url.split('@')[1] if '@' in db_url else 'configured'}")

    engine = create_engine(db_url, future=True)

    # Test 1: Database connection
    if not test_database_connection(engine):
        print("\nAborting: Cannot connect to database")
        sys.exit(1)

    # Test 2: If specific org_id provided, test just that org
    if args.org_id:
        test_single_org(engine, args.org_id)
        print("\n" + "=" * 60)
        print("TEST COMPLETE")
        print("=" * 60)
        return

    # Test 3: Get organisations from master database
    org_ids = test_organisations_table(engine)
    if not org_ids:
        print("\nNo organisations found. Cannot continue testing.")
        sys.exit(1)

    # Test 4: Check tenant schemas
    test_all_tenant_schemas(engine, org_ids, verbose=args.verbose)

    # Test 5: Simulate export queries
    if not args.skip_queries:
        test_export_only_simulation(engine, org_ids)

    print("\n" + "=" * 60)
    print("TEST COMPLETE")
    print("=" * 60)
    print("\nNext steps:")
    print("  1. Run export-only to verify: python scripts/maintenance/purge_calendar_data.py --export-only")
    print("  2. Check exported JSON files")
    print("  3. Run full purge when ready: python scripts/maintenance/purge_calendar_data.py --yes")


if __name__ == "__main__":
    main()
