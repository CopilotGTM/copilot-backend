#!/usr/bin/env python3
# ruff: noqa: S608
"""Merge legacy Google/Microsoft capability rows into provider parent rows.

Dry-run is the default. Use ``--execute --yes`` to export affected rows and
mutate data. The script processes one organisation per transaction and refuses
to delete retired rows while any discovered ``integration_id`` reference remains.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote_plus

from dotenv import load_dotenv
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Connection, Engine

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.infra.utils.resource_naming import get_schema_name  # noqa: E402

ProviderArg = Literal["google", "microsoft", "all"]

GOOGLE_PROVIDER_TYPES = ("google", "gmail", "google_calendar")
MICROSOFT_PROVIDER_TYPES = ("microsoft", "outlook_email", "outlook_calendar", "teams")
PROVIDER_TYPES = {
    "google": GOOGLE_PROVIDER_TYPES,
    "microsoft": MICROSOFT_PROVIDER_TYPES,
}
PROVIDER_PARENT = {"google": "google", "microsoft": "microsoft"}
SURVIVOR_PRIORITY = {
    "google": {"google_calendar": 0, "google": 1, "gmail": 2},
    "microsoft": {"outlook_calendar": 0, "microsoft": 1, "outlook_email": 2, "teams": 3},
}

CURSOR_KEYS = {
    "gmail_history_id",
    "email_folder_delta_links",
    "calendar_delta_link",
    "outlook_delta_link",
}
WEBHOOK_KEYS = {
    "google_webhook_subscriptions",
    "microsoft_graph_subscriptions",
    "microsoft_graph_subscription",
}


@dataclass(frozen=True)
class RewriteTable:
    table_name: str
    column_name: str = "integration_id"
    schema: str | None = None


ORG_REWRITE_TABLES: dict[str, RewriteTable] = {
    table: RewriteTable(table)
    for table in (
        "integration_sync_history",
        "integration_sync_states",
        "organisation_sources",
        "email_folders",
        "emails",
        "email_thread_summaries",
        "integration_participants",
        "integration_participant_identities",
        "calendar_event_user_states",
        "messaging_channel_integration_states",
        "messaging_deal_links",
        "messaging_thread_summaries",
        "integration_activity_requests",
        "deal_stakeholders",
    )
}
MASTER_REWRITE_TABLES: dict[str, RewriteTable] = {
    "organisation_integration_details": RewriteTable(
        "organisation_integration_details",
        schema="public",
    )
}
EVENT_CENTRIC_TABLES = {
    "calendar_events",
    "calendar_event_attendees",
    "calendar_event_deal_associations",
    "meeting_jobs",
    "meeting_recordings",
    "deal_timeline_events",
    "meeting_transcripts",
}


@dataclass(frozen=True)
class IntegrationRecord:
    integration_id: uuid.UUID
    organisation_id: uuid.UUID
    created_by_user_id: uuid.UUID
    integration_email: str | None
    connection_id: str | None
    integration_type: str
    name: str
    is_active: bool
    last_synced_at: datetime | None
    last_sync_status: str | None
    last_sync_error: str | None
    consecutive_sync_failures: int
    connection_metadata: dict[str, Any]
    updated_at: datetime | None

    @classmethod
    def from_mapping(cls, row: dict[str, Any]) -> IntegrationRecord:
        return cls(
            integration_id=uuid.UUID(str(row["integration_id"])),
            organisation_id=uuid.UUID(str(row["organisation_id"])),
            created_by_user_id=uuid.UUID(str(row["created_by_user_id"])),
            integration_email=row.get("integration_email"),
            connection_id=row.get("connection_id"),
            integration_type=str(row["integration_type"]),
            name=str(row.get("name") or row["integration_type"]),
            is_active=bool(row.get("is_active", True)),
            last_synced_at=row.get("last_synced_at"),
            last_sync_status=row.get("last_sync_status"),
            last_sync_error=row.get("last_sync_error"),
            consecutive_sync_failures=int(row.get("consecutive_sync_failures") or 0),
            connection_metadata=dict(row.get("connection_metadata") or {}),
            updated_at=row.get("updated_at"),
        )


@dataclass(frozen=True)
class MergePlan:
    provider: str
    group_key: tuple[str, str, str]
    survivor: IntegrationRecord
    records: list[IntegrationRecord]

    @property
    def retired(self) -> list[IntegrationRecord]:
        return [row for row in self.records if row.integration_id != self.survivor.integration_id]

    @property
    def retired_ids(self) -> list[uuid.UUID]:
        return [row.integration_id for row in self.retired]


@dataclass(frozen=True)
class IntegrationReference:
    schema: str
    table_name: str
    column_name: str
    row_count: int


class RemainingReferenceError(RuntimeError):
    pass


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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org-id", type=lambda value: str(uuid.UUID(value)))
    parser.add_argument(
        "--provider",
        choices=("google", "microsoft", "all"),
        default="all",
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument(
        "--output-dir",
        default="./provider_parent_merge_exports",
        help="Export directory used in execute mode.",
    )
    return parser.parse_args()


def build_merge_plans(
    rows: list[IntegrationRecord],
    *,
    provider: ProviderArg,
) -> list[MergePlan]:
    providers = ("google", "microsoft") if provider == "all" else (provider,)
    plans: list[MergePlan] = []
    for provider_name in providers:
        provider_rows = [
            row for row in rows if row.integration_type in PROVIDER_TYPES[provider_name]
        ]
        groups: dict[tuple[str, str, str], list[IntegrationRecord]] = {}
        for row in provider_rows:
            account_key = _connected_account_key(row)
            key = (str(row.organisation_id), str(row.created_by_user_id), account_key)
            groups.setdefault(key, []).append(row)

        for key, group_rows in groups.items():
            survivor = min(
                group_rows,
                key=lambda row: (
                    SURVIVOR_PRIORITY[provider_name].get(row.integration_type, 99),
                    -(row.updated_at.timestamp() if row.updated_at else 0),
                    str(row.integration_id),
                ),
            )
            if len(group_rows) < 2 and survivor.integration_type == PROVIDER_PARENT[provider_name]:
                continue
            plans.append(
                MergePlan(
                    provider=provider_name,
                    group_key=key,
                    survivor=survivor,
                    records=group_rows,
                )
            )
    return plans


def build_sync_state_payload(
    row: IntegrationRecord,
    *,
    survivor_id: uuid.UUID,
    existing_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = dict(row.connection_metadata or {})
    legacy_cursor_metadata = {key: metadata[key] for key in CURSOR_KEYS if key in metadata}
    state_cursor_metadata = dict((existing_state or {}).get("cursor_metadata") or {})
    cursor_metadata = state_cursor_metadata or legacy_cursor_metadata
    legacy_webhook_metadata = {key: metadata[key] for key in WEBHOOK_KEYS if key in metadata}
    state_webhook_metadata = dict((existing_state or {}).get("webhook_metadata") or {})
    stale_webhook_metadata = state_webhook_metadata or legacy_webhook_metadata
    migration_metadata: dict[str, Any] = {
        "legacy_integration_id": str(row.integration_id),
        "legacy_integration_type": row.integration_type,
        "webhook_metadata_stale": bool(stale_webhook_metadata),
    }
    if stale_webhook_metadata:
        migration_metadata["legacy_webhook_metadata"] = stale_webhook_metadata

    return {
        "integration_id": survivor_id,
        "organisation_id": row.organisation_id,
        "capability": row.integration_type,
        "enabled": bool((existing_state or {}).get("enabled", row.is_active)),
        "last_synced_at": (existing_state or {}).get("last_synced_at") or row.last_synced_at,
        "last_sync_status": (existing_state or {}).get("last_sync_status") or row.last_sync_status,
        "last_sync_error": (existing_state or {}).get("last_sync_error") or row.last_sync_error,
        "consecutive_sync_failures": int(
            (existing_state or {}).get("consecutive_sync_failures")
            if (existing_state or {}).get("consecutive_sync_failures") is not None
            else row.consecutive_sync_failures
        ),
        "cursor_metadata": cursor_metadata,
        # Stored callback URLs contain the legacy integration id; clearing the
        # active subscription root makes the existing ensure task create fresh callbacks.
        "webhook_metadata": {},
        "sync_metadata": {"provider_parent_migration": migration_metadata},
    }


def ensure_no_remaining_references(references: list[IntegrationReference]) -> None:
    remaining = [ref for ref in references if ref.row_count > 0]
    if not remaining:
        return
    details = ", ".join(
        f"{ref.schema}.{ref.table_name}.{ref.column_name}={ref.row_count}" for ref in remaining
    )
    raise RemainingReferenceError(f"Retired integrations still have references: {details}")


def _connected_account_key(row: IntegrationRecord) -> str:
    if row.integration_email:
        return row.integration_email.strip().lower()
    if row.connection_id:
        return f"connection:{row.connection_id.strip().lower()}"
    return f"integration:{row.integration_id}"


def _parent_metadata(row: IntegrationRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in dict(row.connection_metadata or {}).items()
        if key not in CURSOR_KEYS and key not in WEBHOOK_KEYS
    }


def _json_serializer(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"{type(obj).__name__} is not JSON serializable")


def _safe_identifier(value: str) -> str:
    if not value.replace("_", "").isalnum():
        raise ValueError(f"Unsafe identifier: {value}")
    return value


def _qualified(schema: str, table_name: str) -> str:
    return f"{_safe_identifier(schema)}.{_safe_identifier(table_name)}"


def get_all_organisation_ids(engine: Engine) -> list[uuid.UUID]:
    with engine.connect() as connection:
        result = connection.execute(text("SELECT organisation_id FROM public.organisations"))
        return [uuid.UUID(str(row[0])) for row in result]


def schema_exists(connection: Connection, schema: str) -> bool:
    result = connection.execute(
        text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :schema"),
        {"schema": schema},
    )
    return result.fetchone() is not None


def table_column_exists(
    connection: Connection,
    *,
    schema: str,
    table_name: str,
    column_name: str,
) -> bool:
    result = connection.execute(
        text(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = :schema
              AND table_name = :table_name
              AND column_name = :column_name
            """
        ),
        {"schema": schema, "table_name": table_name, "column_name": column_name},
    )
    return result.fetchone() is not None


