"""Trigger full/manual initial sync tasks across organizations.

This script is intended for controlled maintenance windows where we need to
re-run integration syncs without reusing provider delta cursors.

Behavior by integration family:
- Calendar: clears calendar delta/sync token metadata and last_synced_at, then dispatches
  the manual calendar integration task.
- Email: clears per-folder delta/history cursors and last_synced_at, resets folder sync
  timestamps, then dispatches the manual email integration task.
- Messaging channels: clears last_synced_at on the integration, then dispatches the
  manual messaging integration task.
- CRM: dispatches per-integration manual tasks. Deals/contacts/companies are sent with
  force_full_sync=True. Users/pipelines/activities are already full-sync tasks.

Dry run is the default. Use --execute to persist resets and enqueue tasks.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import attributes
from sqlmodel import col

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.domain.models.master_tables import Organisation, OrganisationStatus  # noqa: E402
from src.domain.models.organisation_tables.sync import (  # noqa: E402
    IntegrationSyncState,
    IntegrationType,
    KnowledgeIntegration,
)
from src.domain.models.organisation_tables.sync.email_models import EmailFolder  # noqa: E402
from src.infra.database.sessions import (  # noqa: E402
    acquire_master_async_session,
    acquire_org_async_session,
)
from src.infra.utils.logger import get_logger  # noqa: E402
from src.service.integrations.calendar_cursor_metadata import set_calendar_delta_link  # noqa: E402
from src.service.integrations.custom_token_refresh_service import (  # noqa: E402
    CustomTokenRefreshService,
)
from src.service.integrations.email_delta_sync_service import (  # noqa: E402
    EMAIL_DELTA_LINKS_KEY,
    GMAIL_HISTORY_ID_KEY,
)
from src.service.integrations.scope_validation import (  # noqa: E402
    get_missing_required_scopes,
    get_missing_required_token_fields,
)
from src.workers.integrations_worker.celery_app import app  # noqa: E402

logger = get_logger(__name__)

CALENDAR_TYPES = {IntegrationType.GOOGLE_CALENDAR, IntegrationType.OUTLOOK_CALENDAR}
PROVIDER_PARENT_TYPES = {IntegrationType.GOOGLE, IntegrationType.MICROSOFT}
EMAIL_TYPES = {IntegrationType.GMAIL, IntegrationType.OUTLOOK_EMAIL}
MESSAGING_TYPES = {IntegrationType.TEAMS}
CRM_TYPES = {IntegrationType.HUBSPOT, IntegrationType.SALESFORCE}
SUPPORTED_TYPES = CALENDAR_TYPES | PROVIDER_PARENT_TYPES | EMAIL_TYPES | MESSAGING_TYPES | CRM_TYPES
DEFAULT_FAMILIES = {"calendar", "email", "messaging", "crm"}


@dataclass
class DispatchPlan:
    """One existing worker task to enqueue after its cursor reset commits."""

    task_name: str
    kwargs: dict[str, Any]


@dataclass
class IntegrationPlan:
    """Dry-run description of resets and dispatches for one integration."""

    organisation_id: UUID
    integration_id: UUID
    integration_type: str
    resets: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    dispatches: list[DispatchPlan] = field(default_factory=list)


@dataclass
class RunStats:
    """Aggregate counts printed after a maintenance run."""

    integrations_considered: int = 0
    integrations_prepared: int = 0
    state_resets: int = 0
    tasks_dispatched: int = 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reset cursor state and dispatch initial integration sync tasks.",
    )
    parser.add_argument(
        "--org-id",
        action="append",
        help="Specific organisation UUID(s). Defaults to all approved organisations.",
    )
    parser.add_argument(
        "--family",
        action="append",
        choices=["calendar", "email", "messaging", "crm"],
        help="Limit to one or more integration families.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Persist resets and dispatch the tasks. Default is dry run.",
    )
    parser.add_argument(
        "--integration-id",
        action="append",
        help="Limit work to explicit integration UUID(s). Required when enabling capabilities.",
    )
    parser.add_argument(
        "--enable-calendar-capability",
        action="store_true",
        help="Enable/create calendar state for allowlisted provider-parent integrations.",
    )
    parser.add_argument(
        "--all-orgs",
        action="store_true",
        help="Explicitly target every approved organisation.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm broad --all-orgs execution.",
    )
    return parser.parse_args()


def _validate_args(args: argparse.Namespace) -> None:
    """Reject broad or capability-enabling execution without explicit intent."""
    if args.all_orgs and args.org_id:
        raise ValueError("Use either --all-orgs or --org-id, not both.")
    if args.enable_calendar_capability and not args.integration_id:
        raise ValueError("--enable-calendar-capability requires --integration-id.")
    if (
        args.execute
        and not args.org_id
        and not args.integration_id
        and not (args.all_orgs and args.yes)
    ):
        raise ValueError("Global execution requires --all-orgs --yes.")
    if args.execute and args.all_orgs and not args.yes:
        raise ValueError("--all-orgs execution requires --yes.")


async def _get_target_org_ids(raw_ids: list[str] | None) -> list[UUID]:
    if raw_ids:
        return [UUID(value) for value in raw_ids]

    async for session in acquire_master_async_session():
        stmt = select(col(Organisation.organisation_id)).where(
            col(Organisation.status) == OrganisationStatus.APPROVED,
        )
        result = await session.execute(stmt)
        return [row[0] for row in result.all()]
    return []


def _family_enabled(integration_type: IntegrationType, families: set[str]) -> bool:
    if integration_type in PROVIDER_PARENT_TYPES:
        return "calendar" in families
    if integration_type in CALENDAR_TYPES:
        return "calendar" in families
    if integration_type in EMAIL_TYPES:
        return "email" in families
    if integration_type in MESSAGING_TYPES:
        return "messaging" in families
    if integration_type in CRM_TYPES:
        return "crm" in families
    return False


def _calendar_capability(integration_type: IntegrationType) -> IntegrationType | None:
    if integration_type == IntegrationType.GOOGLE:
        return IntegrationType.GOOGLE_CALENDAR
    if integration_type == IntegrationType.MICROSOFT:
        return IntegrationType.OUTLOOK_CALENDAR
    return integration_type if integration_type in CALENDAR_TYPES else None


def _build_dispatches(
    organisation_id: UUID, integration: KnowledgeIntegration
) -> list[DispatchPlan]:
    integration_type = integration.integration_type
    if isinstance(integration_type, str):
        integration_type = IntegrationType(integration_type)

    base_kwargs = {
        "organisation_id": str(organisation_id),
        "integration_id": str(integration.integration_id),
    }

    if integration_type in CALENDAR_TYPES | PROVIDER_PARENT_TYPES:
        return [DispatchPlan("integrations.sync_calendar_integration", dict(base_kwargs))]
    if integration_type in EMAIL_TYPES:
        return [DispatchPlan("integrations.sync_email_integration", dict(base_kwargs))]
    if integration_type in MESSAGING_TYPES:
        return [DispatchPlan("integrations.sync_messaging_channels_integration", dict(base_kwargs))]
    if integration_type in CRM_TYPES:
        return [
            DispatchPlan("integrations.sync_crm_users_integration", dict(base_kwargs)),
            DispatchPlan("integrations.sync_crm_pipelines_integration", dict(base_kwargs)),
            DispatchPlan(
                "integrations.sync_crm_contacts_integration",
                {**base_kwargs, "force_full_sync": True},
            ),
            DispatchPlan(
                "integrations.sync_crm_companies_integration",
                {**base_kwargs, "force_full_sync": True},
            ),
            DispatchPlan(
                "integrations.sync_crm_deals_integration",
                {**base_kwargs, "force_full_sync": True, "skip_enrichment": False},
            ),
            DispatchPlan("integrations.sync_crm_activities_integration", dict(base_kwargs)),
        ]
    return []


async def _prepare_org(
    organisation_id: UUID,
    *,
    families: set[str],
    execute: bool,
    stats: RunStats,
    integration_ids: set[UUID] | None = None,
    enable_calendar_capability: bool = False,
    refreshed_ids: set[UUID] | None = None,
) -> list[IntegrationPlan]:
    plans: list[IntegrationPlan] = []

    async for session in acquire_org_async_session(organisation_id):
        stmt = select(KnowledgeIntegration).where(
            col(KnowledgeIntegration.integration_type).in_(tuple(t.value for t in SUPPORTED_TYPES)),
            col(KnowledgeIntegration.is_active).is_(True),
            col(KnowledgeIntegration.connection_id).isnot(None),
        )
        if integration_ids:
            stmt = stmt.where(col(KnowledgeIntegration.integration_id).in_(integration_ids))
        result = await session.execute(stmt)
        integrations = list(result.scalars().all())
        stats.integrations_considered += len(integrations)

        for integration in integrations:
            integration_type = integration.integration_type
            if isinstance(integration_type, str):
                integration_type = IntegrationType(integration_type)

            if not _family_enabled(integration_type, families):
                continue

            calendar_state = None
            calendar_capability = _calendar_capability(integration_type)
            if integration_type in PROVIDER_PARENT_TYPES and calendar_capability is not None:
                state_stmt = select(IntegrationSyncState).where(
                    col(IntegrationSyncState.integration_id) == integration.integration_id,
                    col(IntegrationSyncState.capability) == calendar_capability,
                )
                calendar_state = (await session.execute(state_stmt)).scalar_one_or_none()
                needs_enable = calendar_state is None or not calendar_state.enabled
                explicitly_allowed = bool(
                    enable_calendar_capability
                    and integration_ids
                    and integration.integration_id in integration_ids
                )
                if needs_enable and not explicitly_allowed:
                    # Missing/disabled state may be an explicit disconnect, so
                    # broad reset modes never create or enable it implicitly.
                    continue

            plan = IntegrationPlan(
                organisation_id=organisation_id,
                integration_id=integration.integration_id,
                integration_type=integration_type.value,
                dispatches=_build_dispatches(organisation_id, integration),
            )

            if (
                integration_type in PROVIDER_PARENT_TYPES
                and calendar_capability is not None
                and (calendar_state is None or not calendar_state.enabled)
            ):
                missing_scopes = get_missing_required_scopes(
                    calendar_capability,
                    list(integration.scopes or []),
                )
                missing_token_fields = get_missing_required_token_fields(
                    calendar_capability,
                    dict(integration.credentials or {}),
                )
                if missing_scopes:
                    plan.blockers.append(
                        f"missing scopes: {', '.join(sorted(missing_scopes))}"
                    )
                if missing_token_fields:
                    plan.blockers.append(
                        f"missing token fields: {', '.join(sorted(missing_token_fields))}"
                    )
                if execute and integration.integration_id not in (refreshed_ids or set()):
                    plan.blockers.append("live token refresh did not succeed")

                if plan.blockers:
                    plan.dispatches = []
                    plans.append(plan)
                    stats.integrations_prepared += 1
                    continue

                if calendar_state is None:
                    calendar_state = IntegrationSyncState(
                        organisation_id=organisation_id,
                        integration_id=integration.integration_id,
                        capability=calendar_capability,
                        enabled=True,
                    )
                    plan.resets.append("created enabled calendar capability state")
                else:
                    calendar_state.enabled = True
                    plan.resets.append("enabled calendar capability state")
                if execute:
                    session.add(calendar_state)

            if integration_type in CALENDAR_TYPES:
                updated_metadata = set_calendar_delta_link(
                    dict(integration.connection_metadata or {}),
                    integration_type,
                    None,
                )
                if updated_metadata != (integration.connection_metadata or {}):
                    plan.resets.append("cleared calendar cursor")
                    if execute:
                        integration.connection_metadata = updated_metadata
                        attributes.flag_modified(integration, "connection_metadata")
                if integration.last_synced_at is not None:
                    plan.resets.append("set last_synced_at=NULL")
                    if execute:
                        integration.last_synced_at = None
                if integration.last_sync_status is not None:
                    plan.resets.append("cleared last_sync_status")
                    if execute:
                        integration.last_sync_status = None
                if integration.last_sync_error is not None:
                    plan.resets.append("cleared last_sync_error")
                    if execute:
                        integration.last_sync_error = None

            elif integration_type in PROVIDER_PARENT_TYPES and calendar_state is not None:
                updated_metadata = set_calendar_delta_link(
                    dict(calendar_state.cursor_metadata or {}),
                    calendar_capability,
                    None,
                )
                if updated_metadata != (calendar_state.cursor_metadata or {}):
                    plan.resets.append("cleared calendar capability cursor")
                    if execute:
                        calendar_state.cursor_metadata = updated_metadata
                        attributes.flag_modified(calendar_state, "cursor_metadata")
                if calendar_state.last_synced_at is not None:
                    plan.resets.append("set calendar capability last_synced_at=NULL")
                    if execute:
                        calendar_state.last_synced_at = None
                if calendar_state.last_sync_status is not None:
                    plan.resets.append("cleared calendar capability last_sync_status")
                    if execute:
                        calendar_state.last_sync_status = None
                if calendar_state.last_sync_error is not None:
                    plan.resets.append("cleared calendar capability last_sync_error")
                    if execute:
                        calendar_state.last_sync_error = None
                if execute:
                    session.add(calendar_state)

            elif integration_type in EMAIL_TYPES:
                updated_metadata = dict(integration.connection_metadata or {})
                removed = False
                if EMAIL_DELTA_LINKS_KEY in updated_metadata:
                    updated_metadata.pop(EMAIL_DELTA_LINKS_KEY, None)
                    removed = True
                if GMAIL_HISTORY_ID_KEY in updated_metadata:
                    updated_metadata.pop(GMAIL_HISTORY_ID_KEY, None)
                    removed = True
                if removed:
                    plan.resets.append("cleared email cursors")
                    if execute:
                        integration.connection_metadata = updated_metadata
                        attributes.flag_modified(integration, "connection_metadata")
                if integration.last_synced_at is not None:
                    plan.resets.append("set last_synced_at=NULL")
                    if execute:
                        integration.last_synced_at = None
                if integration.last_sync_status is not None:
                    plan.resets.append("cleared last_sync_status")
                    if execute:
                        integration.last_sync_status = None
                if integration.last_sync_error is not None:
                    plan.resets.append("cleared last_sync_error")
                    if execute:
                        integration.last_sync_error = None
                folder_stmt = select(EmailFolder).where(
                    col(EmailFolder.integration_id) == integration.integration_id
                )
                folder_result = await session.execute(folder_stmt)
                folders = list(folder_result.scalars().all())
                folder_reset_needed = False
                for folder in folders:
                    if folder.last_synced_at is not None or folder.last_sync_cursor is not None:
                        folder_reset_needed = True
                        if execute:
                            folder.last_synced_at = None
                            folder.last_sync_cursor = None
                            session.add(folder)
                if folder_reset_needed:
                    plan.resets.append(f"reset {len(folders)} email folder sync markers")

            elif integration_type in MESSAGING_TYPES:
                if integration.last_synced_at is not None:
                    plan.resets.append("set last_synced_at=NULL")
                    if execute:
                        integration.last_synced_at = None
                if integration.last_sync_status is not None:
                    plan.resets.append("cleared last_sync_status")
                    if execute:
                        integration.last_sync_status = None
                if integration.last_sync_error is not None:
                    plan.resets.append("cleared last_sync_error")
                    if execute:
                        integration.last_sync_error = None

            if execute:
                session.add(integration)

            plans.append(plan)
            stats.integrations_prepared += 1
            stats.state_resets += len(plan.resets)

        if execute:
            await session.commit()

    return plans


def _dispatch(plans: list[IntegrationPlan], execute: bool, stats: RunStats) -> None:
    for plan in plans:
        print(f"  Integration {plan.integration_id} [{plan.integration_type}]")
        if plan.resets:
            for reset in plan.resets:
                print(f"    reset: {reset}")
        else:
            print("    reset: none")

        for blocker in plan.blockers:
            print(f"    blocked: {blocker}")

        for dispatch in plan.dispatches:
            if execute:
                task = app.send_task(
                    dispatch.task_name,
                    kwargs=dispatch.kwargs,
                    queue="integrations_priority",
                )
                print(f"    dispatched: {dispatch.task_name} task_id={task.id}")
            else:
                print(f"    would dispatch: {dispatch.task_name} kwargs={dispatch.kwargs}")
            stats.tasks_dispatched += 1


async def main() -> None:
    args = _parse_args()
    try:
        _validate_args(args)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    families = set(args.family or DEFAULT_FAMILIES)
    org_ids = await _get_target_org_ids(args.org_id)
    integration_ids = (
        {UUID(value) for value in args.integration_id} if args.integration_id else None
    )

    mode = "EXECUTE" if args.execute else "DRY RUN"
    print("=" * 90)
    print(f"  TRIGGER INITIAL INTEGRATION SYNCS - {mode}")
    print(f"  Families: {', '.join(sorted(families))}")
    print("=" * 90)

    if not org_ids:
        print("No organisations matched the request.")
        return

    stats = RunStats()
    all_plans: list[IntegrationPlan] = []

    for org_id in org_ids:
        print(f"\n--- Org: {org_id} ---")
        refreshed_ids: set[UUID] = set()
        if args.execute and args.enable_calendar_capability and integration_ids:
            # Token refresh is the live proof that a stored user grant still
            # works. It runs before the reset session so no database connection
            # is held across the provider request.
            refresh_service = CustomTokenRefreshService(org_id)
            for integration_id in integration_ids:
                refresh_result = await refresh_service.refresh_single_token_async(integration_id)
                if refresh_result.get("success"):
                    refreshed_ids.add(integration_id)
                elif refresh_result.get("error") != "Integration not found":
                    print(
                        f"  Integration {integration_id} token validation failed: "
                        f"{refresh_result.get('error') or refresh_result.get('reason')}"
                    )

        org_plans = await _prepare_org(
            org_id,
            families=families,
            execute=args.execute,
            stats=stats,
            integration_ids=integration_ids,
            enable_calendar_capability=args.enable_calendar_capability,
            refreshed_ids=refreshed_ids,
        )
        if not org_plans:
            print("  No matching active integrations")
            continue
        _dispatch(org_plans, args.execute, stats)
        all_plans.extend(org_plans)

    print("\n" + "=" * 90)
    print(
        "TOTAL: "
        f"{len(org_ids)} org(s), "
        f"{stats.integrations_considered} integration row(s) considered, "
        f"{stats.integrations_prepared} integration row(s) prepared, "
        f"{stats.state_resets} reset action(s), "
        f"{stats.tasks_dispatched} task dispatch(es)"
    )
    if not args.execute:
        print("DRY RUN - no state changes committed and no tasks dispatched.")
    print("=" * 90)


if __name__ == "__main__":
    asyncio.run(main())
