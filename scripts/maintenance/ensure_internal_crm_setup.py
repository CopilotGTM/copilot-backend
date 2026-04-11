#!/usr/bin/env python3
"""Ensure internal CRM integration + default pipeline exist for organisations.

This script iterates organisation schemas and creates the internal CRM integration
and default pipeline/stages if missing. It is idempotent.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv
from sqlalchemy import select, text
from src.domain.models.master_tables import (
    Organisation,
    OrganisationStatus,
)
from src.infra.database.sessions import (
    acquire_master_session,
    acquire_org_async_session,
)
from src.infra.utils.resource_naming import get_schema_name
from src.service.crm.internal.internal_crm_setup_service import (
    InternalCRMSetupService,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))


SYSTEM_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


@dataclass(frozen=True)
class TargetOrg:
    organisation_id: UUID
    organisation_name: str
    status: str
    schema_name: str
    schema_exists: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ensure internal CRM integration + default pipeline exist for organisations."
    )
    parser.add_argument(
        "--org-id",
        type=UUID,
        help="Optional organisation UUID to target (default: none).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process all organisations except FAILED.",
    )
    parser.add_argument(
        "--user-id",
        type=UUID,
        help="User UUID to attribute integration creation (default: system user).",
    )
    return parser.parse_args()


def _schema_exists(session, schema_name: str) -> bool:
    result = session.execute(
        text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :schema"),
        {"schema": schema_name},
    )
    return result.fetchone() is not None


def load_target_orgs(org_id: UUID | None, run_all: bool) -> list[TargetOrg]:
    session_gen = acquire_master_session()
    session = next(session_gen)
    try:
        if run_all:
            stmt = select(Organisation).where(Organisation.status != OrganisationStatus.FAILED)
        else:
            if not org_id:
                return []
            stmt = select(Organisation).where(Organisation.organisation_id == org_id)

        orgs = list(session.execute(stmt).scalars().all())
        targets: list[TargetOrg] = []
        for org in orgs:
            schema_name = get_schema_name(org.organisation_id)
            targets.append(
                TargetOrg(
                    organisation_id=org.organisation_id,
                    organisation_name=org.organisation_name,
                    status=org.status.value,
                    schema_name=schema_name,
                    schema_exists=_schema_exists(session, schema_name),
                )
            )
        return targets
    finally:
        with contextlib.suppress(Exception):
            session.close()
        with contextlib.suppress(Exception):
            session_gen.close()


async def ensure_internal_crm(targets: list[TargetOrg], user_id: UUID) -> None:
    totals = {"processed": 0, "created": 0, "skipped": 0, "failed": 0}

    for target in targets:
        totals["processed"] += 1
        print(
            f"\n[ORG] {target.organisation_name} | {target.organisation_id} | "
            f"status={target.status} | schema={target.schema_name}"
        )

        if not target.schema_exists:
            print("  - Skipping: schema does not exist")
            totals["skipped"] += 1
            continue

        try:
            async for session in acquire_org_async_session(target.organisation_id):
                setup_service = InternalCRMSetupService(session, target.organisation_id, user_id)
                status = await setup_service.get_status()
                if status:
                    print(
                        f"  - Internal CRM already configured: "
                        f"integration={status.integration.integration_id}, "
                        f"pipeline={status.pipeline.pipeline_id}, "
                        f"stages={len(status.stages)}"
                    )
                    totals["skipped"] += 1
                else:
                    result = await setup_service.setup_internal_crm()
                    print(
                        f"  - Created internal CRM setup: "
                        f"integration={result.integration.integration_id}, "
                        f"pipeline={result.pipeline.pipeline_id}, "
                        f"stages={len(result.stages)}"
                    )
                    totals["created"] += 1
                break
        except Exception as exc:
            totals["failed"] += 1
            print(f"  - ERROR: {exc}")

    print(
        "\n[SUMMARY] "
        f"processed={totals['processed']} created={totals['created']} "
        f"skipped={totals['skipped']} failed={totals['failed']}"
    )


def main() -> None:
    load_dotenv()
    args = parse_args()

    if not args.all and not args.org_id:
        print("No organisations specified. Use --all or --org-id.")
        return

    targets = load_target_orgs(args.org_id, args.all)
    if not targets:
        print("No organisations found.")
        return

    print(f"Resolved {len(targets)} organisation(s):")
    for target in targets:
        exists = "yes" if target.schema_exists else "no"
        print(
            f" - {target.organisation_id} | {target.organisation_name} | "
            f"status={target.status} | schema={target.schema_name} | schema_exists={exists}"
        )

    user_id = args.user_id or SYSTEM_USER_ID
    asyncio.run(ensure_internal_crm(targets, user_id))


if __name__ == "__main__":
    main()