def load_integration_rows(
    connection: Connection,
    *,
    schema: str,
    organisation_id: uuid.UUID,
    provider: ProviderArg,
) -> list[IntegrationRecord]:
    providers = ("google", "microsoft") if provider == "all" else (provider,)
    integration_types = [value for name in providers for value in PROVIDER_TYPES[name]]
    query = text(
        f"""
        SELECT *
        FROM {_qualified(schema, "knowledge_integrations")}
        WHERE organisation_id = :organisation_id
          AND integration_type IN :integration_types
        ORDER BY created_by_user_id, integration_email, connection_id, integration_type
        """
    ).bindparams(bindparam("integration_types", expanding=True))
    rows = (
        connection.execute(
            query,
            {
                "organisation_id": str(organisation_id),
                "integration_types": integration_types,
            },
        )
        .mappings()
        .all()
    )
    return [IntegrationRecord.from_mapping(dict(row)) for row in rows]


def export_plan_rows(
    connection: Connection,
    *,
    output_dir: Path,
    schema: str,
    organisation_id: uuid.UUID,
    plan: MergePlan,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    ids = [str(plan.survivor.integration_id), *[str(value) for value in plan.retired_ids]]
    query = text(
        f"""
        SELECT *
        FROM {_qualified(schema, "knowledge_integrations")}
        WHERE organisation_id = :organisation_id
          AND integration_id IN :integration_ids
        """
    ).bindparams(bindparam("integration_ids", expanding=True))
    rows = (
        connection.execute(
            query,
            {"organisation_id": str(organisation_id), "integration_ids": ids},
        )
        .mappings()
        .all()
    )
    payload = {
        "organisation_id": str(organisation_id),
        "schema": schema,
        "provider": plan.provider,
        "survivor_id": str(plan.survivor.integration_id),
        "retired_ids": [str(value) for value in plan.retired_ids],
        "knowledge_integrations": [dict(row) for row in rows],
        "references": export_reference_rows(
            connection,
            schema=schema,
            organisation_id=organisation_id,
            retired_ids=plan.retired_ids,
        ),
    }
    filename = (
        f"{schema}_{plan.provider}_{str(plan.survivor.integration_id)[:8]}_"
        "knowledge_integrations.json"
    )
    with (output_dir / filename).open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=_json_serializer)


