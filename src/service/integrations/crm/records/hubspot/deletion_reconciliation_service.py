"""Low-frequency HubSpot delete reconciliation.

Webhooks handle deletes in real time. This service is a slow safety net for the
rare case where a delete webhook is missed: it verifies active local ids against
HubSpot batch-read and soft-deletes only ids HubSpot no longer returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.domain.models.organisation_tables.sync.crm import CRMSourceObjectType
from src.domain.models.organisation_tables.sync.crm.activities import CRMActivity, CRMActivityType
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.source_mapping import CRMSourceMappingHelper
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.service.integrations.sync_history_manager import datetime_now_naive
from src.workers.integrations_worker.providers.crm.hubspot.reconciliation.batch_object_existence_fetcher import (
    HubSpotBatchObjectExistenceFetcher,
)

logger = get_logger(__name__)

_SUPPORTED_OBJECTS = frozenset({"deals", "companies", "contacts", "tasks", "notes"})
_MODEL_BY_OBJECT: dict[str, Any] = {
    "deals": CRMDeal,
    "companies": CRMCompany,
    "contacts": CRMContact,
    "tasks": CRMActivity,
    "notes": CRMActivity,
}
_SOURCE_OBJECT_BY_OBJECT: dict[str, CRMSourceObjectType] = {
    "deals": CRMSourceObjectType.DEAL,
    "companies": CRMSourceObjectType.COMPANY,
    "contacts": CRMSourceObjectType.CONTACT,
    "tasks": CRMSourceObjectType.TASK,
    "notes": CRMSourceObjectType.NOTE,
}
_ID_COLUMN_BY_OBJECT: dict[str, Any] = {
    "deals": CRMDeal.deal_id,
    "companies": CRMCompany.company_id,
    "contacts": CRMContact.contact_id,
    "tasks": CRMActivity.activity_id,
    "notes": CRMActivity.activity_id,
}
_DEFAULT_OBJECT_KEYS = ("deals", "companies", "contacts", "tasks", "notes")


@dataclass(slots=True, frozen=True)
class HubSpotDeletionReconciliationObjectResult:
    """Summary for one object type during delete reconciliation."""

    object_key: str
    checked_count: int
    missing_count: int
    soft_deleted_count: int


@dataclass(slots=True, frozen=True)
class HubSpotDeletionReconciliationResult:
    """Aggregate result from a full HubSpot delete-reconciliation run."""

    object_results: tuple[HubSpotDeletionReconciliationObjectResult, ...]
    updated_metadata: dict[str, Any]

    @property
    def checked_count(self) -> int:
        """Total checked local ids across all object families."""
        return sum(result.checked_count for result in self.object_results)

    @property
    def missing_count(self) -> int:
        """Total ids missing in HubSpot across all object families."""
        return sum(result.missing_count for result in self.object_results)

    @property
    def soft_deleted_count(self) -> int:
        """Total soft deletes applied across all object families."""
        return sum(result.soft_deleted_count for result in self.object_results)


class HubSpotDeletionReconciliationService:
    """Reconcile missed HubSpot delete webhooks for one integration."""

    def __init__(self, *, batch_size: int = 100) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self.batch_size = batch_size

    async def run(
        self,
        *,
        organisation_id,
        integration_id,
        object_keys: tuple[str, ...] = _DEFAULT_OBJECT_KEYS,
        checked_at: datetime | None = None,
    ) -> HubSpotDeletionReconciliationResult:
        """Refresh local delete state for configured CRM object families."""
        token_info, metadata = await self._load_token_context(
            organisation_id=organisation_id,
            integration_id=integration_id,
        )
        now = checked_at or datetime.now(UTC)
        updated_metadata = dict(metadata)
        object_results: list[HubSpotDeletionReconciliationObjectResult] = []

        for object_key in object_keys:
            result = await self.reconcile_object(
                object_key=object_key,
                token_info=token_info,
                checked_at=now,
            )
            updated_metadata = _set_delete_reconciliation_metadata(
                updated_metadata,
                result,
                checked_at=now,
            )
            object_results.append(result)

        await self._save_metadata(
            organisation_id=organisation_id,
            integration_id=integration_id,
            metadata=updated_metadata,
        )
        return HubSpotDeletionReconciliationResult(
            object_results=tuple(object_results),
            updated_metadata=updated_metadata,
        )

    async def reconcile_object(
        self,
        *,
        object_key: str,
        token_info: IntegrationTokenInfo,
        checked_at: datetime,  # noqa: ARG002 - reserved for future per-object audit stamps
    ) -> HubSpotDeletionReconciliationObjectResult:
        """Reconcile one object family and return missing vs. present counts."""
        if object_key not in _SUPPORTED_OBJECTS:
            raise ValueError(f"Unsupported HubSpot reconciliation object: {object_key}")

        local_ids = await self._load_active_external_ids(
            organisation_id=token_info.organisation_id,
            integration_id=token_info.integration_id,
            object_key=object_key,
        )
        missing_ids: list[str] = []

        for batch in _chunks(local_ids, self.batch_size):
            existing_ids = await HubSpotBatchObjectExistenceFetcher(
                token_info=token_info,
                object_type=object_key,
                object_ids=batch,
            ).fetch_existing_ids()
            missing_ids.extend(object_id for object_id in batch if object_id not in existing_ids)

        soft_deleted_count = 0
        if missing_ids:
            soft_deleted_count = await self._soft_delete_external_ids(
                organisation_id=token_info.organisation_id,
                integration_id=token_info.integration_id,
                object_key=object_key,
                external_ids=missing_ids,
            )

        logger.info(
            "HubSpot delete reconciliation object complete",
            extra={
                "organisation_id": str(token_info.organisation_id),
                "integration_id": str(token_info.integration_id),
                "object_key": object_key,
                "checked_count": len(local_ids),
                "missing_count": len(missing_ids),
                "soft_deleted_count": soft_deleted_count,
            },
        )
        return HubSpotDeletionReconciliationObjectResult(
            object_key=object_key,
            checked_count=len(local_ids),
            missing_count=len(missing_ids),
            soft_deleted_count=soft_deleted_count,
        )

    async def _load_active_external_ids(
        self,
        *,
        organisation_id,
        integration_id,
        object_key: str,
    ) -> list[str]:
        async for session in acquire_org_async_session(organisation_id):
            helper = CRMSourceMappingHelper(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
                provider="hubspot",
            )
            source_ids = await helper.load_local_id_map(
                object_type=_SOURCE_OBJECT_BY_OBJECT[object_key],
            )
            if not source_ids:
                return []

            model = _MODEL_BY_OBJECT[object_key]
            id_column = _ID_COLUMN_BY_OBJECT[object_key]
            stmt = select(id_column).where(
                col(model.organisation_id) == organisation_id,
                col(model.integration_id) == integration_id,
                col(model.is_deleted).is_(False),
                col(id_column).in_(set(source_ids.values())),
            )
            if object_key == "companies":
                stmt = stmt.where(col(CRMCompany.is_active).is_(True))
            elif object_key == "tasks":
                stmt = stmt.where(
                    col(CRMActivity.activity_type).in_(
                        [CRMActivityType.TASK.value, CRMActivityType.TODO.value]
                    )
                )
            elif object_key == "notes":
                stmt = stmt.where(col(CRMActivity.activity_type) == CRMActivityType.NOTE.value)
            result = await session.execute(stmt)
            live_local_ids = {row[0] for row in result.all() if row[0] is not None}
            return [
                external_id
                for external_id, local_id in source_ids.items()
                if local_id in live_local_ids
            ]
        return []

    async def _soft_delete_external_ids(
        self,
        *,
        organisation_id,
        integration_id,
        object_key: str,
        external_ids: list[str],
    ) -> int:
        if not external_ids:
            return 0
        model = _MODEL_BY_OBJECT[object_key]
        id_column = _ID_COLUMN_BY_OBJECT[object_key]
        now = datetime.now(UTC)
        async for session in acquire_org_async_session(organisation_id):
            helper = CRMSourceMappingHelper(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
                provider="hubspot",
            )
            external_to_local = await helper.mark_external_ids_deleted(
                object_type=_SOURCE_OBJECT_BY_OBJECT[object_key],
                external_ids=external_ids,
            )
            if not external_to_local:
                await session.commit()
                return 0

            stmt = select(model).where(
                col(model.organisation_id) == organisation_id,
                col(model.integration_id) == integration_id,
                col(model.is_deleted).is_(False),
                col(id_column).in_(set(external_to_local.values())),
            )
            if object_key == "tasks":
                stmt = stmt.where(
                    col(CRMActivity.activity_type).in_(
                        [CRMActivityType.TASK.value, CRMActivityType.TODO.value]
                    )
                )
            elif object_key == "notes":
                stmt = stmt.where(col(CRMActivity.activity_type) == CRMActivityType.NOTE.value)
            rows = (await session.execute(stmt)).scalars().all()
            soft_deleted_count = 0
            for row in rows:
                row.is_deleted = True
                row.synced_at = now
                if isinstance(row, CRMCompany):
                    row.is_active = False
                session.add(row)
                soft_deleted_count += 1
            await session.commit()
            return soft_deleted_count
        return 0

    async def _load_token_context(self, *, organisation_id, integration_id):
        async for session in acquire_org_async_session(organisation_id):
            statement = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.organisation_id) == organisation_id,
                col(KnowledgeIntegration.integration_id) == integration_id,
            )
            integration = (await session.execute(statement)).scalar_one_or_none()
            if integration is None:
                raise ValueError(
                    f"Integration {integration_id} not found for organisation {organisation_id}"
                )
            integration_type = integration.integration_type
            if isinstance(integration_type, str):
                integration_type = IntegrationType(integration_type)
            if integration_type != IntegrationType.HUBSPOT:
                raise ValueError(
                    f"Integration {integration_id} is {integration_type.value}, expected hubspot"
                )
            if not integration.is_active:
                raise ValueError(f"Integration {integration_id} is not active")
            return (
                IntegrationTokenInfo.from_integration(integration, organisation_id),
                dict(integration.connection_metadata or {}),
            )
        raise ValueError("Could not acquire org session for HubSpot delete reconciliation")

    async def _save_metadata(self, *, organisation_id, integration_id, metadata: dict[str, Any]):
        async for session in acquire_org_async_session(organisation_id):
            statement = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.organisation_id) == organisation_id,
                col(KnowledgeIntegration.integration_id) == integration_id,
            )
            integration = (await session.execute(statement)).scalar_one_or_none()
            if integration is None:
                return
            integration.connection_metadata = metadata
            integration.last_synced_at = datetime_now_naive()
            session.add(integration)
            await session.commit()
            return


def _set_delete_reconciliation_metadata(
    metadata: dict[str, Any],
    result: HubSpotDeletionReconciliationObjectResult,
    *,
    checked_at: datetime,
) -> dict[str, Any]:
    updated = dict(metadata)
    record_sync = dict(updated.get("record_sync") or {})
    object_entry = dict(record_sync.get(result.object_key) or {})
    object_entry["delete_reconciliation"] = {
        "last_checked_at": checked_at.isoformat(),
        "last_checked_count": result.checked_count,
        "last_missing_count": result.missing_count,
        "last_soft_deleted_count": result.soft_deleted_count,
    }
    record_sync[result.object_key] = object_entry
    updated["record_sync"] = record_sync
    return updated


def _chunks(values: list[str], size: int):
    for index in range(0, len(values), size):
        yield values[index : index + size]


__all__ = [
    "HubSpotDeletionReconciliationObjectResult",
    "HubSpotDeletionReconciliationResult",
    "HubSpotDeletionReconciliationService",
]
