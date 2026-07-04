#!/usr/bin/env python3
"""Merge duplicate ``crm_users`` rows that share one normalized email.

Dry-run is the default. Use ``--execute --yes`` only after reviewing the
reported duplicate groups. The merge keeps one canonical CRM user ID per
``organisation_id + lower(trim(email))`` and rewrites dependent CRM references
before deleting duplicate ``crm_users`` rows.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from src.infra.database.sessions import master_session_scope  # noqa: E402
from src.infra.utils.resource_naming import get_schema_name  # noqa: E402


@dataclass(frozen=True)
class TargetOrg:
    organisation_id: uuid.UUID
    organisation_name: str
    status: str
    schema_name: str
    schema_exists: bool


@dataclass(frozen=True)
class CRMUserRow:
    user_id: uuid.UUID
    email: str
    normalized_email: str
    integration_id: uuid.UUID | None
    is_active: bool
    is_deleted: bool
    full_name: str | None
    synced_at: str | None
    updated_at: str | None


@dataclass(frozen=True)
class ReferenceSpec:
    table_name: str
    column_name: str
    predicate_sql: str | None = None

    @property
    def key(self) -> str:
        return f"{self.table_name}.{self.column_name}"


@dataclass
class MergePlan:
    normalized_email: str
    survivor_user_id: uuid.UUID
    loser_user_ids: list[uuid.UUID]
    duplicate_user_ids: list[uuid.UUID]
    app_user_id: uuid.UUID | None
    insert_survivor_from_user_id: uuid.UUID | None = None
    skipped_reason: str | None = None

    @property
    def can_execute(self) -> bool:
        return self.skipped_reason is None and len(self.loser_user_ids) > 0


@dataclass
class OrgMergeReport:
    organisation_id: str
    organisation_name: str
    schema_name: str
    mode: str
    duplicate_groups: int = 0
    executable_groups: int = 0
    skipped_groups: int = 0
    duplicate_rows: int = 0
    deleted_rows: int = 0
    inserted_survivors: int = 0
    reference_updates: dict[str, int] = field(default_factory=dict)
    source_mapping_updates: int = 0
    plans: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


# These columns are expected to carry crm_users.user_id values. Some are
# FK-constrained in current schemas, some are semantic/polymorphic references, and
# older tenant schemas can differ. Keep the static list as the safety net and
# augment it with live FK introspection below.
STATIC_REFERENCE_SPECS = (
    ReferenceSpec("ci_calls", "crm_owner_id"),
    ReferenceSpec("ci_call_speakers", "crm_user_id"),
    ReferenceSpec("integration_participants", "crm_user_id"),
    ReferenceSpec("crm_activities", "owner_id"),
    ReferenceSpec("crm_tasks", "assigned_to_user_id"),
    ReferenceSpec("crm_tasks", "created_by_user_id"),
    ReferenceSpec("crm_tasks", "owner_user_id"),
    ReferenceSpec("crm_tasks", "completed_by_user_id"),
    ReferenceSpec("crm_notes", "author_user_id"),
    ReferenceSpec("crm_companies", "owner_id"),
    ReferenceSpec("crm_contacts", "owner_id"),
    ReferenceSpec("lead_forms", "created_by_user_id"),
    ReferenceSpec("lead_forms", "updated_by_user_id"),
    ReferenceSpec("crm_leads", "owner_id"),
    ReferenceSpec("crm_leads", "created_by_user_id"),
    ReferenceSpec("crm_leads", "updated_by_user_id"),
    ReferenceSpec("crm_deals", "owner_id"),
    ReferenceSpec("crm_deals", "created_by_user_id"),
    ReferenceSpec("crm_deals", "updated_by_user_id"),
    ReferenceSpec("crm_lead_status_history", "actor_user_id"),
    ReferenceSpec("crm_deal_participants", "related_local_id", "participant_type = 'USER'"),
    ReferenceSpec("crm_lead_participants", "related_local_id", "participant_type = 'USER'"),
    ReferenceSpec("crm_activity_links", "object_id", "object_type = 'USER'"),
    ReferenceSpec("crm_activities", "related_local_id", "related_object_type = 'USER'"),
    ReferenceSpec("crm_object_associations", "from_local_id", "from_object_type = 'USER'"),
    ReferenceSpec("crm_object_associations", "to_local_id", "to_object_type = 'USER'"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry-run or merge duplicate crm_users rows by normalized email."
    )
    parser.add_argument("--org-id", type=uuid.UUID, help="Organisation UUID to process.")
    parser.add_argument("--all", action="store_true", help="Process all non-failed organisations.")
    parser.add_argument("--execute", action="store_true", help="Persist merge changes.")
    parser.add_argument("--yes", action="store_true", help="Required with --execute.")
    parser.add_argument(
        "--output-json",
        type=Path,
        help="Optional path for a JSON report. Parent directory is created if needed.",
    )
    parser.add_argument(
        "--include-failed-orgs",
        action="store_true",
        help="Include FAILED organisations when --all is used.",
    )
    return parser.parse_args()


def normalize_email(email: str | None) -> str | None:
    normalized = (email or "").strip().lower()
    return normalized or None


def choose_template_row(rows: list[CRMUserRow], app_user_id: uuid.UUID | None) -> CRMUserRow:
    def score(row: CRMUserRow) -> tuple[int, int, int, str]:
        return (
            1 if app_user_id and row.user_id == app_user_id else 0,
            1 if row.is_active else 0,
            1 if not row.is_deleted else 0,
            row.synced_at or row.updated_at or "",
        )

    return max(rows, key=score)


def build_merge_plan(
    normalized_email: str,
    rows: list[CRMUserRow],
    app_user_ids: list[uuid.UUID],
    all_crm_user_ids: set[uuid.UUID],
) -> MergePlan:
    duplicate_user_ids = [row.user_id for row in rows]
    if len(app_user_ids) > 1:
        return MergePlan(
            normalized_email=normalized_email,
            survivor_user_id=rows[0].user_id,
            loser_user_ids=[],
            duplicate_user_ids=duplicate_user_ids,
            app_user_id=None,
            skipped_reason="multiple public.users rows match this email in the organisation",
        )

    app_user_id = app_user_ids[0] if app_user_ids else None
    template = choose_template_row(rows, app_user_id)
    insert_survivor_from_user_id: uuid.UUID | None = None

    if app_user_id is None:
        survivor_user_id = template.user_id
    elif app_user_id in duplicate_user_ids:
        survivor_user_id = app_user_id
    elif app_user_id in all_crm_user_ids:
        return MergePlan(
            normalized_email=normalized_email,
            survivor_user_id=app_user_id,
            loser_user_ids=[],
            duplicate_user_ids=duplicate_user_ids,
            app_user_id=app_user_id,
            skipped_reason="public user already has a different crm_users row",
        )
    else:
        survivor_user_id = app_user_id
        insert_survivor_from_user_id = template.user_id

    loser_user_ids = [user_id for user_id in duplicate_user_ids if user_id != survivor_user_id]
    return MergePlan(
        normalized_email=normalized_email,
        survivor_user_id=survivor_user_id,
        loser_user_ids=loser_user_ids,
        duplicate_user_ids=duplicate_user_ids,
        app_user_id=app_user_id,
        insert_survivor_from_user_id=insert_survivor_from_user_id,
    )


def load_target_orgs(
    session: Session,
    *,
    org_id: uuid.UUID | None,
    run_all: bool,
    include_failed_orgs: bool,
) -> list[TargetOrg]:
    if not run_all and org_id is None:
        return []

    where_sql = ""
    params: dict[str, Any] = {}
    if org_id is not None:
        where_sql = "WHERE organisation_id = :org_id"
        params["org_id"] = str(org_id)
    elif not include_failed_orgs:
        where_sql = "WHERE status <> 'FAILED'"

    org_query = f"""
            SELECT organisation_id, organisation_name, status
            FROM public.organisations
            {where_sql}
            ORDER BY organisation_name
            """  # noqa: S608 - where_sql is selected from fixed local clauses above.
    rows = session.execute(
        text(org_query),
        params,
    ).mappings()

    targets: list[TargetOrg] = []
    for row in rows:
        organisation_id = uuid.UUID(str(row["organisation_id"]))
        schema_name = get_schema_name(organisation_id)
        schema_exists = (
            session.execute(
                text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :schema"),
                {"schema": schema_name},
            ).first()
            is not None
        )
        targets.append(
            TargetOrg(
                organisation_id=organisation_id,
                organisation_name=str(row["organisation_name"]),
                status=str(row["status"]),
                schema_name=schema_name,
                schema_exists=schema_exists,
            )
        )
    return targets


def load_duplicate_crm_users(session: Session) -> dict[str, list[CRMUserRow]]:
    rows = session.execute(
        text(
            """
            WITH duplicate_emails AS (
                SELECT lower(btrim(email)) AS normalized_email
                FROM crm_users
                WHERE email IS NOT NULL AND btrim(email) <> ''
                GROUP BY lower(btrim(email))
                HAVING count(*) > 1
            )
            SELECT
                user_id,
                email,
                lower(btrim(email)) AS normalized_email,
                integration_id,
                is_active,
                is_deleted,
                full_name,
                synced_at::text AS synced_at,
                updated_at::text AS updated_at
            FROM crm_users
            WHERE lower(btrim(email)) IN (SELECT normalized_email FROM duplicate_emails)
            ORDER BY lower(btrim(email)), is_deleted, is_active DESC, synced_at DESC NULLS LAST
            """
        )
    ).mappings()

    groups: dict[str, list[CRMUserRow]] = {}
    for row in rows:
        normalized_email = str(row["normalized_email"])
        groups.setdefault(normalized_email, []).append(
            CRMUserRow(
                user_id=uuid.UUID(str(row["user_id"])),
                email=str(row["email"]),
                normalized_email=normalized_email,
                integration_id=(
                    uuid.UUID(str(row["integration_id"])) if row["integration_id"] else None
                ),
                is_active=bool(row["is_active"]),
                is_deleted=bool(row["is_deleted"]),
                full_name=str(row["full_name"]) if row["full_name"] else None,
                synced_at=str(row["synced_at"]) if row["synced_at"] else None,
                updated_at=str(row["updated_at"]) if row["updated_at"] else None,
            )
        )
    return groups


def load_app_user_ids_by_email(
    session: Session,
    organisation_id: uuid.UUID,
    normalized_emails: set[str],
) -> dict[str, list[uuid.UUID]]:
    if not normalized_emails:
        return {}

    rows = session.execute(
        text(
            """
            SELECT lower(btrim(email)) AS normalized_email, user_id
            FROM public.users
            WHERE organisation_id = :organisation_id
              AND lower(btrim(email)) = ANY(:emails)
            """
        ),
        {"organisation_id": str(organisation_id), "emails": list(normalized_emails)},
    ).mappings()

    result: dict[str, list[uuid.UUID]] = {}
    for row in rows:
        result.setdefault(str(row["normalized_email"]), []).append(uuid.UUID(str(row["user_id"])))
    return result


def load_all_crm_user_ids(session: Session) -> set[uuid.UUID]:
    rows = session.execute(text("SELECT user_id FROM crm_users")).scalars().all()
    return {uuid.UUID(str(row)) for row in rows}


def table_exists(session: Session, table_name: str) -> bool:
    return (
        session.execute(
            text(
                """
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = current_schema()
                  AND table_name = :table_name
                """
            ),
            {"table_name": table_name},
        ).first()
        is not None
    )


def column_exists(session: Session, table_name: str, column_name: str) -> bool:
    return (
        session.execute(
            text(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = :table_name
                  AND column_name = :column_name
                """
            ),
            {"table_name": table_name, "column_name": column_name},
        ).first()
        is not None
    )


