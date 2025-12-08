#!/usr/bin/env python3
"""Utility script to purge CRM, integration sync, and calendar data.

This is destructive. Use in lower environments or after taking backups.

Examples
--------
- Delete everything:
    python scripts/maintenance/purge_crm_and_calendar.py --yes
- Delete data for a single organisation:
    python scripts/maintenance/purge_crm_and_calendar.py --org-id <UUID> --yes
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from urllib.parse import quote_plus

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from src.infra.utils.resource_naming import get_schema_name


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

    # URL-encode username and password to handle special characters like @
    user_encoded = quote_plus(user)
    pwd_encoded = quote_plus(pwd)

    return f"postgresql://{user_encoded}:{pwd_encoded}@{host}:{port}/{name}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Delete CRM, integration sync, and calendar data.")
    parser.add_argument(
        "--org-id",
        type=lambda value: str(uuid.UUID(value)),
        help="Optional organisation UUID to scope the purge. "
        "If omitted, data from all organisations will be removed.",
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
    return parser.parse_args()


def confirm(org_id: str | None, schema: str | None, auto_confirm: bool) -> None:
    if auto_confirm:
        return

    if schema:
        scope = f"schema '{schema}'"
    else:
        scope = f"organisation {org_id}" if org_id else "ALL organisations"
    prompt = (
        f"This will permanently delete CRM, integration sync history, "
        f"and calendar data for {scope}. Continue? [y/N]: "
    )
    response = input(prompt).strip().lower()
    if response not in {"y", "yes"}:
        print("Aborted.")
        sys.exit(0)


def qualify(table: str, schema: str | None) -> str:
    if schema:
        if not schema.replace("_", "").isalnum():
            raise ValueError(f"Unsafe schema name: {schema}")
        return f"{schema}.{table}"
    return table


# SQL template constants for deletion operations
DELETE_TEMPLATE = "DELETE FROM {table} WHERE organisation_id = :org_id"
DELETE_ATTENDEES_QUERY = """
DELETE FROM {attendees}
WHERE event_id IN (
    SELECT event_id FROM {events}
    WHERE organisation_id = :org_id
)
"""
DELETE_INTEGRATION_SYNC_QUERY = """
DELETE FROM {history}
WHERE integration_id IN (
    SELECT ki.integration_id
    FROM {integrations} AS ki
    WHERE organisation_id = :org_id
)
"""


def purge_data(engine, org_id: str | None, schema: str | None) -> None:
    with engine.begin() as connection:
        params = {"org_id": org_id} if org_id else {}

        def execute(statement: str) -> None:
            connection.execute(text(statement), params)

        if org_id:
            # Calendar data
            attendees_table = qualify("calendar_event_attendees", schema)
            events_table = qualify("calendar_events", schema)
            # Table names are validated via qualify(), cannot be parameterized in SQL
            query = DELETE_ATTENDEES_QUERY.format(  # noqa: S608
                attendees=attendees_table, events=events_table
            )
            execute(query)
            # noinspection PyUnresolvedReference - Table name validated via qualify()
            execute(DELETE_TEMPLATE.format(table=events_table))  # noqa: S608

            # CRM deal-related tables
            # noinspection PyUnresolvedReference - Table names validated via qualify()
            execute(DELETE_TEMPLATE.format(table=qualify("crm_deal_stage_history", schema)))  # noqa: S608
            execute(DELETE_TEMPLATE.format(table=qualify("crm_deal_participants", schema)))  # noqa: S608
            execute(DELETE_TEMPLATE.format(table=qualify("crm_deals", schema)))  # noqa: S608
            execute(DELETE_TEMPLATE.format(table=qualify("crm_deal_stages", schema)))  # noqa: S608
            execute(DELETE_TEMPLATE.format(table=qualify("crm_pipelines", schema)))  # noqa: S608

            # Other CRM entities
            execute(DELETE_TEMPLATE.format(table=qualify("crm_contacts", schema)))  # noqa: S608
            execute(DELETE_TEMPLATE.format(table=qualify("crm_companies", schema)))  # noqa: S608
            execute(DELETE_TEMPLATE.format(table=qualify("crm_users", schema)))  # noqa: S608

            # Integration sync history (linked via knowledge_integrations)
            history_table = qualify("integration_sync_history", schema)
            integrations_table = qualify("knowledge_integrations", schema)
            # noinspection PyUnresolvedReference - Table names validated via qualify()
            sync_query = DELETE_INTEGRATION_SYNC_QUERY.format(  # noqa: S608
                history=history_table, integrations=integrations_table
            )
            execute(sync_query)
        else:
            # No organisation scope → wipe everything
            tables_in_order = [
                "calendar_event_attendees",
                "calendar_events",
                "crm_deal_stage_history",
                "crm_deal_participants",
                "crm_deals",
                "crm_deal_stages",
                "crm_pipelines",
                "crm_contacts",
                "crm_companies",
                "crm_users",
                "integration_sync_history",
            ]
            for table in tables_in_order:
                qualified_table = qualify(table, schema)
                # noinspection PyUnresolvedReference - Table name validated via qualify()
                execute(f"DELETE FROM {qualified_table}")  # noqa: S608


def main() -> None:
    args = parse_args()

    # Calculate schema from org_id if not explicitly provided
    schema = args.schema
    if args.org_id and not schema:
        org_uuid = uuid.UUID(args.org_id)
        schema = get_schema_name(org_uuid)
        print(f"Calculated schema name from org_id: {schema}")

    confirm(args.org_id, schema, args.yes)

    db_url = load_database_url()
    engine = create_engine(db_url, future=True)

    if schema:
        scope = f"schema '{schema}'"
    else:
        scope = f"organisation {args.org_id}" if args.org_id else "ALL organisations"
    print(f"Purging CRM, integration sync, and calendar data for {scope}...")
    purge_data(engine, args.org_id, schema)
    print("Purge complete.")


if __name__ == "__main__":
    main()