def export_reference_rows(
    connection: Connection,
    *,
    schema: str,
    organisation_id: uuid.UUID,
    retired_ids: list[uuid.UUID],
) -> dict[str, list[dict[str, Any]]]:
    if not retired_ids:
        return {}

    exported: dict[str, list[dict[str, Any]]] = {}
    for table_config in list(ORG_REWRITE_TABLES.values()) + list(MASTER_REWRITE_TABLES.values()):
        target_schema = table_config.schema or schema
        if not table_column_exists(
            connection,
            schema=target_schema,
            table_name=table_config.table_name,
            column_name=table_config.column_name,
        ):
            continue

        org_clause = ""
        params: dict[str, Any] = {"retired_ids": [str(value) for value in retired_ids]}
        if table_column_exists(
            connection,
            schema=target_schema,
            table_name=table_config.table_name,
            column_name="organisation_id",
        ):
            org_clause = "AND organisation_id = :organisation_id"
            params["organisation_id"] = str(organisation_id)

        query = text(
            f"""
            SELECT *
            FROM {_qualified(target_schema, table_config.table_name)}
            WHERE {_safe_identifier(table_config.column_name)} IN :retired_ids
            {org_clause}
            """
        ).bindparams(bindparam("retired_ids", expanding=True))
        rows = connection.execute(query, params).mappings().all()
        if rows:
            exported[f"{target_schema}.{table_config.table_name}"] = [dict(row) for row in rows]
    return exported


