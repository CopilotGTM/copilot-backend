#!/usr/bin/env python3
# ruff: noqa: E402, I001
"""Preview or purge Salesforce CRM rows for one organisation.

This is for rerunning a Salesforce initial sync after a failed/partial load.
It deliberately preserves `knowledge_integrations`; do not replace this with
`TRUNCATE ... CASCADE`, because `crm_deals` is referenced by that table.

Examples:
    uv run python -m scripts.maintenance.purge_salesforce_initial_sync_data \
        --org-id d0a590bf-d364-4e0d-a4a1-4ad9d6525467

    uv run python -m scripts.maintenance.purge_salesforce_initial_sync_data \
        --org-id d0a590bf-d364-4e0d-a4a1-4ad9d6525467 --execute --yes
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.infra.utils.resource_naming import get_schema_name  # noqa: E402


SAFE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
PROVIDER = "salesforce"
PRESERVED_TABLES = {"knowledge_integrations"}
ROOT_TABLES = [
    "crm_activities",
    "crm_deals",
    "crm_contacts",
    "crm_companies",
    "crm_users",
    "crm_deal_stages",
    "crm_pipelines",
]
STATIC_TABLES_BY_INTEGRATION = [
    "crm_source_object_mappings",
    "integration_sync_history",
]
TABLE_DISPLAY_ORDER = [
    "knowledge_integrations_deal_id_cleared",
    "crm_source_object_mappings",
    "integration_sync_history",
    "crm_activities",
    "crm_deal_stage_history",
    "crm_deal_participants",
    "deal_stakeholders",
    "deal_jobs",
    "deal_assets",
    "deal_memories",
    "deal_risks_blockers",
    "deal_stakeholder_insights",
    "calendar_event_deal_associations",
    "deal_email_associations",
    "emails",
    "messaging_deal_links",
    "integration_activity_requests",
    "integration_participants",
    "crm_leads",
    "lead_forms",
    "crm_deals",
    "crm_contacts",
    "crm_companies",
    "crm_deal_stages",
    "crm_pipelines",
    "crm_users",
]


@dataclass(frozen=True)
class ForeignKeyRef:
    child_table: str
    child_column: str
    parent_table: str
    parent_column: str


def load_database_url() -> str:
    load_dotenv()
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        return db_url

    host = os.getenv("DATABASE_HOST", "localhost")
    port = os.getenv("DATABASE_PORT", "5432")
    name = os.getenv("DATABASE_NAME", "postgres")
    user = quote_plus(os.getenv("DATABASE_USER", "postgres"))
    password = quote_plus(os.getenv("DATABASE_PASSWORD", ""))
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview or delete Salesforce CRM rows for one organisation."
    )
    parser.add_argument(
        "--org-id",
        required=True,
        type=lambda value: str(uuid.UUID(value)),
        help="Organisation UUID to purge.",
    )
    parser.add_argument(
        "--integration-id",
        type=lambda value: str(uuid.UUID(value)),
        help="Salesforce integration UUID. If omitted, exactly one active Salesforce integration "
        "must exist for the organisation.",
    )
    parser.add_argument(
        "--schema",
        help="Schema override. Defaults to org schema derived from --org-id.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Persist deletion. Without this flag the script prints a dry-run preview.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required with --execute to skip the interactive confirmation.",
    )
    return parser.parse_args()


def validate_identifier(value: str) -> str:
    if not SAFE_IDENTIFIER_RE.match(value):
        raise ValueError(f"Unsafe SQL identifier: {value}")
    return value


def qualify(schema: str, table: str) -> str:
    return f"{validate_identifier(schema)}.{validate_identifier(table)}"


def temp_table_name(table: str) -> str:
    return f"tmp_sf_purge_{validate_identifier(table)}"


def table_exists(conn: Connection, schema: str, table: str) -> bool:
    result = conn.execute(
        text(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = :schema
              AND table_name = :table
            """
        ),
        {"schema": schema, "table": table},
    )
    return result.first() is not None


