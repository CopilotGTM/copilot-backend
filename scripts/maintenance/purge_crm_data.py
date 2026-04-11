#!/usr/bin/env python3
"""Utility script to export and purge CRM data.

This script exports all CRM-related data to JSON files before deletion,
allowing for data comparison and recovery if needed.

Tables affected (in deletion order):
- crm_deal_stage_history
- crm_deal_participants
- crm_activities
- crm_deals
- crm_contacts
- crm_companies
- crm_deal_stages
- crm_pipelines
- crm_users
- integration_sync_history (for CRM integrations only)

Also resets:
- last_synced_at, last_sync_status, last_sync_error on CRM integrations
- crm_{entity}_last_sync_at timestamps in connection_metadata

Examples
--------
- Export and delete all CRM data:
    python scripts/maintenance/purge_crm_data.py --yes

- Export and delete for a specific organisation:
    python scripts/maintenance/purge_crm_data.py --org-id <UUID> --yes

- Export only (no deletion):
    python scripts/maintenance/purge_crm_data.py --export-only

- Specify custom output directory:
    python scripts/maintenance/purge_crm_data.py --output-dir ./my_exports --yes
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.infra.utils.resource_naming import get_schema_name  # noqa: E402


# CRM integration types
CRM_INTEGRATION_TYPES = ("hubspot", "salesforce")

# CRM tables in FK deletion order
CRM_TABLES_DELETE_ORDER = [
    # Tables that reference crm_deals
    "crm_deal_stage_history",
    "crm_deal_participants",
    "deal_stakeholders",  # FK to crm_deals
    "messaging_deal_links",  # FK to crm_deals
    "deal_email_associations",  # FK to crm_deals
    "crm_activities",
    # Main CRM entities
    "crm_deals",
    "crm_contacts",
    "crm_companies",
    # Reference tables (deleted after entities that reference them)
    "crm_deal_stages",
    "crm_pipelines",
    "crm_users",
]

# CRM entity types for timestamp reset
CRM_ENTITY_TYPES = ["deals", "pipelines", "contacts", "companies", "users"]


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export and delete CRM data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--org-id",
        type=lambda value: str(uuid.UUID(value)),
        help="Optional organisation UUID to scope the purge. "
        "If omitted, data from all organisations will be processed.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt and execute immediately.",
    )
    parser.add_argument(
        "--schema",
        type=str,
        help="Optional schema name to target (e.g., org_<uuid>). "
        "If omitted, defaults to the database search_path.",
    )
    parser.add_argument(
        "--export-only",
        action="store_true",
        help="Only export data to JSON, do not delete.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="./crm_data_exports",
        help="Directory to save exported JSON files (default: ./crm_data_exports).",
    )
    return parser.parse_args()


def confirm(org_id: str | None, schema: str | None, auto_confirm: bool, export_only: bool) -> None:
    if auto_confirm:
        return

    if schema:
        scope = f"schema '{schema}'"
    elif org_id:
        scope = f"organisation {org_id}"
    else:
        scope = "ALL organisations (will iterate over all tenant schemas)"

    action = "export" if export_only else "export and DELETE"
    prompt = (
        f"This will {action} CRM data for {scope}.\n"
        f"Tables: {', '.join(CRM_TABLES_DELETE_ORDER)}, integration_sync_history (CRM only)\n"
        f"Continue? [y/N]: "
    )
    response = input(prompt).strip().lower()
    if response not in {"y", "yes"}:
        print("Aborted.")
        sys.exit(0)


def qualify(table: str, schema: str | None) -> str:
    """Return fully qualified table name if schema is provided."""
    if schema:
        if not schema.replace("_", "").isalnum():
            raise ValueError(f"Unsafe schema name: {schema}")
        return f"{schema}.{table}"
    return table


def json_serializer(obj: Any) -> Any:
    """Custom JSON serializer for objects not serializable by default."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def export_table_to_json(
    engine: Engine,
    table_name: str,
    output_path: Path,
    org_id: str | None,
    schema: str | None,
    where_clause: str | None = None,
) -> int:
    """Export a table to JSON file and return row count."""
    qualified_table = qualify(table_name, schema)

    # Build query
    if where_clause:
        query = f"SELECT * FROM {qualified_table} WHERE {where_clause}"  # noqa: S608
    elif org_id:
        query = f"SELECT * FROM {qualified_table} WHERE organisation_id = :org_id"  # noqa: S608
    else:
        query = f"SELECT * FROM {qualified_table}"  # noqa: S608

    params = {"org_id": org_id} if org_id and not where_clause else {}

    with engine.connect() as connection:
        result = connection.execute(text(query), params)
        rows = result.mappings().all()

        # Convert to list of dicts
        data = [dict(row) for row in rows]

        # Write to JSON
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=json_serializer)

        return len(data)


