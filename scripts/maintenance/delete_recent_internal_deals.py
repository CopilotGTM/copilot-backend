#!/usr/bin/env python3
"""Delete recently created internal CRM deals for an organisation.

Designed for repeated CSV import testing in non-production environments.

What it removes:
- CRM deal-linked rows (stage history, participants, activities, stakeholders)
- The target deals from `crm_deals`
- Orphaned provisional contacts/companies created in the same lookback window

By default this script runs in dry-run mode and only prints counts.
Use `--apply --yes` to execute deletion.

Examples:
    python -m scripts.maintenance.delete_recent_internal_deals --org-id <UUID>
    python -m scripts.maintenance.delete_recent_internal_deals --org-id <UUID> --minutes 120 --apply --yes
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from dotenv import load_dotenv
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Connection, Engine

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.infra.utils.resource_naming import get_schema_name  # noqa: E402


INTERNAL_CRM_TYPE = "internal_crm"
SAFE_SCHEMA_RE = re.compile(r"^[a-zA-Z0-9_]+$")
SAFE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_]+$")
CUSTOM_FIELD_ENTITY_DEAL = "deal"
EXCLUDED_DEAL_FK_CHILD_TABLES = {
    "knowledge_integrations",
}

DEFAULT_CHILD_TABLE_ORDER = [
    "crm_deal_stage_history",
    "crm_deal_participants",
    "crm_activities",
    "deal_stakeholders",
]


def load_database_url() -> str:
    """Build DB URL from env, matching existing maintenance script conventions."""
    load_dotenv()
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        return db_url

    host = os.getenv("DATABASE_HOST", "localhost")
    port = os.getenv("DATABASE_PORT", "5432")
    name = os.getenv("DATABASE_NAME", "postgres")
    user = os.getenv("DATABASE_USER", "postgres")
    pwd = os.getenv("DATABASE_PASSWORD", "")
    return f"postgresql://{quote_plus(user)}:{quote_plus(pwd)}@{host}:{port}/{name}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete recently created internal CRM deals for a single organisation."
    )
    parser.add_argument(
        "--org-id",
        required=True,
        type=lambda value: str(uuid.UUID(value)),
        help="Organisation UUID to target.",
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=60,
        help="Lookback window in minutes (default: 60).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Delete all internal CRM deals for this organisation (ignores --minutes).",
    )
    parser.add_argument(
        "--schema",
        type=str,
        help="Optional schema override. Defaults to schema derived from --org-id.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute deletion. Without this flag the script runs as dry-run.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation when --apply is provided.",
    )
    parser.add_argument(
        "--include-soft-deleted",
        action="store_true",
        help="Also include deals already soft-deleted in the lookback window.",
    )
    parser.add_argument(
        "--keep-custom-fields",
        action="store_true",
        help="Do not delete non-predefined deal custom field definitions.",
    )
    return parser.parse_args()


def validate_schema_name(schema: str) -> str:
    if not SAFE_SCHEMA_RE.match(schema):
        raise ValueError(f"Unsafe schema name: {schema}")
    return schema


def validate_identifier(identifier: str) -> str:
    if not SAFE_IDENTIFIER_RE.match(identifier):
        raise ValueError(f"Unsafe identifier: {identifier}")
    return identifier


def table_exists(conn: Connection, schema: str, table_name: str) -> bool:
    result = conn.execute(
        text(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = :schema
              AND table_name = :table
            """
        ),
        {"schema": schema, "table": table_name},
    )
    return result.first() is not None


def get_internal_integration_ids(conn: Connection, schema: str, org_id: str) -> list[uuid.UUID]:
    query = text(
        f"""
        SELECT integration_id
        FROM {schema}.knowledge_integrations
        WHERE organisation_id = :org_id
          AND integration_type = :integration_type
        """
    )
    rows = conn.execute(query, {"org_id": org_id, "integration_type": INTERNAL_CRM_TYPE}).fetchall()
    return [row[0] for row in rows]