def load_fk_reference_specs(session: Session) -> list[ReferenceSpec]:
    rows = session.execute(
        text(
            """
            SELECT kcu.table_name, kcu.column_name
            FROM information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage AS ccu
              ON ccu.constraint_name = tc.constraint_name
             AND ccu.constraint_schema = tc.constraint_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = current_schema()
              AND ccu.table_schema = current_schema()
              AND ccu.table_name = 'crm_users'
              AND ccu.column_name = 'user_id'
            ORDER BY kcu.table_name, kcu.column_name
            """
        )
    ).mappings()
    return [ReferenceSpec(str(row["table_name"]), str(row["column_name"])) for row in rows]


def load_reference_specs(session: Session) -> list[ReferenceSpec]:
    specs = {spec.key: spec for spec in load_fk_reference_specs(session)}
    for spec in STATIC_REFERENCE_SPECS:
        if table_exists(session, spec.table_name) and column_exists(
            session, spec.table_name, spec.column_name
        ):
            specs.setdefault(spec.key, spec)
    return list(specs.values())


def quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def set_org_search_path(session: Session, schema_name: str) -> None:
    session.execute(
        text("SELECT set_config('search_path', :search_path, true)"),
        {"search_path": f"{schema_name}, public"},
    )


def count_reference_rows(
    session: Session,
    spec: ReferenceSpec,
    loser_user_ids: list[uuid.UUID],
) -> int:
    predicate = f" AND {spec.predicate_sql}" if spec.predicate_sql else ""
    reference_count_query = f"""
            SELECT count(*)
            FROM {quoted_identifier(spec.table_name)}
            WHERE {quoted_identifier(spec.column_name)} = ANY(:loser_user_ids)
            {predicate}
            """  # noqa: S608 - table/column names come from introspection/static specs.
    result = session.execute(
        text(reference_count_query),
        {"loser_user_ids": loser_user_ids},
    ).scalar_one()
    return int(result)


