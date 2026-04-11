#!/usr/bin/env python3
"""Utility script to export and purge messaging messages data.

This script exports all messaging message data to JSON files before deletion,
allowing for data comparison and recovery if needed.

NOTE: This script ONLY purges messages, keeping channels and channel metadata intact.
Use purge_messaging_channels.py to purge all channel data including channels themselves.

Tables affected:
- messaging_messages
- messaging_deal_links (message-level links only, where message_id IS NOT NULL)

Tables NOT affected:
- messaging_channels (preserved)
- messaging_channel_members (preserved)
- messaging_thread_summaries (preserved)
- messaging_deal_links (channel-level links preserved)

Examples
--------
- Export and delete all messaging messages:
    python scripts/maintenance/purge_messaging_messages.py --yes

- Export and delete for a specific organisation:
    python scripts/maintenance/purge_messaging_messages.py --org-id <UUID> --yes

- Export only (no deletion):
    python scripts/maintenance/purge_messaging_messages.py --export-only

- Specify custom output directory:
    python scripts/maintenance/purge_messaging_messages.py --output-dir ./my_exports --yes
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export and delete messaging messages data (keeps channels intact).",
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
        default="./messaging_messages_exports",
        help="Directory to save exported JSON files (default: ./messaging_messages_exports).",
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
        f"This will {action} messaging MESSAGES data for {scope}.\n"
        f"Tables affected: messaging_messages, messaging_deal_links (message-level only)\n"
        f"Tables preserved: messaging_channels, messaging_channel_members, messaging_thread_summaries\n"
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


def export_message_deal_links(
    engine: Engine,
    output_path: Path,
    org_id: str | None,
    schema: str | None,
) -> int:
    """Export messaging_deal_links for message-level links only (message_id IS NOT NULL)."""
    deal_links_table = qualify("messaging_deal_links", schema)

    if org_id:
        query = f"""
            SELECT * FROM {deal_links_table}
            WHERE organisation_id = :org_id
              AND message_id IS NOT NULL
        """  # noqa: S608
        params = {"org_id": org_id}
    else:
        query = f"SELECT * FROM {deal_links_table} WHERE message_id IS NOT NULL"  # noqa: S608
        params = {}

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


def export_single_org_data(
    engine: Engine,
    org_id: str,
    schema: str,
    base_dir: Path,
) -> dict[str, int]:
    """Export messaging messages data for a single organisation/schema."""
    counts = {}

    # Export messaging_messages
    print("    Exporting messaging_messages...", end=" ", flush=True)
    count = export_table_to_json(
        engine,
        "messaging_messages",
        base_dir / "messaging_messages.json",
        org_id,
        schema,
    )
    counts["messaging_messages"] = count
    print(f"{count} records")

    # Export messaging_deal_links (message-level only)
    print("    Exporting messaging_deal_links (message-level)...", end=" ", flush=True)
    count = export_message_deal_links(
        engine, base_dir / "messaging_deal_links_messages.json", org_id, schema
    )
    counts["messaging_deal_links"] = count
    print(f"{count} records")

    return counts


def export_all_data(
    engine: Engine,
    org_id: str | None,
    schema: str | None,
    output_dir: str,
) -> dict[str, int]:
    """Export all messaging messages data to JSON files."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # If org_id is provided, use single org export
    if org_id and schema:
        org_suffix = f"_org_{org_id[:8]}"
        base_dir = Path(output_dir) / f"messaging_messages_export_{timestamp}{org_suffix}"

        print(f"\nExporting messaging messages data to: {base_dir}")
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
    base_dir = Path(output_dir) / f"messaging_messages_export_{timestamp}_all_orgs"
    print(f"\nExporting messaging messages data for ALL organisations to: {base_dir}")

    # Get all organisation IDs from master database
    print("  Fetching all organisation IDs from master database...")
    org_ids = get_all_organisation_ids(engine)
    print(f"  Found {len(org_ids)} organisations")

    total_counts: dict[str, int] = {
        "messaging_messages": 0,
        "messaging_deal_links": 0,
    }
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
                total_counts[table] += count

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
    """Delete messaging messages data for a single organisation/schema.

    Deletion order respects FK constraints:
    1. messaging_deal_links (message-level links only, where message_id IS NOT NULL)
    2. messaging_messages

    NOTE: Channel-level deal links (channel_id IS NOT NULL, message_id IS NULL)
    are preserved along with channels.
    """
    counts = {}

    with engine.begin() as connection:

        def execute_delete(query: str, params: dict | None = None) -> int:
            result = connection.execute(text(query), params or {})
            return result.rowcount

        # 1. Delete messaging_deal_links (message-level only)
        print("    Deleting messaging_deal_links (message-level)...", end=" ", flush=True)
        deal_links_table = qualify("messaging_deal_links", schema)
        query = f"""
            DELETE FROM {deal_links_table}
            WHERE organisation_id = :org_id
              AND message_id IS NOT NULL
        """  # noqa: S608
        count = execute_delete(query, {"org_id": org_id})
        counts["messaging_deal_links"] = count
        print(f"{count} deleted")

        # 2. Delete messaging_messages
        print("    Deleting messaging_messages...", end=" ", flush=True)
        messages_table = qualify("messaging_messages", schema)
        query = f"DELETE FROM {messages_table} WHERE organisation_id = :org_id"  # noqa: S608
        count = execute_delete(query, {"org_id": org_id})
        counts["messaging_messages"] = count
        print(f"{count} deleted")

        # 3. Reset last_synced_at and last_sync_cursor on channels to trigger message re-sync
        print("    Resetting sync cursors on messaging_channels...", end=" ", flush=True)
        channels_table = qualify("messaging_channels", schema)
        query = f"""
            UPDATE {channels_table}
            SET last_synced_at = NULL,
                last_sync_cursor = NULL
            WHERE organisation_id = :org_id
        """  # noqa: S608
        result = connection.execute(text(query), {"org_id": org_id})
        counts["channels_reset"] = result.rowcount
        print(f"{result.rowcount} reset")

    return counts