def get_target_deals(
    conn: Connection,
    *,
    schema: str,
    org_id: str,
    cutoff: datetime | None,
    integration_ids: list[uuid.UUID],
    include_soft_deleted: bool,
) -> list[dict[str, Any]]:
    where_soft_deleted = "" if include_soft_deleted else "AND d.is_deleted IS FALSE"
    where_cutoff = "" if cutoff is None else "AND d.created_at >= :cutoff"
    stmt = text(
        f"""
        SELECT d.deal_id AS deal_id,
               d.name,
               d.created_at,
               d.is_deleted
        FROM {schema}.crm_deals d
        WHERE d.organisation_id = :org_id
          AND d.integration_id IN :integration_ids
          {where_cutoff}
          {where_soft_deleted}
        ORDER BY d.created_at DESC
        """
    ).bindparams(bindparam("integration_ids", expanding=True))
    params: dict[str, Any] = {
        "org_id": org_id,
        "integration_ids": integration_ids,
    }
    if cutoff is not None:
        params["cutoff"] = cutoff
    rows = conn.execute(stmt, params).mappings()
    return [dict(row) for row in rows]


def get_deal_fk_table_columns(conn: Connection, schema: str) -> dict[str, list[str]]:
    """Return child tables that reference crm_deals(deal_id), keyed by table name."""
    rows = conn.execute(
        text(
            """
            SELECT tc.table_name AS table_name,
                   kcu.column_name AS column_name
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
              AND ccu.table_name = 'crm_deals'
              AND ccu.column_name = 'deal_id'
            """
        ),
        {"schema": schema},
    ).mappings()

    refs: dict[str, list[str]] = {}
    for row in rows:
        table_name = validate_identifier(str(row["table_name"]))
        column_name = validate_identifier(str(row["column_name"]))
        if table_name == "crm_deals":
            continue
        if table_name in EXCLUDED_DEAL_FK_CHILD_TABLES:
            continue
        refs.setdefault(table_name, [])
        if column_name not in refs[table_name]:
            refs[table_name].append(column_name)

    # Keep known high-volume tables first for easier preview readability.
    ordered: dict[str, list[str]] = {}
    for table_name in DEFAULT_CHILD_TABLE_ORDER:
        if table_name in refs:
            ordered[table_name] = refs.pop(table_name)
    for table_name in sorted(refs.keys()):
        ordered[table_name] = refs[table_name]
    return ordered


def count_rows_by_deal_refs(
    conn: Connection,
    *,
    schema: str,
    table_name: str,
    deal_ids: list[uuid.UUID],
    deal_columns: list[str],
) -> int:
    if not deal_columns:
        return 0
    schema = validate_schema_name(schema)
    table_name = validate_identifier(table_name)
    deal_columns = [validate_identifier(col) for col in deal_columns]

    where_clauses: list[str] = []
    bind_params = []
    params: dict[str, Any] = {}
    for idx, column in enumerate(deal_columns):
        param_name = f"deal_ids_{idx}"
        where_clauses.append(f"{column} IN :{param_name}")
        bind_params.append(bindparam(param_name, expanding=True))
        params[param_name] = deal_ids
    stmt = text(
        f"""
        SELECT COUNT(*)
        FROM {schema}.{table_name}
        WHERE {" OR ".join(where_clauses)}
        """
    ).bindparams(*bind_params)
    return int(conn.execute(stmt, params).scalar_one() or 0)


def delete_rows_by_deal_refs(
    conn: Connection,
    *,
    schema: str,
    table_name: str,
    deal_ids: list[uuid.UUID],
    deal_columns: list[str],
) -> int:
    if not deal_columns:
        return 0
    schema = validate_schema_name(schema)
    table_name = validate_identifier(table_name)
    deal_columns = [validate_identifier(col) for col in deal_columns]

    where_clauses: list[str] = []
    bind_params = []
    params: dict[str, Any] = {}
    for idx, column in enumerate(deal_columns):
        param_name = f"deal_ids_{idx}"
        where_clauses.append(f"{column} IN :{param_name}")
        bind_params.append(bindparam(param_name, expanding=True))
        params[param_name] = deal_ids
    stmt = text(
        f"""
        DELETE FROM {schema}.{table_name}
        WHERE {" OR ".join(where_clauses)}
        """
    ).bindparams(*bind_params)
    result = conn.execute(stmt, params)
    return int(result.rowcount or 0)