def update_reference_rows(
    session: Session,
    spec: ReferenceSpec,
    plan: MergePlan,
) -> int:
    predicate = f" AND {spec.predicate_sql}" if spec.predicate_sql else ""
    reference_update_query = f"""
            UPDATE {quoted_identifier(spec.table_name)}
            SET {quoted_identifier(spec.column_name)} = :survivor_user_id
            WHERE {quoted_identifier(spec.column_name)} = ANY(:loser_user_ids)
            {predicate}
            """  # noqa: S608 - table/column names come from introspection/static specs.
    result = session.execute(
        text(reference_update_query),
        {
            "survivor_user_id": str(plan.survivor_user_id),
            "loser_user_ids": plan.loser_user_ids,
        },
    )
    return int(getattr(result, "rowcount", 0) or 0)


def count_source_mapping_rows(session: Session, loser_user_ids: list[uuid.UUID]) -> int:
    if not table_exists(session, "crm_source_object_mappings"):
        return 0
    result = session.execute(
        text(
            """
            SELECT count(*)
            FROM crm_source_object_mappings
            WHERE local_object_type = 'user'
              AND local_object_id = ANY(:loser_user_ids)
            """
        ),
        {"loser_user_ids": loser_user_ids},
    ).scalar_one()
    return int(result)


