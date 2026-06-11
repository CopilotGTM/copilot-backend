from __future__ import annotations

import argparse
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session
from src.domain.models.master_tables import Organisation
from src.infra.config.configs import database_config
from src.infra.database.sessions import master_session_scope
from src.infra.database.tenant_runtime_config import validate_tenant_runtime_config
from src.infra.database.tenant_runtime_grants import TenantRuntimeGrantService
from src.infra.utils.resource_naming import get_role_name, get_schema_name


def resolve_target_org_ids(
    session: Session,
    *,
    all_orgs: bool,
    org_id: UUID | None,
) -> list[UUID]:
    if org_id is not None:
        return [org_id]
    if not all_orgs:
        raise RuntimeError("Pass --all-orgs or --org-id")
    result = session.execute(select(Organisation.organisation_id))
    return list(result.scalars().all())


def grant_schema_access(*, all_orgs: bool, org_id: UUID | None, execute: bool) -> int:
    config = validate_tenant_runtime_config(
        database_user=database_config.DATABASE_USER,
        tenant_database_user=database_config.TENANT_DATABASE_USER,
        tenant_password=database_config.TENANT_DATABASE_PASSWORD,
        tenant_password_secret_name=database_config.TENANT_DATABASE_PASSWORD_SECRET_NAME,
    )

    with master_session_scope() as session:
        org_ids = resolve_target_org_ids(session, all_orgs=all_orgs, org_id=org_id)
        if not execute:
            for target_org_id in org_ids:
                print(f"dry-run grant tenant runtime access for {target_org_id}")
            return len(org_ids)

        service = TenantRuntimeGrantService(session)
        for target_org_id in org_ids:
            service.grant_tenant_runtime_role_access(
                schema_name=get_schema_name(target_org_id),
                schema_owner_role=get_role_name(target_org_id),
                migration_owner_role=database_config.DATABASE_USER,
                runtime_role=config.user,
            )
        session.commit()
        return len(org_ids)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all-orgs", action="store_true")
    parser.add_argument("--org-id", type=UUID)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    count = grant_schema_access(all_orgs=args.all_orgs, org_id=args.org_id, execute=args.execute)
    print(f"processed {count} organisation schemas")


if __name__ == "__main__":
    main()