def count_orphan_provisional_contacts(
    conn: Connection,
    *,
    schema: str,
    org_id: str,
    integration_ids: list[uuid.UUID],
    cutoff: datetime | None,
) -> int:
    where_cutoff = "" if cutoff is None else "AND c.created_at >= :cutoff"
    stmt = text(
        f"""
        SELECT COUNT(*)
        FROM {schema}.crm_contacts c
        WHERE c.organisation_id = :org_id
          AND c.integration_id IN :integration_ids
          {where_cutoff}
          AND c.is_deleted IS FALSE
          AND COALESCE((c.properties ->> 'is_provisional')::boolean, FALSE) = TRUE
          AND NOT EXISTS (
              SELECT 1
              FROM {schema}.crm_deal_participants p
              WHERE p.organisation_id = c.organisation_id
                AND p.integration_id = c.integration_id
                AND p.participant_type = 'contact'
                AND p.related_local_id = c.contact_id
                AND p.is_deleted IS FALSE
          )
          AND NOT EXISTS (
              SELECT 1
              FROM {schema}.crm_deals d
              WHERE d.organisation_id = c.organisation_id
                AND d.integration_id = c.integration_id
                AND d.primary_contact_external_id = c.external_id
                AND d.is_deleted IS FALSE
          )
        """
    ).bindparams(bindparam("integration_ids", expanding=True))
    params: dict[str, Any] = {
        "org_id": org_id,
        "integration_ids": integration_ids,
    }
    if cutoff is not None:
        params["cutoff"] = cutoff
    return int(conn.execute(stmt, params).scalar_one() or 0)


def delete_orphan_provisional_contacts(
    conn: Connection,
    *,
    schema: str,
    org_id: str,
    integration_ids: list[uuid.UUID],
    cutoff: datetime | None,
) -> int:
    where_cutoff = "" if cutoff is None else "AND c.created_at >= :cutoff"
    stmt = text(
        f"""
        DELETE FROM {schema}.crm_contacts c
        WHERE c.organisation_id = :org_id
          AND c.integration_id IN :integration_ids
          {where_cutoff}
          AND c.is_deleted IS FALSE
          AND COALESCE((c.properties ->> 'is_provisional')::boolean, FALSE) = TRUE
          AND NOT EXISTS (
              SELECT 1
              FROM {schema}.crm_deal_participants p
              WHERE p.organisation_id = c.organisation_id
                AND p.integration_id = c.integration_id
                AND p.participant_type = 'contact'
                AND p.related_local_id = c.contact_id
                AND p.is_deleted IS FALSE
          )
          AND NOT EXISTS (
              SELECT 1
              FROM {schema}.crm_deals d
              WHERE d.organisation_id = c.organisation_id
                AND d.integration_id = c.integration_id
                AND d.primary_contact_external_id = c.external_id
                AND d.is_deleted IS FALSE
          )
        """
    ).bindparams(bindparam("integration_ids", expanding=True))
    params: dict[str, Any] = {
        "org_id": org_id,
        "integration_ids": integration_ids,
    }
    if cutoff is not None:
        params["cutoff"] = cutoff
    result = conn.execute(stmt, params)
    return int(result.rowcount or 0)


def count_orphan_provisional_companies(
    conn: Connection,
    *,
    schema: str,
    org_id: str,
    integration_ids: list[uuid.UUID],
    cutoff: datetime | None,
) -> int:
    where_cutoff = "" if cutoff is None else "AND c.created_at >= :cutoff"
    stmt = text(
        f"""
        SELECT COUNT(*)
        FROM {schema}.crm_companies c
        WHERE c.organisation_id = :org_id
          AND c.integration_id IN :integration_ids
          {where_cutoff}
          AND c.is_deleted IS FALSE
          AND COALESCE((c.properties ->> 'is_provisional')::boolean, FALSE) = TRUE
          AND NOT EXISTS (
              SELECT 1
              FROM {schema}.crm_deal_participants p
              WHERE p.organisation_id = c.organisation_id
                AND p.integration_id = c.integration_id
                AND p.participant_type = 'company'
                AND p.related_local_id = c.company_id
                AND p.is_deleted IS FALSE
          )
          AND NOT EXISTS (
              SELECT 1
              FROM {schema}.crm_deals d
              WHERE d.organisation_id = c.organisation_id
                AND d.integration_id = c.integration_id
                AND d.company_external_id = c.external_id
                AND d.is_deleted IS FALSE
          )
        """
    ).bindparams(bindparam("integration_ids", expanding=True))
    params: dict[str, Any] = {
        "org_id": org_id,
        "integration_ids": integration_ids,
    }
    if cutoff is not None:
        params["cutoff"] = cutoff
    return int(conn.execute(stmt, params).scalar_one() or 0)