def upsert_capability_state(
    connection: Connection, *, schema: str, payload: dict[str, Any]
) -> None:
    query = text(
        f"""
        INSERT INTO {_qualified(schema, "integration_sync_states")} AS state (
            sync_state_id,
            organisation_id,
            integration_id,
            capability,
            enabled,
            last_synced_at,
            last_sync_status,
            last_sync_error,
            consecutive_sync_failures,
            cursor_metadata,
            webhook_metadata,
            sync_metadata,
            created_at,
            updated_at
        )
        VALUES (
            :sync_state_id,
            :organisation_id,
            :integration_id,
            :capability,
            :enabled,
            :last_synced_at,
            :last_sync_status,
            :last_sync_error,
            :consecutive_sync_failures,
            CAST(:cursor_metadata AS jsonb),
            CAST(:webhook_metadata AS jsonb),
            CAST(:sync_metadata AS jsonb),
            NOW(),
            NOW()
        )
        ON CONFLICT (integration_id, capability) DO UPDATE SET
            enabled = EXCLUDED.enabled,
            last_synced_at = COALESCE(EXCLUDED.last_synced_at, state.last_synced_at),
            last_sync_status = COALESCE(EXCLUDED.last_sync_status, state.last_sync_status),
            last_sync_error = COALESCE(EXCLUDED.last_sync_error, state.last_sync_error),
            consecutive_sync_failures = EXCLUDED.consecutive_sync_failures,
            cursor_metadata = state.cursor_metadata || EXCLUDED.cursor_metadata,
            webhook_metadata = EXCLUDED.webhook_metadata,
            sync_metadata = state.sync_metadata || EXCLUDED.sync_metadata,
            updated_at = NOW()
        """
    )
    connection.execute(
        query,
        {
            "sync_state_id": str(uuid.uuid4()),
            "organisation_id": str(payload["organisation_id"]),
            "integration_id": str(payload["integration_id"]),
            "capability": payload["capability"],
            "enabled": payload["enabled"],
            "last_synced_at": payload["last_synced_at"],
            "last_sync_status": payload["last_sync_status"],
            "last_sync_error": payload["last_sync_error"],
            "consecutive_sync_failures": payload["consecutive_sync_failures"],
            "cursor_metadata": json.dumps(payload["cursor_metadata"], default=_json_serializer),
            "webhook_metadata": json.dumps(payload["webhook_metadata"], default=_json_serializer),
            "sync_metadata": json.dumps(payload["sync_metadata"], default=_json_serializer),
        },
    )


def update_survivor_parent(connection: Connection, *, schema: str, plan: MergePlan) -> None:
    connection.execute(
        text(
            f"""
            UPDATE {_qualified(schema, "knowledge_integrations")}
            SET integration_type = :provider,
                name = :name,
                connection_metadata = CAST(:connection_metadata AS jsonb),
                is_active = true,
                auto_sync_enabled = true,
                updated_at = NOW()
            WHERE organisation_id = :organisation_id
              AND integration_id = :integration_id
            """
        ),
        {
            "provider": PROVIDER_PARENT[plan.provider],
            "name": "Google" if plan.provider == "google" else "Microsoft",
            "connection_metadata": json.dumps(_parent_metadata(plan.survivor)),
            "organisation_id": str(plan.survivor.organisation_id),
            "integration_id": str(plan.survivor.integration_id),
        },
    )


