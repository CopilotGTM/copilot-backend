from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Protocol
from uuid import UUID

from src.infra.database.sessions import acquire_org_async_session
from src.service.integrations.sync_health import record_sync_failure


class MarkHistoryFailedFn(Protocol):
    def __call__(
        self,
        org_id: UUID,
        sync_id: UUID | None,
        *,
        error_class: str,
        error_message: str,
    ) -> Awaitable[None]: ...


class UpdateIntegrationMetadataFn(Protocol):
    def __call__(
        self,
        *,
        session: Any,
        organisation_id: UUID,
        integration_id: UUID,
        status: str,
        result_metadata: dict[str, Any],
        error_class: str | None,
        error_message: str | None,
    ) -> Awaitable[None]: ...


async def record_crm_task_failure(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    sync_id: UUID | None,
    error_class: str,
    error_message: str,
    error: Exception,
    mark_history_failed: MarkHistoryFailedFn,
    update_integration_metadata: UpdateIntegrationMetadataFn,
) -> None:
    """Persist failure state shared by CRM Tier 3 schema/record sync tasks."""

    await mark_history_failed(
        organisation_id,
        sync_id,
        error_class=error_class,
        error_message=error_message,
    )
    async for session in acquire_org_async_session(organisation_id):
        await update_integration_metadata(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            status="failed",
            result_metadata={},
            error_class=error_class,
            error_message=error_message,
        )
    await record_sync_failure(
        organisation_id=organisation_id,
        integration_id=integration_id,
        error=error,
    )