def delete_orphan_provisional_companies(
    conn: Connection,
    *,
    schema: str,
    org_id: str,
    integration_ids: list[uuid.UUID],
    cutoff: datetime | None,
) -> int:
    where_cutoff = "" if cutoff is None else "AND c.created_at >= :cutoff"
    stmt = text(
        f"""
        DELETE FROM {schema}.crm_companies c
        WHERE c.organisation_id = :org_id
          AND c.integration_id IN :integration_ids
          {where_cutoff}
          AND c.is_deleted IS FALSE
          AND COALESCE((c.properties ->> 'is_provisional')::boolean, FALSE) = TRUE
          AND NOT EXISTS (
              SELECT 1
              FROM {schema}.crm_deal_participants p
              WHERE p.organisation_id = c.organisation_id
                AND p.integration_id = c.integration_id
                AND p.participant_type = 'company'
                AND p.related_local_id = c.company_id
                AND p.is_deleted IS FALSE
          )
          AND NOT EXISTS (
              SELECT 1
              FROM {schema}.crm_deals d
              WHERE d.organisation_id = c.organisation_id
                AND d.integration_id = c.integration_id
                AND d.company_external_id = c.external_id
                AND d.is_deleted IS FALSE
          )
        """
    ).bindparams(bindparam("integration_ids", expanding=True))
    params: dict[str, Any] = {
        "org_id": org_id,
        "integration_ids": integration_ids,
    }
    if cutoff is not None:
        params["cutoff"] = cutoff
    result = conn.execute(stmt, params)
    return int(result.rowcount or 0)


def count_deal_custom_field_definitions(conn: Connection, *, schema: str, org_id: str) -> int:
    stmt = text(
        f"""
        SELECT COUNT(*)
        FROM {schema}.crm_custom_field_definitions
        WHERE organisation_id = :org_id
          AND entity_type = :entity_type
          AND COALESCE(is_predefined, FALSE) = FALSE
        """
    )
    return int(
        conn.execute(stmt, {"org_id": org_id, "entity_type": CUSTOM_FIELD_ENTITY_DEAL}).scalar_one()
        or 0
    )


def delete_deal_custom_field_definitions(conn: Connection, *, schema: str, org_id: str) -> int:
    stmt = text(
        f"""
        DELETE FROM {schema}.crm_custom_field_definitions
        WHERE organisation_id = :org_id
          AND entity_type = :entity_type
          AND COALESCE(is_predefined, FALSE) = FALSE
        """
    )
    result = conn.execute(stmt, {"org_id": org_id, "entity_type": CUSTOM_FIELD_ENTITY_DEAL})
    return int(result.rowcount or 0)


def print_deal_preview(deals: list[dict[str, Any]]) -> None:
    print("\nTarget deals:")
    for deal in deals[:20]:
        created_at = deal.get("created_at")
        created_str = (
            created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at)
        )
        print(
            f"  - {deal['deal_id']} | {deal.get('name') or '<unnamed>'} | "
            f"created_at={created_str} | is_deleted={deal.get('is_deleted')}"
        )
    if len(deals) > 20:
        print(f"  ... and {len(deals) - 20} more")


def confirm_execution(
    *,
    org_id: str,
    schema: str,
    minutes: int,
    delete_all: bool,
    delete_custom_fields: bool,
) -> None:
    scope = "all internal deals" if delete_all else f"last {minutes} minute(s)"
    prompt = (
        "\nThis will permanently delete recent internal CRM deal data.\n"
        f"Organisation: {org_id}\n"
        f"Schema: {schema}\n"
        f"Scope: {scope}\n"
        f"Delete non-predefined deal custom fields: {delete_custom_fields}\n"
        "Continue? [y/N]: "
    )
    answer = input(prompt).strip().lower()
    if answer not in {"y", "yes"}:
        print("Aborted.")
        sys.exit(0)


