"""Audit and optionally backfill Gmail webhook routes in the master schema.

The default mode is read-only. It reads each active approved tenant once, compares
its enabled Gmail integrations with the public connection locator, and prints
counts plus identifiers for records that need attention. Email addresses are
intentionally omitted from output.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from src.infra.database.sessions import master_session_scope, org_session_scope


@dataclass(frozen=True)
class GmailRoute:
    """Tenant-owned Gmail route that should have a public locator entry."""

    connection_id: str
    organisation_id: str
    integration_id: str
    integration_email: str | None


@dataclass(frozen=True)
class MasterRoute:
    """Current public locator values for one provider connection."""

    connection_id: str
    organisation_id: str
    integration_id: str
    integration_email: str | None


@dataclass(frozen=True)
class PlannedRoute:
    """One route classified for reporting or a guarded update."""

    status: str
    route: GmailRoute


def normalize_email(value: str | None) -> str | None:
    """Return the persisted routing-key representation."""
    return value.strip().lower() if value and value.strip() else None


def build_plan(
    gmail_routes: list[GmailRoute],
    master_routes: dict[str, MasterRoute],
) -> list[PlannedRoute]:
    """Classify routes without reading or mutating a database."""
    plan: list[PlannedRoute] = []
    for route in gmail_routes:
        email = normalize_email(route.integration_email)
        mapping = master_routes.get(route.connection_id)
        if email is None:
            status = "blank_email"
        elif mapping is None:
            status = "missing_mapping"
        elif (
            mapping.organisation_id != route.organisation_id
            or mapping.integration_id != route.integration_id
        ):
            status = "mismatched_mapping"
        elif normalize_email(mapping.integration_email) == email:
            status = "routable"
        else:
            status = "would_update"
        plan.append(PlannedRoute(status=status, route=route))
    return plan


def _load_organisation_ids(master_session: Any) -> list[str]:
    result = master_session.execute(
        text(
            """
            SELECT organisation_id::text
            FROM public.organisations
            WHERE is_active IS TRUE AND status = 'APPROVED'
            ORDER BY organisation_id
            """
        )
    )
    return list(result.scalars().all())


def _master_column_exists(master_session: Any) -> bool:
    return bool(
        master_session.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'organisation_integration_details'
                      AND column_name = 'integration_email'
                )
                """
            )
        ).scalar_one()
    )


def _load_master_routes(master_session: Any, *, has_email_column: bool) -> dict[str, MasterRoute]:
    query = (
        text(
            """
            SELECT connection_id::text, organisation_id::text, integration_id::text,
                   integration_email
            FROM public.organisation_integration_details
            """
        )
        if has_email_column
        else text(
            """
            SELECT connection_id::text, organisation_id::text, integration_id::text,
                   NULL::varchar AS integration_email
            FROM public.organisation_integration_details
            """
        )
    )
    result = master_session.execute(query)
    return {
        row.connection_id: MasterRoute(
            connection_id=row.connection_id,
            organisation_id=row.organisation_id,
            integration_id=row.integration_id,
            integration_email=row.integration_email,
        )
        for row in result
    }


def _route_query(*, all_integrations: bool) -> Any:
    """Select the broad or Gmail-only tenant source without dynamic SQL."""
    if all_integrations:
        return text(
            """
            SELECT ki.connection_id, ki.organisation_id::text, ki.integration_id::text,
                   ki.integration_email
            FROM knowledge_integrations AS ki
            WHERE ki.organisation_id = CAST(:organisation_id AS uuid)
              AND ki.is_active IS TRUE
              AND ki.connection_id IS NOT NULL
            ORDER BY ki.integration_id
            """
        )
    return text(
        """
        SELECT ki.connection_id, ki.organisation_id::text, ki.integration_id::text,
               ki.integration_email
        FROM knowledge_integrations AS ki
        JOIN integration_sync_states AS state
          ON state.integration_id = ki.integration_id
         AND state.organisation_id = ki.organisation_id
        WHERE ki.organisation_id = CAST(:organisation_id AS uuid)
          AND ki.integration_type = 'google'
          AND ki.is_active IS TRUE
          AND ki.connection_id IS NOT NULL
          AND state.capability = 'gmail'
          AND state.enabled IS TRUE
        ORDER BY ki.integration_id
        """
    )


