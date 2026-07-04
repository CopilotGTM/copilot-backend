#!/usr/bin/env python3
"""Ensure app users have matching ``crm_users.user_id`` identities.

Dry-run is the default. The script uses the master DB connection and switches
``search_path`` per tenant schema, so it can cover tenant schemas even when the
per-tenant runtime secret is unavailable.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import uuid
from collections import Counter
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
    schema_name: str
    schema_exists: bool


@dataclass(frozen=True)
class AppUserRow:
    user_id: uuid.UUID
    email: str | None
    normalized_email: str | None
    first_name: str
    last_name: str
    user_role: str | None
    job_title: str | None
    status: str


@dataclass(frozen=True)
class CRMUserIdentityRow:
    user_id: uuid.UUID
    email: str | None
    normalized_email: str | None


@dataclass
class AppIdentityReport:
    organisation_id: str
    organisation_name: str
    schema_name: str
    mode: str
    app_users_checked: int = 0
    already_same_id: int = 0
    missing_same_id: int = 0
    missing_crm_email: int = 0
    same_email_different_id: int = 0
    ambiguous_app_email: int = 0
    ensured: int = 0
    created_internal_integrations: int = 0
    error: str | None = None
    examples: list[dict[str, Any]] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--org-id", type=uuid.UUID)
    target.add_argument("--all", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args()
    if args.execute and not args.yes:
        parser.error("--execute requires --yes")
    return args


def normalize_email(email: str | None) -> str | None:
    normalized = (email or "").strip().lower()
    return normalized or None


def set_org_search_path(session: Session, schema_name: str) -> None:
    session.execute(
        text("SELECT set_config('search_path', :search_path, true)"),
        {"search_path": f"{schema_name}, public"},
    )


def load_target_orgs(session: Session, org_id: uuid.UUID | None) -> list[TargetOrg]:
    rows = session.execute(
        text(
            """
            SELECT organisation_id, organisation_name
            FROM public.organisations
            WHERE (:org_id IS NULL OR organisation_id = :org_id)
            ORDER BY organisation_name
            """
        ),
        {"org_id": str(org_id) if org_id else None},
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
                schema_name=schema_name,
                schema_exists=schema_exists,
            )
        )
    return targets


def load_app_users(session: Session, organisation_id: uuid.UUID) -> list[AppUserRow]:
    rows = session.execute(
        text(
            """
            SELECT
                user_id,
                email,
                lower(btrim(email)) AS normalized_email,
                first_name,
                last_name,
                user_role::text AS user_role,
                job_title,
                status::text AS status
            FROM public.users
            WHERE organisation_id = :organisation_id
            ORDER BY email, user_id
            """
        ),
        {"organisation_id": str(organisation_id)},
    ).mappings()

    return [
        AppUserRow(
            user_id=uuid.UUID(str(row["user_id"])),
            email=str(row["email"]) if row["email"] else None,
            normalized_email=str(row["normalized_email"]) if row["normalized_email"] else None,
            first_name=str(row["first_name"]),
            last_name=str(row["last_name"]),
            user_role=str(row["user_role"]) if row["user_role"] else None,
            job_title=str(row["job_title"]) if row["job_title"] else None,
            status=str(row["status"]),
        )
        for row in rows
    ]


def load_crm_user_identities(session: Session) -> list[CRMUserIdentityRow]:
    rows = session.execute(
        text(
            """
            SELECT user_id, email, lower(btrim(email)) AS normalized_email
            FROM crm_users
            ORDER BY email, user_id
            """
        )
    ).mappings()
    return [
        CRMUserIdentityRow(
            user_id=uuid.UUID(str(row["user_id"])),
            email=str(row["email"]) if row["email"] else None,
            normalized_email=str(row["normalized_email"]) if row["normalized_email"] else None,
        )
        for row in rows
    ]


def get_or_create_integration_id(
    session: Session,
    *,
    organisation_id: uuid.UUID,
    actor_user: AppUserRow,
    execute: bool,
) -> tuple[uuid.UUID | None, bool]:
    integration_id = session.execute(
        text(
            """
            SELECT integration_id
            FROM knowledge_integrations
            WHERE organisation_id = :organisation_id
              AND integration_type = 'internal_crm'
            ORDER BY created_at NULLS LAST, integration_id
            LIMIT 1
            """
        ),
        {"organisation_id": str(organisation_id)},
    ).scalar_one_or_none()
    if integration_id:
        return uuid.UUID(str(integration_id)), False

    if not execute:
        return None, True

    new_integration_id = uuid.uuid4()
    session.execute(
        text(
            """
            INSERT INTO knowledge_integrations (
                integration_id,
                organisation_id,
                created_by_user_id,
                created_by_admin,
                integration_scope,
                integration_type,
                name,
                credentials,
                is_active,
                auto_sync_enabled,
                connection_metadata
            )
            VALUES (
                :integration_id,
                :organisation_id,
                :created_by_user_id,
                false,
                'organization',
                'internal_crm',
                'Internal CRM',
                '{}'::jsonb,
                true,
                true,
                '{}'::jsonb
            )
            """
        ),
        {
            "integration_id": str(new_integration_id),
            "organisation_id": str(organisation_id),
            "created_by_user_id": str(actor_user.user_id),
        },
    )
    return new_integration_id, True


def build_display_name(app_user: AppUserRow) -> str:
    return " ".join(part for part in [app_user.first_name, app_user.last_name] if part).strip()


def insert_crm_user_for_app_user(
    session: Session,
    *,
    organisation_id: uuid.UUID,
    integration_id: uuid.UUID,
    app_user: AppUserRow,
) -> None:
    full_name = build_display_name(app_user)
    session.execute(
        text(
            """
            INSERT INTO crm_users (
                user_id,
                organisation_id,
                integration_id,
                email,
                first_name,
                last_name,
                full_name,
                user_role,
                job_title,
                is_active,
                source_created_at,
                source_modified_at,
                synced_at,
                last_seen_at,
                is_deleted
            )
            VALUES (
                :user_id,
                :organisation_id,
                :integration_id,
                :email,
                :first_name,
                :last_name,
                :full_name,
                :user_role,
                :job_title,
                :is_active,
                now(),
                now(),
                now(),
                now(),
                false
            )
            """
        ),
        {
            "user_id": str(app_user.user_id),
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
            "email": app_user.email,
            "first_name": app_user.first_name,
            "last_name": app_user.last_name,
            "full_name": full_name,
            "user_role": app_user.user_role,
            "job_title": app_user.job_title,
            "is_active": app_user.status == "ACTIVE",
        },
    )


def inspect_or_ensure_org(
    *,
    session: Session,
    target: TargetOrg,
    execute: bool,
) -> AppIdentityReport:
    report = AppIdentityReport(
        organisation_id=str(target.organisation_id),
        organisation_name=target.organisation_name,
        schema_name=target.schema_name,
        mode="execute" if execute else "dry_run",
    )
    if not target.schema_exists:
        report.error = "schema does not exist"
        return report

    set_org_search_path(session, target.schema_name)
    app_users = load_app_users(session, target.organisation_id)
    report.app_users_checked = len(app_users)

    email_counts = Counter(user.normalized_email for user in app_users if user.normalized_email)
    crm_users = load_crm_user_identities(session)
    crm_ids = {crm_user.user_id for crm_user in crm_users}
    crm_by_email: dict[str, list[CRMUserIdentityRow]] = {}
    for crm_user in crm_users:
        if crm_user.normalized_email:
            crm_by_email.setdefault(crm_user.normalized_email, []).append(crm_user)

    integration_id: uuid.UUID | None = None
    created_integration_recorded = False
    for app_user in app_users:
        if app_user.user_id in crm_ids:
            report.already_same_id += 1
            continue

        report.missing_same_id += 1
        if app_user.normalized_email and email_counts[app_user.normalized_email] > 1:
            report.ambiguous_app_email += 1
            if len(report.examples) < 20:
                report.examples.append(
                    {
                        "reason": "ambiguous_app_email",
                        "email": app_user.normalized_email,
                        "app_user_id": str(app_user.user_id),
                    }
                )
            continue

        crm_email_rows = crm_by_email.get(app_user.normalized_email or "", [])
        if crm_email_rows:
            report.same_email_different_id += 1
        else:
            report.missing_crm_email += 1

        if len(report.examples) < 20:
            report.examples.append(
                {
                    "reason": (
                        "same_email_different_id" if crm_email_rows else "missing_crm_email"
                    ),
                    "email": app_user.normalized_email,
                    "app_user_id": str(app_user.user_id),
                    "crm_user_ids": [str(row.user_id) for row in crm_email_rows],
                }
            )

        if execute:
            if integration_id is None:
                integration_id, created_integration = get_or_create_integration_id(
                    session,
                    organisation_id=target.organisation_id,
                    actor_user=app_user,
                    execute=True,
                )
                if created_integration and not created_integration_recorded:
                    report.created_internal_integrations += 1
                    created_integration_recorded = True
            if integration_id is None:
                raise RuntimeError("Unable to resolve integration_id for crm_users insert")
            insert_crm_user_for_app_user(
                session,
                organisation_id=target.organisation_id,
                integration_id=integration_id,
                app_user=app_user,
            )
            report.ensured += 1
        else:
            _, would_create_integration = get_or_create_integration_id(
                session,
                organisation_id=target.organisation_id,
                actor_user=app_user,
                execute=False,
            )
            if would_create_integration and not created_integration_recorded:
                report.created_internal_integrations += 1
                created_integration_recorded = True

    if execute:
        session.commit()
    else:
        session.rollback()
    return report


def run(args: argparse.Namespace) -> dict[str, Any]:
    reports: list[AppIdentityReport] = []
    with master_session_scope() as session:
        targets = load_target_orgs(session, args.org_id)
        for target in targets:
            try:
                report = inspect_or_ensure_org(
                    session=session,
                    target=target,
                    execute=args.execute,
                )
            except Exception as exc:
                report = AppIdentityReport(
                    organisation_id=str(target.organisation_id),
                    organisation_name=target.organisation_name,
                    schema_name=target.schema_name,
                    mode="execute" if args.execute else "dry_run",
                    error=str(exc),
                )
                with contextlib.suppress(Exception):
                    session.rollback()
            reports.append(report)
            print(
                f"[ORG] {report.organisation_name} | checked={report.app_users_checked} "
                f"missing_same_id={report.missing_same_id} ensured={report.ensured} "
                f"ambiguous={report.ambiguous_app_email} error={report.error}"
            )

    summary = {
        "mode": "execute" if args.execute else "dry_run",
        "organisations_checked": len(reports),
        "errors": sum(1 for report in reports if report.error),
        "app_users_checked": sum(report.app_users_checked for report in reports),
        "already_same_id": sum(report.already_same_id for report in reports),
        "missing_same_id": sum(report.missing_same_id for report in reports),
        "missing_crm_email": sum(report.missing_crm_email for report in reports),
        "same_email_different_id": sum(
            report.same_email_different_id for report in reports
        ),
        "ambiguous_app_email": sum(report.ambiguous_app_email for report in reports),
        "ensured": sum(report.ensured for report in reports),
        "created_internal_integrations": sum(
            report.created_internal_integrations for report in reports
        ),
    }
    payload = {"organisations": [asdict(report) for report in reports], "summary": summary}
    print("[SUMMARY]")
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(payload, indent=2, sort_keys=True))
        print(f"Wrote JSON report: {args.output_json}")
    return payload


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_args()
    run(args)


if __name__ == "__main__":
    main()
