"""Tier 3 chain terminal: mark a Salesforce initial sync as fully successful."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.sync_history_manager import datetime_now_naive
from src.service.integrations.webhooks.salesforce.metadata import (
    SALESFORCE_CDC_OBJECTS,
)
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.client import send_integrations_task_async
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)

_TASK_NAME = "integrations.mark_salesforce_initial_sync_succeeded"
_L = TASK_LIMITS[_TASK_NAME]


@app.task(
    name=_TASK_NAME,
    bind=True,
    max_retries=_L["retries"],
    default_retry_delay=_L.get("retry_delay", 30),
    acks_late=True,
    soft_time_limit=_L["soft"],
    time_limit=_L["hard"],
)
def mark_salesforce_initial_sync_succeeded_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
) -> dict[str, Any]:
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)

    async def _run() -> dict[str, Any]:
        cdc_channels: list[str] = []
        async for session in acquire_org_async_session(org_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.organisation_id) == org_id,
                col(KnowledgeIntegration.integration_id) == int_id,
            )
            integration = (await session.execute(stmt)).scalar_one_or_none()
            if integration is None:
                return {
                    "status": "noop",
                    "reason": "integration_not_found",
                    "integration_id": integration_id,
                }

            metadata = dict(integration.connection_metadata or {})
            metadata["initial_sync_status"] = "success"
            metadata["initial_sync_completed_at"] = datetime.now(UTC).isoformat()
            metadata.pop("initial_sync_failed_phase", None)
            metadata.pop("initial_sync_placeholder", None)
            integration.connection_metadata = metadata
            integration.last_synced_at = datetime_now_naive()
            if _has_salesforce_cdc_identity(
                metadata=metadata,
                credentials=dict(getattr(integration, "credentials", None) or {}),
            ):
                cdc_channels = _enabled_salesforce_cdc_channels(metadata)
            session.add(integration)
            await session.commit()

        task_ids: list[str] = []
        for cdc_channel in cdc_channels:
            task_id = await send_integrations_task_async(
                task_name="integrations.run_salesforce_cdc_subscriber",
                kwargs={
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "channel": cdc_channel,
                },
                queue="integrations_streaming",
                routing_key="integrations_streaming",
            )
            task_ids.append(task_id)

        return {
            "status": "success",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "salesforce_cdc_task_id": task_ids[0] if task_ids else None,
            "salesforce_cdc_task_ids": task_ids,
        }

    return run_async(_run())


def _enabled_salesforce_cdc_channels(metadata: dict[str, Any]) -> list[str]:
    cdc_metadata = dict(metadata.get("salesforce_cdc") or {})
    channels: list[str] = []
    for object_key, config in SALESFORCE_CDC_OBJECTS.items():
        object_metadata = dict(cdc_metadata.get(object_key) or {})
        if object_metadata.get("enabled") is True:
            channels.append(str(object_metadata.get("channel") or config["channel"]))
    return channels


def _has_salesforce_cdc_identity(
    *,
    metadata: dict[str, Any],
    credentials: dict[str, Any],
) -> bool:
    cdc_metadata = dict(metadata.get("salesforce_cdc") or {})
    return (
        any(dict(entry or {}).get("enabled") is True for entry in cdc_metadata.values())
        and bool(str(metadata.get("salesforce_organization_id") or "").strip())
        and bool(
            str(
                credentials.get("instance_url") or metadata.get("salesforce_instance_url") or ""
            ).strip()
        )
    )