def load_existing_sync_state(
    connection: Connection,
    *,
    schema: str,
    integration_id: uuid.UUID,
    capability: str,
) -> dict[str, Any] | None:
    if not table_column_exists(
        connection,
        schema=schema,
        table_name="integration_sync_states",
        column_name="integration_id",
    ):
        return None
    query = text(
        f"""
        SELECT *
        FROM {_qualified(schema, "integration_sync_states")}
        WHERE integration_id = :integration_id
          AND capability = :capability
        ORDER BY updated_at DESC
        LIMIT 1
        """
    )
    row = (
        connection.execute(
            query,
            {"integration_id": str(integration_id), "capability": capability},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def delete_retired_sync_states(
    connection: Connection,
    *,
    schema: str,
    retired_ids: list[uuid.UUID],
) -> int:
    if not retired_ids:
        return 0

    if not table_column_exists(
        connection,
        schema=schema,
        table_name="integration_sync_states",
        column_name="integration_id",
    ):
        return 0
    query = text(
        f"""
        DELETE FROM {_qualified(schema, "integration_sync_states")}
        WHERE integration_id IN :retired_ids
        """
    ).bindparams(bindparam("retired_ids", expanding=True))
    return int(
        connection.execute(
            query,
            {"retired_ids": [str(value) for value in retired_ids]},
        ).rowcount
        or 0
    )


def rewrite_table(
    connection: Connection,
    *,
    rewrite_table: RewriteTable,
    schema: str,
    organisation_id: uuid.UUID,
    survivor_id: uuid.UUID,
    retired_ids: list[uuid.UUID],
) -> int:
    if not retired_ids:
        return 0

    target_schema = rewrite_table.schema or schema
    if not table_column_exists(
        connection,
        schema=target_schema,
        table_name=rewrite_table.table_name,
        column_name=rewrite_table.column_name,
    ):
        return 0

    _assert_no_unique_conflict(
        connection,
        schema=target_schema,
        table_name=rewrite_table.table_name,
        column_name=rewrite_table.column_name,
        survivor_id=survivor_id,
        retired_ids=retired_ids,
    )

    qualified = _qualified(target_schema, rewrite_table.table_name)
    org_clause = ""
    params: dict[str, Any] = {
        "survivor_id": str(survivor_id),
        "retired_ids": [str(value) for value in retired_ids],
    }
    if table_column_exists(
        connection,
        schema=target_schema,
        table_name=rewrite_table.table_name,
        column_name="organisation_id",
    ):
        org_clause = "AND organisation_id = :organisation_id"
        params["organisation_id"] = str(organisation_id)

    query = text(
        f"""
        UPDATE {qualified}
        SET {_safe_identifier(rewrite_table.column_name)} = :survivor_id
        WHERE {_safe_identifier(rewrite_table.column_name)} IN :retired_ids
        {org_clause}
        """
    ).bindparams(bindparam("retired_ids", expanding=True))
    return int(connection.execute(query, params).rowcount or 0)


def discover_integration_references(
    connection: Connection,
    *,
    org_schema: str,
    retired_ids: list[uuid.UUID],
) -> list[IntegrationReference]:
    if not retired_ids:
        return []

    refs: list[IntegrationReference] = []
    table_rows = connection.execute(
        text(
            """
            SELECT table_schema, table_name, column_name
            FROM information_schema.columns
            WHERE column_name = 'integration_id'
              AND table_schema IN (:org_schema, 'public')
              AND table_name <> 'knowledge_integrations'
            ORDER BY table_schema, table_name
            """
        ),
        {"org_schema": org_schema},
    ).mappings()
    for row in table_rows:
        schema = str(row["table_schema"])
        table_name = str(row["table_name"])
        column_name = str(row["column_name"])
        qualified = _qualified(schema, table_name)
        query = text(
            f"""
            SELECT count(*)
            FROM {qualified}
            WHERE {_safe_identifier(column_name)} IN :retired_ids
            """
        ).bindparams(bindparam("retired_ids", expanding=True))
        count = int(
            connection.execute(
                query, {"retired_ids": [str(value) for value in retired_ids]}
            ).scalar()
            or 0
        )
        if count:
            refs.append(
                IntegrationReference(
                    schema=schema,
                    table_name=table_name,
                    column_name=column_name,
                    row_count=count,
                )
            )
    return refs


def delete_retired_integrations(
    connection: Connection,
    *,
    schema: str,
    organisation_id: uuid.UUID,
    retired_ids: list[uuid.UUID],
) -> int:
    if not retired_ids:
        return 0

    query = text(
        f"""
        DELETE FROM {_qualified(schema, "knowledge_integrations")}
        WHERE organisation_id = :organisation_id
          AND integration_id IN :retired_ids
        """
    ).bindparams(bindparam("retired_ids", expanding=True))
    return int(
        connection.execute(
            query,
            {
                "organisation_id": str(organisation_id),
                "retired_ids": [str(value) for value in retired_ids],
            },
        ).rowcount
        or 0
    )


def process_org(
    connection: Connection,
    *,
    organisation_id: uuid.UUID,
    provider: ProviderArg,
    execute: bool,
    output_dir: Path,
) -> list[dict[str, Any]]:
    schema = get_schema_name(organisation_id)
    if not schema_exists(connection, schema):
        return [
            {"organisation_id": str(organisation_id), "schema": schema, "status": "missing_schema"}
        ]

    lock_key = int(organisation_id.int % 2147483647)
    connection.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})

    rows = load_integration_rows(
        connection,
        schema=schema,
        organisation_id=organisation_id,
        provider=provider,
    )
    plans = build_merge_plans(rows, provider=provider)
    summaries: list[dict[str, Any]] = []
    for plan in plans:
        table_counts: dict[str, int] = {}
        if execute:
            export_plan_rows(
                connection,
                output_dir=output_dir,
                schema=schema,
                organisation_id=organisation_id,
                plan=plan,
            )
            update_survivor_parent(connection, schema=schema, plan=plan)
            for row in plan.records:
                if row.integration_type in PROVIDER_TYPES[plan.provider]:
                    upsert_capability_state(
                        connection,
                        schema=schema,
                        payload=build_sync_state_payload(
                            row,
                            survivor_id=plan.survivor.integration_id,
                            existing_state=load_existing_sync_state(
                                connection,
                                schema=schema,
                                integration_id=row.integration_id,
                                capability=row.integration_type,
                            ),
                        ),
                    )
            deleted_legacy_states = delete_retired_sync_states(
                connection,
                schema=schema,
                retired_ids=plan.retired_ids,
            )
            table_counts["integration_sync_states"] = deleted_legacy_states
            for table_config in list(ORG_REWRITE_TABLES.values()) + list(
                MASTER_REWRITE_TABLES.values()
            ):
                if table_config.table_name == "integration_sync_states":
                    continue
                table_counts[table_config.table_name] = rewrite_table(
                    connection,
                    rewrite_table=table_config,
                    schema=schema,
                    organisation_id=organisation_id,
                    survivor_id=plan.survivor.integration_id,
                    retired_ids=plan.retired_ids,
                )
            remaining = discover_integration_references(
                connection,
                org_schema=schema,
                retired_ids=plan.retired_ids,
            )
            ensure_no_remaining_references(remaining)
            deleted = delete_retired_integrations(
                connection,
                schema=schema,
                organisation_id=organisation_id,
                retired_ids=plan.retired_ids,
            )
        else:
            deleted = 0
            for table_config in list(ORG_REWRITE_TABLES.values()) + list(
                MASTER_REWRITE_TABLES.values()
            ):
                target_schema = table_config.schema or schema
                if table_column_exists(
                    connection,
                    schema=target_schema,
                    table_name=table_config.table_name,
                    column_name=table_config.column_name,
                ):
                    table_counts[table_config.table_name] = _count_table_references(
                        connection,
                        schema=target_schema,
                        table_name=table_config.table_name,
                        column_name=table_config.column_name,
                        retired_ids=plan.retired_ids,
                    )
            remaining = discover_integration_references(
                connection,
                org_schema=schema,
                retired_ids=plan.retired_ids,
            )

        summaries.append(
            {
                "organisation_id": str(organisation_id),
                "schema": schema,
                "provider": plan.provider,
                "survivor_id": str(plan.survivor.integration_id),
                "retired_ids": [str(value) for value in plan.retired_ids],
                "rewrite_counts": table_counts,
                "remaining_references": [
                    {
                        "schema": ref.schema,
                        "table": ref.table_name,
                        "column": ref.column_name,
                        "count": ref.row_count,
                    }
                    for ref in remaining
                ],
                "deleted_retired_rows": deleted,
                "mode": "execute" if execute else "dry_run",
            }
        )
    if not plans:
        summaries.append(
            {
                "organisation_id": str(organisation_id),
                "schema": schema,
                "status": "no_merge_groups",
                "mode": "execute" if execute else "dry_run",
            }
        )
    return summaries


