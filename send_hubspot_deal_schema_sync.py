"""Dispatch the HubSpot post-connect initial-sync chain for one integration.

Despite the filename (kept for script-history continuity), this script now
dispatches the full chain — not just the deal schema sync. The chain is:

    schema_sync  →  pipelines_sync  →  … (future phases) …  →  finalize

On any link failure, the chain aborts via `link_error` and the integration's
`connection_metadata.initial_sync_status` is set to `"failed"` by the failure
terminator. On full success, the terminal finalize link sets it to `"success"`.

Per-task queue routing is owned by `IntegrationsWorkerConfig.task_routes`;
this script does not override queue or routing_key.

Usage:
    uv run python send_hubspot_deal_schema_sync.py <organisation_id> <integration_id>

    # Single-phase dispatch modes — bypass the chain (will NOT flip
    # initial_sync_status). Useful for isolated re-runs during development.
    uv run python send_hubspot_deal_schema_sync.py --schema-only     <org> <int>
    uv run python send_hubspot_deal_schema_sync.py --pipelines-only  <org> <int>
    uv run python send_hubspot_deal_schema_sync.py --users-only      <org> <int>
    uv run python send_hubspot_deal_schema_sync.py --companies-only  <org> <int>
    uv run python send_hubspot_deal_schema_sync.py --contacts-only   <org> <int>
    uv run python send_hubspot_deal_schema_sync.py --deals-only      <org> <int>
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from uuid import UUID

_SCHEMA_TASK_NAME = "integrations.sync_hubspot_deal_schema"
_PIPELINES_TASK_NAME = "integrations.sync_hubspot_pipelines"
_USERS_TASK_NAME = "integrations.sync_hubspot_users"
_COMPANIES_TASK_NAME = "integrations.sync_hubspot_companies"
_CONTACTS_TASK_NAME = "integrations.sync_hubspot_contacts"
_DEALS_TASK_NAME = "integrations.sync_hubspot_deals"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Dispatch the HubSpot post-connect initial-sync chain "
            "(schema → pipelines → … → finalize) for one integration."
        ),
    )
    parser.add_argument("organisation_id", type=UUID, help="Organisation UUID")
    parser.add_argument("integration_id", type=UUID, help="Integration UUID")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--schema-only",
        action="store_true",
        help=(
            "Dispatch only integrations.sync_hubspot_deal_schema (no chain, "
            "no initial_sync_status flip)."
        ),
    )
    mode.add_argument(
        "--pipelines-only",
        action="store_true",
        help=(
            "Dispatch only integrations.sync_hubspot_pipelines (no chain, "
            "no initial_sync_status flip)."
        ),
    )
    mode.add_argument(
        "--users-only",
        action="store_true",
        help=(
            "Dispatch only integrations.sync_hubspot_users (no chain, "
            "no initial_sync_status flip)."
        ),
    )
    mode.add_argument(
        "--companies-only",
        action="store_true",
        help=(
            "Dispatch only integrations.sync_hubspot_companies (no chain, "
            "no initial_sync_status flip)."
        ),
    )
    mode.add_argument(
        "--contacts-only",
        action="store_true",
        help=(
            "Dispatch only integrations.sync_hubspot_contacts (no chain, "
            "no initial_sync_status flip)."
        ),
    )
    mode.add_argument(
        "--deals-only",
        action="store_true",
        help=(
            "Dispatch only integrations.sync_hubspot_deals (no chain, "
            "no initial_sync_status flip)."
        ),
    )
    return parser


def _dispatch_chain(organisation_id: UUID, integration_id: UUID) -> str:
    from src.service.integrations.crm.records.common.hubspot_chain import (
        build_hubspot_initial_sync_chain,
    )

    chain_sig = build_hubspot_initial_sync_chain(
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    result = chain_sig.apply_async()
    # apply_async on a chain returns an AsyncResult for the last link in sync
    # mode and the same for canvas mode; typed as Optional to account for
    # the rare eager-result path.
    return str(getattr(result, "id", None) or "<unknown>")


def _dispatch_single(
    task_name: str,
    organisation_id: UUID,
    integration_id: UUID,
) -> str:
    """Fire one Tier 3 task directly, bypassing the chain.

    Useful for isolated re-runs. Per-task routing comes from `task_routes`
    on the integrations Celery app — we don't override queue here.
    """
    from src.workers.integrations_worker.celery_app import app

    result = app.send_task(
        task_name,
        kwargs={
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
        },
    )
    return str(result.id)


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)

    if args.schema_only:
        task_id = _dispatch_single(
            _SCHEMA_TASK_NAME, args.organisation_id, args.integration_id
        )
        print(f"dispatched schema-only: task_id={task_id}")
        return 0

    if args.pipelines_only:
        task_id = _dispatch_single(
            _PIPELINES_TASK_NAME, args.organisation_id, args.integration_id
        )
        print(f"dispatched pipelines-only: task_id={task_id}")
        return 0

    if args.users_only:
        task_id = _dispatch_single(
            _USERS_TASK_NAME, args.organisation_id, args.integration_id
        )
        print(f"dispatched users-only: task_id={task_id}")
        return 0

    if args.companies_only:
        task_id = _dispatch_single(
            _COMPANIES_TASK_NAME, args.organisation_id, args.integration_id
        )
        print(f"dispatched companies-only: task_id={task_id}")
        return 0

    if args.contacts_only:
        task_id = _dispatch_single(
            _CONTACTS_TASK_NAME, args.organisation_id, args.integration_id
        )
        print(f"dispatched contacts-only: task_id={task_id}")
        return 0

    if args.deals_only:
        task_id = _dispatch_single(
            _DEALS_TASK_NAME, args.organisation_id, args.integration_id
        )
        print(f"dispatched deals-only: task_id={task_id}")
        return 0

    chain_id = _dispatch_chain(args.organisation_id, args.integration_id)
    print(f"dispatched initial-sync chain: id={chain_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
