"""HubSpot post-connect initial-sync chain builder.

Single source of truth for the order in which HubSpot phases run after OAuth
succeeds. Each phase is one Tier 3 task; the chain runs them serially. On any
failure, the chain aborts via `link_error` and the failure terminator records
`initial_sync_status="failed"`. On full success, the terminal finalize link
records `initial_sync_status="success"`.

Adding a new phase: append one `task_signature(...)` line ABOVE the finalize
task line. Do NOT change InitialSyncService and do NOT change earlier phases.

See `docs/superpowers/specs/2026-04-22-hubspot-crm-pipelines-record-sync-design.md`
sections 9.2 and 11.3 for the full contract.
"""

from __future__ import annotations

from uuid import UUID

from celery.canvas import Signature, chain
from src.workers.integrations_worker.client import task_signature


def build_hubspot_initial_sync_chain(
    organisation_id: UUID,
    integration_id: UUID,
) -> Signature:
    """Return a Celery chain signature for the full HubSpot initial sync.

    Caller dispatches with `.apply_async()`. The chain owns its own routing —
    do not pass `queue=` or `routing_key=` to apply_async; per-task routing is
    configured in `IntegrationsWorkerConfig.task_routes`.
    """
    kwargs = {
        "organisation_id": str(organisation_id),
        "integration_id": str(integration_id),
    }

    failure_terminator = task_signature(
        "integrations.mark_hubspot_initial_sync_failed",
        kwargs=kwargs,
    )

    return chain(
        task_signature("integrations.sync_hubspot_deal_schema", kwargs=kwargs),
        task_signature("integrations.sync_hubspot_pipelines", kwargs=kwargs),
        task_signature("integrations.sync_hubspot_users", kwargs=kwargs),
        task_signature("integrations.sync_hubspot_companies", kwargs=kwargs),
        task_signature("integrations.sync_hubspot_contacts", kwargs=kwargs),
        # phase 6: integrations.sync_hubspot_deals
        # phase 7: integrations.sync_hubspot_stage_history
        # phase 8: integrations.sync_hubspot_associations
        # Terminal finalize link — sole writer of initial_sync_status="success".
        # Must remain the LAST positional task in the chain. New phases append
        # ABOVE this line.
        task_signature(
            "integrations.mark_hubspot_initial_sync_succeeded",
            kwargs=kwargs,
        ),
    ).set(link_error=failure_terminator)