def _load_routes(
    organisation_id: str,
    *,
    all_integrations: bool,
) -> list[GmailRoute]:
    """Load active connected integrations, optionally restricted to enabled Gmail."""
    with org_session_scope(organisation_id) as session:
        result = session.execute(
            _route_query(all_integrations=all_integrations),
            {"organisation_id": organisation_id},
        )
        return [
            GmailRoute(
                connection_id=str(row.connection_id),
                organisation_id=row.organisation_id,
                integration_id=row.integration_id,
                integration_email=row.integration_email,
            )
            for row in result
        ]


def _apply_updates(master_session: Any, plan: list[PlannedRoute]) -> int:
    updated = 0
    for item in plan:
        if item.status != "would_update":
            continue
        route = item.route
        result = master_session.execute(
            text(
                """
                UPDATE public.organisation_integration_details
                SET integration_email = :integration_email, updated_at = now()
                WHERE connection_id = CAST(:connection_id AS uuid)
                  AND organisation_id = CAST(:organisation_id AS uuid)
                  AND integration_id = CAST(:integration_id AS uuid)
                """
            ),
            {
                "integration_email": normalize_email(route.integration_email),
                "connection_id": route.connection_id,
                "organisation_id": route.organisation_id,
                "integration_id": route.integration_id,
            },
        )
        updated += result.rowcount
    master_session.commit()
    return updated


def run(*, execute: bool = False, all_integrations: bool = False) -> dict[str, Any]:
    """Build the environment report and optionally apply exact-match updates."""
    with master_session_scope() as master_session:
        organisation_ids = _load_organisation_ids(master_session)
        has_email_column = _master_column_exists(master_session)
        master_routes = _load_master_routes(
            master_session,
            has_email_column=has_email_column,
        )

        routes: list[GmailRoute] = []
        tenant_errors: list[str] = []
        for organisation_id in organisation_ids:
            try:
                routes.extend(
                    _load_routes(
                        organisation_id,
                        all_integrations=all_integrations,
                    )
                )
            except Exception:
                # A tenant permission or schema problem belongs in the report;
                # it must not hide healthy organisations from the audit.
                tenant_errors.append(organisation_id)

        plan = build_plan(routes, master_routes)
        counts = Counter(item.status for item in plan)
        email_counts = Counter(
            email
            for item in plan
            if (email := normalize_email(item.route.integration_email)) is not None
        )
        duplicate_email_groups = sum(count > 1 for count in email_counts.values())

        if execute and not has_email_column:
            raise RuntimeError("Run the master migration before --execute")
        updated = _apply_updates(master_session, plan) if execute else 0

    return {
        "mode": "execute" if execute else "dry-run",
        "migration_required": not has_email_column,
        "route_scope": "all-integrations" if all_integrations else "gmail",
        "active_approved_organisations": len(organisation_ids),
        "integration_routes": len(routes),
        "routable": counts["routable"],
        "would_update": counts["would_update"],
        "blank_email": counts["blank_email"],
        "missing_mapping": counts["missing_mapping"],
        "mismatched_mapping": counts["mismatched_mapping"],
        "duplicate_email_groups": duplicate_email_groups,
        "tenant_errors": tenant_errors,
        "updated": updated,
        "attention": [
            {
                "status": item.status,
                "connection_id": item.route.connection_id,
                "organisation_id": item.route.organisation_id,
                "integration_id": item.route.integration_id,
            }
            for item in plan
            if item.status not in {"routable", "would_update"}
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Write normalized emails for exact master/tenant matches.",
    )
    parser.add_argument(
        "--all-integrations",
        action="store_true",
        help="Backfill every active connected integration, not only Gmail.",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                execute=args.execute,
                all_integrations=args.all_integrations,
            ),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