def export_crm_sync_history(
    engine: Engine,
    output_path: Path,
    org_id: str | None,
    schema: str | None,
) -> int:
    """Export integration_sync_history for CRM integrations only."""
    history_table = qualify("integration_sync_history", schema)
    integrations_table = qualify("knowledge_integrations", schema)

    # Build query to get sync history for CRM integrations
    if org_id:
        query = f"""
            SELECT h.*
            FROM {history_table} h
            JOIN {integrations_table} ki ON h.integration_id = ki.integration_id
            WHERE ki.organisation_id = :org_id
              AND ki.integration_type IN :integration_types
        """  # noqa: S608
        params: dict[str, Any] = {"org_id": org_id, "integration_types": CRM_INTEGRATION_TYPES}
    else:
        query = f"""
            SELECT h.*
            FROM {history_table} h
            JOIN {integrations_table} ki ON h.integration_id = ki.integration_id
            WHERE ki.integration_type IN :integration_types
        """  # noqa: S608
        params = {"integration_types": CRM_INTEGRATION_TYPES}

    with engine.connect() as connection:
        result = connection.execute(text(query), params)
        rows = result.mappings().all()

        data = [dict(row) for row in rows]

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=json_serializer)

        return len(data)


def get_all_organisation_ids(engine: Engine) -> list[uuid.UUID]:
    """Query master database for all organisation IDs."""
    query = "SELECT organisation_id FROM public.organisations"
    with engine.connect() as connection:
        result = connection.execute(text(query))
        return [uuid.UUID(str(row[0])) for row in result]


def schema_exists(engine: Engine, schema: str) -> bool:
    """Check if a schema exists in the database."""
    query = "SELECT 1 FROM information_schema.schemata WHERE schema_name = :schema"
    with engine.connect() as connection:
        result = connection.execute(text(query), {"schema": schema})
        return result.fetchone() is not None


def table_exists(engine: Engine, table_name: str, schema: str | None) -> bool:
    """Check if a table exists in the database."""
    if schema:
        query = """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = :schema AND table_name = :table
        """
        params = {"schema": schema, "table": table_name}
    else:
        query = """
            SELECT 1 FROM information_schema.tables
            WHERE table_name = :table
        """
        params = {"table": table_name}

    with engine.connect() as connection:
        result = connection.execute(text(query), params)
        return result.fetchone() is not None


def export_single_org_data(
    engine: Engine,
    org_id: str,
    schema: str,
    base_dir: Path,
) -> dict[str, int]:
    """Export CRM data for a single organisation/schema."""
    counts = {}

    for table_name in CRM_TABLES_DELETE_ORDER:
        if not table_exists(engine, table_name, schema):
            print(f"    Table {table_name} does not exist, skipping...")
            counts[table_name] = 0
            continue

        print(f"    Exporting {table_name}...", end=" ", flush=True)
        try:
            count = export_table_to_json(
                engine,
                table_name,
                base_dir / f"{table_name}.json",
                org_id,
                schema,
            )
            counts[table_name] = count
            print(f"{count} records")
        except Exception as e:
            print(f"ERROR: {e}")
            counts[table_name] = 0

    # Export integration_sync_history (CRM only)
    print("    Exporting integration_sync_history (CRM)...", end=" ", flush=True)
    try:
        count = export_crm_sync_history(
            engine, base_dir / "integration_sync_history_crm.json", org_id, schema
        )
        counts["integration_sync_history"] = count
        print(f"{count} records")
    except Exception as e:
        print(f"ERROR: {e}")
        counts["integration_sync_history"] = 0

    return counts