def _count_table_references(
    connection: Connection,
    *,
    schema: str,
    table_name: str,
    column_name: str,
    retired_ids: list[uuid.UUID],
) -> int:
    if not retired_ids:
        return 0

    query = text(
        f"""
        SELECT count(*)
        FROM {_qualified(schema, table_name)}
        WHERE {_safe_identifier(column_name)} IN :retired_ids
        """
    ).bindparams(bindparam("retired_ids", expanding=True))
    return int(
        connection.execute(query, {"retired_ids": [str(value) for value in retired_ids]}).scalar()
        or 0
    )


def _assert_no_unique_conflict(
    connection: Connection,
    *,
    schema: str,
    table_name: str,
    column_name: str,
    survivor_id: uuid.UUID,
    retired_ids: list[uuid.UUID],
) -> None:
    if not retired_ids:
        return

    constraints = connection.execute(
        text(
            """
            SELECT tc.constraint_name,
                   array_agg(kcu.column_name ORDER BY kcu.ordinal_position) AS columns
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_schema = kcu.constraint_schema
             AND tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
             AND tc.table_name = kcu.table_name
            WHERE tc.table_schema = :schema
              AND tc.table_name = :table_name
              AND tc.constraint_type IN ('UNIQUE', 'PRIMARY KEY')
            GROUP BY tc.constraint_name
            """
        ),
        {"schema": schema, "table_name": table_name},
    ).mappings()
    for constraint in constraints:
        columns = list(constraint["columns"] or [])
        if column_name not in columns or len(columns) == 1:
            continue
        compare_columns = [column for column in columns if column != column_name]
        predicates = " AND ".join(
            f"retired.{_safe_identifier(column)} IS NOT DISTINCT FROM survivor.{_safe_identifier(column)}"
            for column in compare_columns
        )
        query = text(
            f"""
            SELECT 1
            FROM {_qualified(schema, table_name)} retired
            JOIN {_qualified(schema, table_name)} survivor
              ON survivor.{_safe_identifier(column_name)} = :survivor_id
             AND {predicates}
            WHERE retired.{_safe_identifier(column_name)} IN :retired_ids
            LIMIT 1
            """
        ).bindparams(bindparam("retired_ids", expanding=True))
        conflict = connection.execute(
            query,
            {
                "survivor_id": str(survivor_id),
                "retired_ids": [str(value) for value in retired_ids],
            },
        ).fetchone()
        if conflict is not None:
            raise RuntimeError(
                f"Updating {schema}.{table_name}.{column_name} would violate "
                f"{constraint['constraint_name']}"
            )