def get_primary_key_columns(conn: Connection, schema: str) -> dict[str, str]:
    rows = conn.execute(
        text(
            """
            SELECT tc.table_name, kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type = 'PRIMARY KEY'
              AND tc.table_schema = :schema
            ORDER BY tc.table_name, kcu.ordinal_position
            """
        ),
        {"schema": schema},
    ).mappings()

    grouped: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        grouped[str(row["table_name"])].append(str(row["column_name"]))

    return {
        validate_identifier(table): validate_identifier(columns[0])
        for table, columns in grouped.items()
        if len(columns) == 1
    }


def get_foreign_keys(conn: Connection, schema: str) -> list[ForeignKeyRef]:
    rows = conn.execute(
        text(
            """
            SELECT
                tc.table_name AS child_table,
                kcu.column_name AS child_column,
                ccu.table_name AS parent_table,
                ccu.column_name AS parent_column
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name = tc.constraint_name
             AND ccu.table_schema = tc.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = :schema
              AND ccu.table_schema = :schema
            """
        ),
        {"schema": schema},
    ).mappings()

    refs: list[ForeignKeyRef] = []
    for row in rows:
        child_table = validate_identifier(str(row["child_table"]))
        if child_table in PRESERVED_TABLES:
            continue
        refs.append(
            ForeignKeyRef(
                child_table=child_table,
                child_column=validate_identifier(str(row["child_column"])),
                parent_table=validate_identifier(str(row["parent_table"])),
                parent_column=validate_identifier(str(row["parent_column"])),
            )
        )
    return refs


def resolve_salesforce_integration_id(conn: Connection, schema: str, org_id: str) -> str:
    rows = conn.execute(
        text(
            f"""
            SELECT integration_id
            FROM {qualify(schema, "knowledge_integrations")}
            WHERE organisation_id = :org_id
              AND integration_type = :integration_type
              AND is_active IS TRUE
            ORDER BY created_at DESC
            """  # noqa: S608
        ),
        {"org_id": org_id, "integration_type": PROVIDER},
    ).fetchall()
    if len(rows) != 1:
        raise RuntimeError(
            f"Expected exactly one active Salesforce integration for org {org_id}; found {len(rows)}. "
            "Pass --integration-id explicitly if this is intentional."
        )
    return str(rows[0][0])


def create_temp_id_table(
    conn: Connection,
    *,
    schema: str,
    table: str,
    pk_column: str,
) -> None:
    temp_table = temp_table_name(table)
    qualified_table = qualify(schema, table)
    conn.execute(text(f"DROP TABLE IF EXISTS {temp_table}"))
    conn.execute(
        text(
            f"""
            CREATE TEMP TABLE {temp_table} (
                id uuid PRIMARY KEY
            ) ON COMMIT DROP
            """
        )
    )
    conn.execute(
        text(
            f"""
            INSERT INTO {temp_table}(id)
            SELECT {pk_column}
            FROM {qualified_table}
            WHERE false
            """  # noqa: S608
        )
    )


def seed_root_tables(
    conn: Connection,
    *,
    schema: str,
    org_id: str,
    integration_id: str,
    pk_columns: dict[str, str],
) -> set[str]:
    seeded: set[str] = set()
    for table in ROOT_TABLES:
        if not table_exists(conn, schema, table) or table not in pk_columns:
            continue
        create_temp_id_table(conn, schema=schema, table=table, pk_column=pk_columns[table])
        result = conn.execute(
            text(
                f"""
                INSERT INTO {temp_table_name(table)}(id)
                SELECT {pk_columns[table]}
                FROM {qualify(schema, table)}
                WHERE organisation_id = :org_id
                  AND integration_id = :integration_id
                ON CONFLICT DO NOTHING
                """  # noqa: S608
            ),
            {"org_id": org_id, "integration_id": integration_id},
        )
        if result.rowcount:
            seeded.add(table)
    return seeded