def export_all_data(
    engine: Engine,
    org_id: str | None,
    schema: str | None,
    output_dir: str,
) -> dict[str, int]:
    """Export all CRM-related data to JSON files."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # If org_id is provided, use single org export
    if org_id and schema:
        org_suffix = f"_org_{org_id[:8]}"
        base_dir = Path(output_dir) / f"crm_export_{timestamp}{org_suffix}"

        print(f"\nExporting CRM data to: {base_dir}")
        counts = export_single_org_data(engine, org_id, schema, base_dir)

        # Write summary
        summary = {
            "exported_at": datetime.now().isoformat(),
            "org_id": org_id,
            "schema": schema,
            "counts": counts,
            "total_records": sum(counts.values()),
        }
        with open(base_dir / "export_summary.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

        print(f"\nExport complete. Total records: {sum(counts.values())}")
        print(f"Summary saved to: {base_dir / 'export_summary.json'}")

        return counts

    # No org_id provided - iterate over all organisations
    base_dir = Path(output_dir) / f"crm_export_{timestamp}_all_orgs"
    print(f"\nExporting CRM data for ALL organisations to: {base_dir}")

    # Get all organisation IDs from master database
    print("  Fetching all organisation IDs from master database...")
    org_ids = get_all_organisation_ids(engine)
    print(f"  Found {len(org_ids)} organisations")

    total_counts: dict[str, int] = {table: 0 for table in CRM_TABLES_DELETE_ORDER}
    total_counts["integration_sync_history"] = 0
    org_summaries = []

    for org_uuid in org_ids:
        org_id_str = str(org_uuid)
        org_schema = get_schema_name(org_uuid)

        # Check if schema exists
        if not schema_exists(engine, org_schema):
            print(f"\n  Org {org_id_str[:8]}: Schema '{org_schema}' does not exist, skipping...")
            continue

        org_dir = base_dir / f"org_{org_id_str[:8]}"
        print(f"\n  Org {org_id_str[:8]} (schema: {org_schema}):")

        try:
            org_counts = export_single_org_data(engine, org_id_str, org_schema, org_dir)

            # Aggregate counts
            for table, count in org_counts.items():
                total_counts[table] = total_counts.get(table, 0) + count

            org_summaries.append(
                {
                    "org_id": org_id_str,
                    "schema": org_schema,
                    "counts": org_counts,
                    "total_records": sum(org_counts.values()),
                }
            )
        except Exception as e:
            print(f"    ERROR: {e}")
            org_summaries.append(
                {
                    "org_id": org_id_str,
                    "schema": org_schema,
                    "error": str(e),
                }
            )

    # Write overall summary
    summary = {
        "exported_at": datetime.now().isoformat(),
        "organisations_processed": len(org_ids),
        "organisations_exported": len([s for s in org_summaries if "error" not in s]),
        "total_counts": total_counts,
        "total_records": sum(total_counts.values()),
        "per_organisation": org_summaries,
    }
    with open(base_dir / "export_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(f"\n\nExport complete. Total records: {sum(total_counts.values())}")
    print(f"Summary saved to: {base_dir / 'export_summary.json'}")

    return total_counts


def delete_single_org_data(engine: Engine, org_id: str, schema: str) -> dict[str, int]:
    """Delete CRM data for a single organisation/schema."""
    counts = {}
    integrations_table = qualify("knowledge_integrations", schema)

    # Delete CRM tables in FK order - each table in its own transaction
    for table_name in CRM_TABLES_DELETE_ORDER:
        if not table_exists(engine, table_name, schema):
            print(f"    Table {table_name} does not exist, skipping...")
            counts[table_name] = 0
            continue

        print(f"    Deleting {table_name}...", end=" ", flush=True)
        qualified_table = qualify(table_name, schema)
        query = f"DELETE FROM {qualified_table} WHERE organisation_id = :org_id"  # noqa: S608
        try:
            with engine.begin() as connection:
                result = connection.execute(text(query), {"org_id": org_id})
                counts[table_name] = result.rowcount
                print(f"{result.rowcount} deleted")
        except Exception as e:
            print(f"ERROR: {e}")
            counts[table_name] = 0

    # Delete integration_sync_history for CRM integrations
    print("    Deleting integration_sync_history (CRM)...", end=" ", flush=True)
    history_table = qualify("integration_sync_history", schema)
    query = f"""
        DELETE FROM {history_table}
        WHERE integration_id IN (
            SELECT integration_id FROM {integrations_table}
            WHERE organisation_id = :org_id
              AND integration_type IN ('hubspot', 'salesforce')
        )
    """  # noqa: S608
    try:
        with engine.begin() as connection:
            result = connection.execute(text(query), {"org_id": org_id})
            counts["integration_sync_history"] = result.rowcount
            print(f"{result.rowcount} deleted")
    except Exception as e:
        print(f"ERROR: {e}")
        counts["integration_sync_history"] = 0

    # Reset last_synced_at, last_sync_status, last_sync_error on CRM integrations
    print("    Resetting integration status...", end=" ", flush=True)
    query = f"""
        UPDATE {integrations_table}
        SET last_synced_at = NULL,
            last_sync_status = NULL,
            last_sync_error = NULL
        WHERE organisation_id = :org_id
          AND integration_type IN ('hubspot', 'salesforce')
    """  # noqa: S608
    try:
        with engine.begin() as connection:
            result = connection.execute(text(query), {"org_id": org_id})
            counts["integrations_status_reset"] = result.rowcount
            print(f"{result.rowcount} reset")
    except Exception as e:
        print(f"ERROR: {e}")
        counts["integrations_status_reset"] = 0

    # Remove crm_*_last_sync_at timestamps from connection_metadata
    print("    Clearing CRM timestamps from connection_metadata...", end=" ", flush=True)
    # Build JSONB removal for all CRM entity timestamps
    # PostgreSQL JSONB syntax: connection_metadata - 'key1' - 'key2' - 'key3'
    removal_keys = " - ".join([f"'crm_{entity}_last_sync_at'" for entity in CRM_ENTITY_TYPES])
    query = f"""
        UPDATE {integrations_table}
        SET connection_metadata = connection_metadata - {removal_keys}
        WHERE organisation_id = :org_id
          AND integration_type IN ('hubspot', 'salesforce')
          AND connection_metadata IS NOT NULL
    """  # noqa: S608
    try:
        with engine.begin() as connection:
            result = connection.execute(text(query), {"org_id": org_id})
            counts["integrations_metadata_reset"] = result.rowcount
            print(f"{result.rowcount} updated")
    except Exception as e:
        print(f"ERROR: {e}")
        counts["integrations_metadata_reset"] = 0

    return counts


def delete_crm_data(engine: Engine, org_id: str | None, schema: str | None) -> dict[str, int]:
    """Delete all CRM-related data in the correct order (respecting FK constraints)."""

    # If org_id and schema are provided, delete for single org
    if org_id and schema:
        print(f"\nDeleting CRM data for schema '{schema}'...")
        return delete_single_org_data(engine, org_id, schema)

    # No org_id provided - iterate over all organisations
    print("\nDeleting CRM data for ALL organisations...")
    print("  Fetching all organisation IDs from master database...")
    org_ids = get_all_organisation_ids(engine)
    print(f"  Found {len(org_ids)} organisations")

    total_counts: dict[str, int] = {table: 0 for table in CRM_TABLES_DELETE_ORDER}
    total_counts["integration_sync_history"] = 0
    total_counts["integrations_status_reset"] = 0
    total_counts["integrations_metadata_reset"] = 0

    for org_uuid in org_ids:
        org_id_str = str(org_uuid)
        org_schema = get_schema_name(org_uuid)

        # Check if schema exists
        if not schema_exists(engine, org_schema):
            print(f"\n  Org {org_id_str[:8]}: Schema '{org_schema}' does not exist, skipping...")
            continue

        print(f"\n  Org {org_id_str[:8]} (schema: {org_schema}):")

        try:
            org_counts = delete_single_org_data(engine, org_id_str, org_schema)

            # Aggregate counts
            for table, count in org_counts.items():
                total_counts[table] = total_counts.get(table, 0) + count
        except Exception as e:
            print(f"    ERROR: {e}")

    return total_counts


def main() -> None:
    args = parse_args()

    # Calculate schema from org_id if not explicitly provided
    schema = args.schema
    if args.org_id and not schema:
        org_uuid = uuid.UUID(args.org_id)
        schema = get_schema_name(org_uuid)
        print(f"Calculated schema name from org_id: {schema}")

    confirm(args.org_id, schema, args.yes, args.export_only)

    db_url = load_database_url()
    engine = create_engine(db_url, future=True)

    # Export data first
    export_counts = export_all_data(engine, args.org_id, schema, args.output_dir)

    if args.export_only:
        print("\n--export-only flag set. Skipping deletion.")
        return

    # Delete data
    delete_counts = delete_crm_data(engine, args.org_id, schema)

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"{'Table':<45} {'Exported':<12} {'Deleted':<12}")
    print("-" * 70)

    # Show CRM tables
    for table in CRM_TABLES_DELETE_ORDER:
        exported = export_counts.get(table, 0)
        deleted = delete_counts.get(table, 0)
        # Skip tables that don't exist (0 in both)
        if (
            exported == 0
            and deleted == 0
            and table not in export_counts
            and table not in delete_counts
        ):
            continue
        print(f"{table:<45} {exported:<12} {deleted:<12}")

    # Show integration_sync_history
    exported = export_counts.get("integration_sync_history", 0)
    deleted = delete_counts.get("integration_sync_history", 0)
    print(f"{'integration_sync_history (CRM)':<45} {exported:<12} {deleted:<12}")

    # Show reset counts
    print("-" * 70)
    print(
        f"{'integrations_status_reset':<45} {'-':<12} {delete_counts.get('integrations_status_reset', 0):<12}"
    )
    print(
        f"{'integrations_metadata_reset':<45} {'-':<12} {delete_counts.get('integrations_metadata_reset', 0):<12}"
    )

    print("-" * 70)
    total_exported = sum(
        v
        for k, v in export_counts.items()
        if k in CRM_TABLES_DELETE_ORDER or k == "integration_sync_history"
    )
    total_deleted = sum(
        v
        for k, v in delete_counts.items()
        if k in CRM_TABLES_DELETE_ORDER or k == "integration_sync_history"
    )
    print(f"{'TOTAL RECORDS':<45} {total_exported:<12} {total_deleted:<12}")
    print("=" * 70)
    print("\nPurge complete.")


if __name__ == "__main__":
    main()