def run_preview(
    conn: Connection,
    *,
    schema: str,
    org_id: str,
    cutoff: datetime | None,
    integration_ids: list[uuid.UUID],
    include_soft_deleted: bool,
    delete_custom_fields: bool,
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, list[str]]]:
    deals = get_target_deals(
        conn,
        schema=schema,
        org_id=org_id,
        cutoff=cutoff,
        integration_ids=integration_ids,
        include_soft_deleted=include_soft_deleted,
    )
    deal_ids = [deal["deal_id"] for deal in deals]
    deal_fk_table_columns = get_deal_fk_table_columns(conn, schema)
    counts: dict[str, int] = {
        "crm_deals": len(deals),
        "orphan_provisional_contacts": 0,
        "orphan_provisional_companies": 0,
        "crm_custom_field_definitions": 0,
    }
    for table_name in deal_fk_table_columns:
        counts.setdefault(table_name, 0)

    if deal_ids:
        for table_name, deal_columns in deal_fk_table_columns.items():
            if table_exists(conn, schema, table_name):
                counts[table_name] = count_rows_by_deal_refs(
                    conn,
                    schema=schema,
                    table_name=table_name,
                    deal_ids=deal_ids,
                    deal_columns=deal_columns,
                )

    if (
        integration_ids
        and table_exists(conn, schema, "crm_contacts")
        and table_exists(conn, schema, "crm_deal_participants")
    ):
        counts["orphan_provisional_contacts"] = count_orphan_provisional_contacts(
            conn,
            schema=schema,
            org_id=org_id,
            integration_ids=integration_ids,
            cutoff=cutoff,
        )
    if (
        integration_ids
        and table_exists(conn, schema, "crm_companies")
        and table_exists(conn, schema, "crm_deal_participants")
    ):
        counts["orphan_provisional_companies"] = count_orphan_provisional_companies(
            conn,
            schema=schema,
            org_id=org_id,
            integration_ids=integration_ids,
            cutoff=cutoff,
        )

    if delete_custom_fields and table_exists(conn, schema, "crm_custom_field_definitions"):
        counts["crm_custom_field_definitions"] = count_deal_custom_field_definitions(
            conn,
            schema=schema,
            org_id=org_id,
        )

    return deals, counts, deal_fk_table_columns


def print_counts(title: str, counts: dict[str, int]) -> None:
    print(f"\n{title}")
    print("-" * 50)
    ordered_keys = [
        "crm_deals",
        "crm_deal_stage_history",
        "crm_deal_participants",
        "crm_activities",
        "deal_stakeholders",
        "deal_jobs",
        "orphan_provisional_contacts",
        "orphan_provisional_companies",
        "crm_custom_field_definitions",
    ]
    for key in ordered_keys:
        if key in counts:
            print(f"{key:<35} {counts.get(key, 0):>8}")
    extra_keys = sorted(key for key in counts if key not in ordered_keys)
    for key in extra_keys:
        print(f"{key:<35} {counts.get(key, 0):>8}")