def expand_target_tables(
    conn: Connection,
    *,
    schema: str,
    pk_columns: dict[str, str],
    foreign_keys: list[ForeignKeyRef],
    seeded_tables: set[str],
) -> tuple[set[str], dict[str, int]]:
    target_tables = set(seeded_tables)
    depths = dict.fromkeys(seeded_tables, 0)
    queue = deque(seeded_tables)

    while queue:
        parent = queue.popleft()
        parent_temp = temp_table_name(parent)
        for ref in foreign_keys:
            if ref.parent_table != parent:
                continue
            child = ref.child_table
            if child in PRESERVED_TABLES or child not in pk_columns:
                continue
            if not table_exists(conn, schema, child):
                continue
            if child not in target_tables:
                create_temp_id_table(
                    conn,
                    schema=schema,
                    table=child,
                    pk_column=pk_columns[child],
                )
                target_tables.add(child)
                queue.append(child)
            child_temp = temp_table_name(child)
            conn.execute(
                text(
                    f"""
                    INSERT INTO {child_temp}(id)
                    SELECT DISTINCT child.{pk_columns[child]}
                    FROM {qualify(schema, child)} child
                    JOIN {parent_temp} parent_ids
                      ON child.{ref.child_column} = parent_ids.id
                    ON CONFLICT DO NOTHING
                    """  # noqa: S608
                )
            )
            depths[child] = max(depths.get(child, 0), depths.get(parent, 0) + 1)

    return target_tables, depths