def update_source_mapping_rows(session: Session, plan: MergePlan) -> int:
    if not table_exists(session, "crm_source_object_mappings"):
        return 0
    result = session.execute(
        text(
            """
            UPDATE crm_source_object_mappings
            SET local_object_id = :survivor_user_id
            WHERE local_object_type = 'user'
              AND local_object_id = ANY(:loser_user_ids)
            """
        ),
        {
            "survivor_user_id": str(plan.survivor_user_id),
            "loser_user_ids": plan.loser_user_ids,
        },
    )
    return int(getattr(result, "rowcount", 0) or 0)


def insert_survivor_from_template(session: Session, plan: MergePlan) -> None:
    if plan.insert_survivor_from_user_id is None:
        return

    columns = [
        str(row)
        for row in session.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'crm_users'
                  AND column_name <> 'user_id'
                ORDER BY ordinal_position
                """
            )
        ).scalars()
    ]
    quoted_columns = ", ".join(quoted_identifier(column) for column in columns)
    insert_query = f"""
            INSERT INTO crm_users (user_id, {quoted_columns})
            SELECT :survivor_user_id, {quoted_columns}
            FROM crm_users
            WHERE user_id = :template_user_id
            """  # noqa: S608 - column list comes from crm_users information_schema.
    session.execute(
        text(insert_query),
        {
            "survivor_user_id": str(plan.survivor_user_id),
            "template_user_id": str(plan.insert_survivor_from_user_id),
        },
    )


def delete_loser_rows(session: Session, plan: MergePlan) -> int:
    result = session.execute(
        text("DELETE FROM crm_users WHERE user_id = ANY(:loser_user_ids)"),
        {"loser_user_ids": plan.loser_user_ids},
    )
    return int(getattr(result, "rowcount", 0) or 0)


def plan_to_dict(plan: MergePlan, rows: list[CRMUserRow]) -> dict[str, Any]:
    def row_to_dict(row: CRMUserRow) -> dict[str, Any]:
        payload = asdict(row)
        payload["user_id"] = str(row.user_id)
        payload["integration_id"] = str(row.integration_id) if row.integration_id else None
        return payload

    return {
        "normalized_email": plan.normalized_email,
        "survivor_user_id": str(plan.survivor_user_id),
        "loser_user_ids": [str(user_id) for user_id in plan.loser_user_ids],
        "duplicate_user_ids": [str(user_id) for user_id in plan.duplicate_user_ids],
        "app_user_id": str(plan.app_user_id) if plan.app_user_id else None,
        "insert_survivor_from_user_id": (
            str(plan.insert_survivor_from_user_id) if plan.insert_survivor_from_user_id else None
        ),
        "skipped_reason": plan.skipped_reason,
        "rows": [row_to_dict(row) for row in rows],
    }


def process_org(
    *,
    target: TargetOrg,
    master_session: Session,
    execute: bool,
) -> OrgMergeReport:
    report = OrgMergeReport(
        organisation_id=str(target.organisation_id),
        organisation_name=target.organisation_name,
        schema_name=target.schema_name,
        mode="execute" if execute else "dry_run",
    )
    if not target.schema_exists:
        report.error = "schema does not exist"
        return report

    set_org_search_path(master_session, target.schema_name)
    org_session = master_session
    try:
        duplicate_groups = load_duplicate_crm_users(org_session)
        report.duplicate_groups = len(duplicate_groups)
        report.duplicate_rows = sum(len(rows) for rows in duplicate_groups.values())
        if not duplicate_groups:
            return report

        app_users_by_email = load_app_user_ids_by_email(
            master_session,
            target.organisation_id,
            set(duplicate_groups),
        )
        all_crm_user_ids = load_all_crm_user_ids(org_session)
        reference_specs = load_reference_specs(org_session)

        for normalized_email, rows in duplicate_groups.items():
            plan = build_merge_plan(
                normalized_email,
                rows,
                app_users_by_email.get(normalized_email, []),
                all_crm_user_ids,
            )
            plan_report = plan_to_dict(plan, rows)
            if not plan.can_execute:
                report.skipped_groups += 1
                report.plans.append(plan_report)
                continue

            report.executable_groups += 1
            for spec in reference_specs:
                count = count_reference_rows(org_session, spec, plan.loser_user_ids)
                if count:
                    report.reference_updates[spec.key] = (
                        report.reference_updates.get(spec.key, 0) + count
                    )
                    plan_report.setdefault("reference_updates", {})[spec.key] = count

            source_mapping_count = count_source_mapping_rows(org_session, plan.loser_user_ids)
            if source_mapping_count:
                report.source_mapping_updates += source_mapping_count
                plan_report["source_mapping_updates"] = source_mapping_count

            if execute:
                insert_survivor_from_template(org_session, plan)
                if plan.insert_survivor_from_user_id is not None:
                    report.inserted_survivors += 1
                    all_crm_user_ids.add(plan.survivor_user_id)

                for spec in reference_specs:
                    updated = update_reference_rows(org_session, spec, plan)
                    if updated:
                        plan_report.setdefault("executed_reference_updates", {})[spec.key] = updated
                executed_mapping_updates = update_source_mapping_rows(org_session, plan)
                if executed_mapping_updates:
                    plan_report["executed_source_mapping_updates"] = executed_mapping_updates
                deleted = delete_loser_rows(org_session, plan)
                report.deleted_rows += deleted
                plan_report["deleted_rows"] = deleted
                for loser_id in plan.loser_user_ids:
                    all_crm_user_ids.discard(loser_id)

            report.plans.append(plan_report)

        if execute:
            org_session.commit()
        else:
            org_session.rollback()
    except Exception:
        org_session.rollback()
        raise
    return report


def write_json_report(path: Path, reports: list[OrgMergeReport], summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "summary": summary,
                "organisations": [asdict(report) for report in reports],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def main() -> None:
    load_dotenv()
    args = parse_args()

    if not args.all and args.org_id is None:
        raise SystemExit("Pass --org-id <UUID> or --all.")
    if args.execute and not args.yes:
        raise SystemExit("Refusing to execute without --yes.")

    reports: list[OrgMergeReport] = []
    with master_session_scope() as master_session:
        targets = load_target_orgs(
            master_session,
            org_id=args.org_id,
            run_all=args.all,
            include_failed_orgs=args.include_failed_orgs,
        )
        print(
            f"Resolved {len(targets)} organisation(s). "
            f"Mode={'EXECUTE' if args.execute else 'DRY RUN'}"
        )
        for target in targets:
            print(f"\n[ORG] {target.organisation_name} | {target.organisation_id}")
            try:
                report = process_org(
                    target=target,
                    master_session=master_session,
                    execute=args.execute,
                )
            except Exception as exc:
                report = OrgMergeReport(
                    organisation_id=str(target.organisation_id),
                    organisation_name=target.organisation_name,
                    schema_name=target.schema_name,
                    mode="execute" if args.execute else "dry_run",
                    error=str(exc),
                )
                with contextlib.suppress(Exception):
                    master_session.rollback()
            reports.append(report)
            if report.error:
                print(f"  ERROR: {report.error}")
                continue
            print(
                "  duplicate_groups={duplicate_groups} executable={executable_groups} "
                "skipped={skipped_groups} duplicate_rows={duplicate_rows} "
                "deleted={deleted_rows} source_mapping_updates={source_mapping_updates}".format(
                    **asdict(report)
                )
            )

    summary = {
        "mode": "execute" if args.execute else "dry_run",
        "organisations_checked": len(reports),
        "organisations_with_duplicates": sum(1 for report in reports if report.duplicate_groups),
        "duplicate_groups": sum(report.duplicate_groups for report in reports),
        "executable_groups": sum(report.executable_groups for report in reports),
        "skipped_groups": sum(report.skipped_groups for report in reports),
        "duplicate_rows": sum(report.duplicate_rows for report in reports),
        "deleted_rows": sum(report.deleted_rows for report in reports),
        "inserted_survivors": sum(report.inserted_survivors for report in reports),
        "source_mapping_updates": sum(report.source_mapping_updates for report in reports),
        "errors": sum(1 for report in reports if report.error),
    }
    print("\n[SUMMARY]")
    print(json.dumps(summary, indent=2, sort_keys=True))

    if args.output_json:
        write_json_report(args.output_json, reports, summary)
        print(f"\nWrote JSON report: {args.output_json}")

    if not args.execute:
        print("\nDry run only. Re-run with --execute --yes after reviewing the report.")


if __name__ == "__main__":
    main()