def execute_delete(
    conn: Connection,
    *,
    schema: str,
    org_id: str,
    cutoff: datetime | None,
    integration_ids: list[uuid.UUID],
    deals: list[dict[str, Any]],
    deal_fk_table_columns: dict[str, list[str]],
    delete_custom_fields: bool,
) -> dict[str, int]:
    deal_ids = [deal["deal_id"] for deal in deals]
    deleted_counts: dict[str, int] = {
        "crm_deals": 0,
        "orphan_provisional_contacts": 0,
        "orphan_provisional_companies": 0,
        "crm_custom_field_definitions": 0,
    }
    for table_name in deal_fk_table_columns:
        deleted_counts.setdefault(table_name, 0)

    if deal_ids:
        for table_name, deal_columns in deal_fk_table_columns.items():
            if table_exists(conn, schema, table_name):
                deleted_counts[table_name] = delete_rows_by_deal_refs(
                    conn,
                    schema=schema,
                    table_name=table_name,
                    deal_ids=deal_ids,
                    deal_columns=deal_columns,
                )

        deleted_counts["crm_deals"] = delete_rows_by_deal_refs(
            conn,
            schema=schema,
            table_name="crm_deals",
            deal_ids=deal_ids,
            deal_columns=["deal_id"],
        )

    if (
        integration_ids
        and table_exists(conn, schema, "crm_contacts")
        and table_exists(conn, schema, "crm_deal_participants")
    ):
        deleted_counts["orphan_provisional_contacts"] = delete_orphan_provisional_contacts(
            conn,
            schema=schema,
            org_id=org_id,
            integration_ids=integration_ids,
            cutoff=cutoff,
        )
    if (
        integration_ids
        and table_exists(conn, schema, "crm_companies")
        and table_exists(conn, schema, "crm_deal_participants")
    ):
        deleted_counts["orphan_provisional_companies"] = delete_orphan_provisional_companies(
            conn,
            schema=schema,
            org_id=org_id,
            integration_ids=integration_ids,
            cutoff=cutoff,
        )

    if delete_custom_fields and table_exists(conn, schema, "crm_custom_field_definitions"):
        deleted_counts["crm_custom_field_definitions"] = delete_deal_custom_field_definitions(
            conn,
            schema=schema,
            org_id=org_id,
        )

    return deleted_counts


def main() -> None:
    args = parse_args()
    if not args.all and args.minutes <= 0:
        raise ValueError("--minutes must be greater than 0 unless --all is set")

    org_id = args.org_id
    schema = args.schema or get_schema_name(uuid.UUID(org_id))
    schema = validate_schema_name(schema)
    cutoff = None if args.all else datetime.now(UTC) - timedelta(minutes=args.minutes)
    delete_custom_fields = not args.keep_custom_fields

    engine: Engine = create_engine(load_database_url(), future=True)

    with engine.connect() as conn:
        if not table_exists(conn, schema, "crm_deals"):
            raise RuntimeError(
                f"Table {schema}.crm_deals not found. Check --org-id/--schema and migrations."
            )
        if not table_exists(conn, schema, "knowledge_integrations"):
            raise RuntimeError(
                f"Table {schema}.knowledge_integrations not found. Check --org-id/--schema."
            )

        integration_ids = get_internal_integration_ids(conn, schema, org_id)
        if not integration_ids:
            print(
                f"No '{INTERNAL_CRM_TYPE}' integration found for org {org_id} in schema '{schema}'. "
                "Deal rows will be zero; continuing for optional custom-field cleanup."
            )
        deals, preview_counts, deal_fk_table_columns = run_preview(
            conn,
            schema=schema,
            org_id=org_id,
            cutoff=cutoff,
            integration_ids=integration_ids,
            include_soft_deleted=args.include_soft_deleted,
            delete_custom_fields=delete_custom_fields,
        )

    if cutoff is None:
        scope = f"org={org_id} schema={schema} mode=all_internal_deals"
    else:
        scope = (
            f"org={org_id} schema={schema} lookback_minutes={args.minutes} "
            f"cutoff_utc={cutoff.isoformat()}"
        )
    print(f"\nScope: {scope}")
    print_deal_preview(deals)
    print_counts("Dry-run counts", preview_counts)

    if not args.apply:
        print("\nDry-run complete. Re-run with --apply to execute deletion.")
        return

    if not args.yes:
        confirm_execution(
            org_id=org_id,
            schema=schema,
            minutes=args.minutes,
            delete_all=args.all,
            delete_custom_fields=delete_custom_fields,
        )

    with engine.begin() as conn:
        deleted_counts = execute_delete(
            conn,
            schema=schema,
            org_id=org_id,
            cutoff=cutoff,
            integration_ids=integration_ids,
            deals=deals,
            deal_fk_table_columns=deal_fk_table_columns,
            delete_custom_fields=delete_custom_fields,
        )

    print_counts("Deleted rows", deleted_counts)
    print("\nDeletion complete.")


if __name__ == "__main__":
    main()