def delete_messaging_messages_data(
    engine: Engine, org_id: str | None, schema: str | None
) -> dict[str, int]:
    """Delete all messaging messages data in the correct order (respecting FK constraints)."""

    # If org_id and schema are provided, delete for single org
    if org_id and schema:
        print(f"\nDeleting messaging messages data for schema '{schema}'...")
        return delete_single_org_data(engine, org_id, schema)

    # No org_id provided - iterate over all organisations
    print("\nDeleting messaging messages data for ALL organisations...")
    print("  Fetching all organisation IDs from master database...")
    org_ids = get_all_organisation_ids(engine)
    print(f"  Found {len(org_ids)} organisations")

    total_counts: dict[str, int] = {
        "messaging_messages": 0,
        "messaging_deal_links": 0,
        "channels_reset": 0,
    }

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
                total_counts[table] += count
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
    delete_counts = delete_messaging_messages_data(engine, args.org_id, schema)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"{'Table':<40} {'Exported':<12} {'Deleted':<12}")
    print("-" * 60)
    for table in export_counts:
        exported = export_counts.get(table, 0)
        deleted = delete_counts.get(table, 0)
        print(f"{table:<40} {exported:<12} {deleted:<12}")
    if "channels_reset" in delete_counts:
        print(f"{'channels_reset':<40} {'N/A':<12} {delete_counts['channels_reset']:<12}")
    print("-" * 60)
    print(f"{'TOTAL':<40} {sum(export_counts.values()):<12} {sum(delete_counts.values()):<12}")
    print("=" * 60)
    print("\nPurge complete. Channels preserved with sync cursors reset for re-sync.")


if __name__ == "__main__":
    main()