def count_temp_rows(conn: Connection, tables: set[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in sorted(tables):
        result = conn.execute(text(f"SELECT COUNT(*) FROM {temp_table_name(table)}"))  # noqa: S608
        counts[table] = int(result.scalar_one() or 0)
    return counts


def count_static_rows(
    conn: Connection,
    *,
    schema: str,
    integration_id: str,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    if table_exists(conn, schema, "crm_source_object_mappings"):
        counts["crm_source_object_mappings"] = int(
            conn.execute(
                text(
                    f"""
                    SELECT COUNT(*)
                    FROM {qualify(schema, "crm_source_object_mappings")}
                    WHERE integration_id = :integration_id
                      AND provider = :provider
                    """  # noqa: S608
                ),
                {"integration_id": integration_id, "provider": PROVIDER},
            ).scalar_one()
            or 0
        )
    if table_exists(conn, schema, "integration_sync_history"):
        counts["integration_sync_history"] = int(
            conn.execute(
                text(
                    f"""
                    SELECT COUNT(*)
                    FROM {qualify(schema, "integration_sync_history")}
                    WHERE integration_id = :integration_id
                    """  # noqa: S608
                ),
                {"integration_id": integration_id},
            ).scalar_one()
            or 0
        )
    return counts


def count_knowledge_integration_deal_links(conn: Connection, *, schema: str) -> int:
    if not table_exists(conn, schema, "knowledge_integrations") or not table_exists(
        conn, schema, "crm_deals"
    ):
        return 0
    return int(
        conn.execute(
            text(
                f"""
                SELECT COUNT(*)
                FROM {qualify(schema, "knowledge_integrations")} ki
                JOIN {temp_table_name("crm_deals")} target_deals
                  ON ki.deal_id = target_deals.id
                """  # noqa: S608
            )
        ).scalar_one()
        or 0
    )


def clear_knowledge_integration_deal_links(conn: Connection, *, schema: str) -> int:
    result = conn.execute(
        text(
            f"""
            UPDATE {qualify(schema, "knowledge_integrations")} ki
            SET deal_id = NULL
            FROM {temp_table_name("crm_deals")} target_deals
            WHERE ki.deal_id = target_deals.id
            """  # noqa: S608
        )
    )
    return int(result.rowcount or 0)


def delete_static_rows(conn: Connection, *, schema: str, integration_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    if table_exists(conn, schema, "crm_source_object_mappings"):
        result = conn.execute(
            text(
                f"""
                DELETE FROM {qualify(schema, "crm_source_object_mappings")}
                WHERE integration_id = :integration_id
                  AND provider = :provider
                """  # noqa: S608
            ),
            {"integration_id": integration_id, "provider": PROVIDER},
        )
        counts["crm_source_object_mappings"] = int(result.rowcount or 0)
    if table_exists(conn, schema, "integration_sync_history"):
        result = conn.execute(
            text(
                f"""
                DELETE FROM {qualify(schema, "integration_sync_history")}
                WHERE integration_id = :integration_id
                """  # noqa: S608
            ),
            {"integration_id": integration_id},
        )
        counts["integration_sync_history"] = int(result.rowcount or 0)
    return counts


def delete_target_rows(
    conn: Connection,
    *,
    schema: str,
    target_tables: set[str],
    depths: dict[str, int],
    pk_columns: dict[str, str],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    ordered_tables = sorted(target_tables, key=lambda table: (depths.get(table, 0), table), reverse=True)
    for table in ordered_tables:
        result = conn.execute(
            text(
                f"""
                DELETE FROM {qualify(schema, table)} target
                USING {temp_table_name(table)} target_ids
                WHERE target.{pk_columns[table]} = target_ids.id
                """  # noqa: S608
            )
        )
        counts[table] = int(result.rowcount or 0)
    return counts


def print_counts(title: str, counts: dict[str, int]) -> None:
    print(f"\n{title}")
    print("-" * 64)
    printed = set()
    for table in TABLE_DISPLAY_ORDER:
        if table in counts:
            print(f"{table:<45} {counts[table]:>12}")
            printed.add(table)
    for table in sorted(set(counts) - printed):
        print(f"{table:<45} {counts[table]:>12}")


def confirm_execution(org_id: str, schema: str, integration_id: str) -> None:
    prompt = (
        "\nThis will delete Salesforce CRM data and dependent rows for one organisation.\n"
        f"Organisation: {org_id}\n"
        f"Schema: {schema}\n"
        f"Salesforce integration: {integration_id}\n"
        "It will NOT delete rows from knowledge_integrations.\n"
        "Continue? [y/N]: "
    )
    answer = input(prompt).strip().lower()
    if answer not in {"y", "yes"}:
        print("Aborted.")
        sys.exit(0)


def build_preview(
    conn: Connection,
    *,
    schema: str,
    org_id: str,
    integration_id: str,
) -> tuple[dict[str, int], set[str], dict[str, int], dict[str, str]]:
    pk_columns = get_primary_key_columns(conn, schema)
    foreign_keys = get_foreign_keys(conn, schema)
    seeded_tables = seed_root_tables(
        conn,
        schema=schema,
        org_id=org_id,
        integration_id=integration_id,
        pk_columns=pk_columns,
    )
    target_tables, depths = expand_target_tables(
        conn,
        schema=schema,
        pk_columns=pk_columns,
        foreign_keys=foreign_keys,
        seeded_tables=seeded_tables,
    )
    counts = count_temp_rows(conn, target_tables)
    counts.update(count_static_rows(conn, schema=schema, integration_id=integration_id))
    counts["knowledge_integrations_deal_id_cleared"] = count_knowledge_integration_deal_links(
        conn,
        schema=schema,
    )
    return counts, target_tables, depths, pk_columns


def main() -> None:
    args = parse_args()
    org_id = args.org_id
    schema = validate_identifier(args.schema or get_schema_name(uuid.UUID(org_id)))
    engine: Engine = create_engine(load_database_url(), future=True)

    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            if not table_exists(conn, schema, "knowledge_integrations"):
                raise RuntimeError(f"{schema}.knowledge_integrations does not exist")
            integration_id = args.integration_id or resolve_salesforce_integration_id(
                conn,
                schema,
                org_id,
            )
            counts, target_tables, depths, pk_columns = build_preview(
                conn,
                schema=schema,
                org_id=org_id,
                integration_id=integration_id,
            )
            print(f"Organisation: {org_id}")
            print(f"Schema: {schema}")
            print(f"Salesforce integration: {integration_id}")
            print_counts("Dry-run rows that would be changed", counts)

            if not args.execute:
                transaction.rollback()
                print("\nDry-run complete. Re-run with --execute --yes to delete.")
                return

            if not args.yes:
                confirm_execution(org_id, schema, integration_id)

            deleted_counts: dict[str, int] = {}
            deleted_counts["knowledge_integrations_deal_id_cleared"] = (
                clear_knowledge_integration_deal_links(conn, schema=schema)
            )
            deleted_counts.update(
                delete_static_rows(conn, schema=schema, integration_id=integration_id)
            )
            deleted_counts.update(
                delete_target_rows(
                    conn,
                    schema=schema,
                    target_tables=target_tables,
                    depths=depths,
                    pk_columns=pk_columns,
                )
            )
            transaction.commit()
        except Exception:
            transaction.rollback()
            raise

    print_counts("Deleted/updated rows", deleted_counts)
    print("\nDeletion complete. knowledge_integrations rows were preserved.")


if __name__ == "__main__":
    main()