def confirm(execute: bool, yes: bool, org_id: str | None, provider: str) -> None:
    if not execute or yes:
        return
    scope = org_id or "ALL organisations"
    response = (
        input(
            f"This will export, rewrite references, and delete retired {provider} rows for "
            f"{scope}. Continue? [y/N]: "
        )
        .strip()
        .lower()
    )
    if response not in {"y", "yes"}:
        print("Aborted.")
        sys.exit(0)


def main() -> None:
    args = parse_args()
    if args.execute and not args.yes:
        confirm(True, False, args.org_id, args.provider)

    engine = create_engine(load_database_url(), future=True)
    org_ids = [uuid.UUID(args.org_id)] if args.org_id else get_all_organisation_ids(engine)
    output_dir = Path(args.output_dir) / datetime.now().strftime("%Y%m%d_%H%M%S")

    all_summaries: list[dict[str, Any]] = []
    for organisation_id in org_ids:
        with engine.begin() as connection:
            summaries = process_org(
                connection,
                organisation_id=organisation_id,
                provider=args.provider,
                execute=args.execute,
                output_dir=output_dir,
            )
            all_summaries.extend(summaries)
        for summary in summaries:
            print(json.dumps(summary, indent=2, default=_json_serializer))

    print(
        json.dumps(
            {
                "mode": "execute" if args.execute else "dry_run",
                "organisations_checked": len(org_ids),
                "merge_groups": len(
                    [summary for summary in all_summaries if "survivor_id" in summary]
                ),
                "export_dir": str(output_dir) if args.execute else None,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
