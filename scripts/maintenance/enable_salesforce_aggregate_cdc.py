"""Safely opt one fully synced Salesforce integration into aggregate CDC discovery."""

from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.infra.database.sessions import acquire_org_async_session
from src.service.integrations.webhooks.salesforce.metadata import AGGREGATE_CDC_CHANNEL


def build_aggregate_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Create aggregate state without reusing incompatible per-object replay cursors."""
    updated = deepcopy(metadata)
    cdc = dict(updated.get("salesforce_cdc") or {})
    cdc["mode"] = "aggregate"
    cdc["aggregate"] = {
        "enabled": True,
        "channel": AGGREGATE_CDC_CHANNEL,
        "status": "pending",
        "last_replay_id": None,
        "replay_preset": "EARLIEST",
    }
    updated["salesforce_cdc"] = cdc
    return updated


async def enable_aggregate_cdc(
    organisation_id: UUID,
    integration_id: UUID,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Validate, display, and optionally persist one aggregate-mode metadata change."""
    async for session in acquire_org_async_session(organisation_id):
        stmt = select(KnowledgeIntegration).where(
            col(KnowledgeIntegration.organisation_id) == organisation_id,
            col(KnowledgeIntegration.integration_id) == integration_id,
        )
        integration = (await session.execute(stmt)).scalar_one_or_none()
        if integration is None:
            raise ValueError("Salesforce integration not found")
        integration_type = integration.integration_type
        if isinstance(integration_type, str):
            integration_type = IntegrationType(integration_type)
        if integration_type != IntegrationType.SALESFORCE or not integration.is_active:
            raise ValueError("Integration must be active Salesforce")
        before = dict(integration.connection_metadata or {})
        if before.get("initial_sync_status") != "success":
            raise ValueError("Salesforce initial sync must be successful")
        credentials = dict(integration.credentials or {})
        if (
            not str(before.get("salesforce_organization_id") or "").strip()
            or not str(
                credentials.get("instance_url") or before.get("salesforce_instance_url") or ""
            ).strip()
        ):
            raise ValueError("Salesforce CDC identity is incomplete")
        if dict(before.get("salesforce_cdc") or {}).get("mode") == "aggregate":
            raise ValueError("Salesforce integration already uses aggregate CDC")
        after = build_aggregate_metadata(before)
        result = {"apply": apply, "before": before, "after": after}
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        if apply:
            integration.connection_metadata = after
            session.add(integration)
            await session.commit()
        return result
    raise RuntimeError("Organisation session factory did not yield")


def main() -> None:
    """Parse the explicit integration target; remain dry-run unless --apply is present."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--organisation-id", type=UUID, required=True)
    parser.add_argument("--integration-id", type=UUID, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(
        enable_aggregate_cdc(
            args.organisation_id,
            args.integration_id,
            apply=args.apply,
        )
    )


if __name__ == "__main__":
    main()
