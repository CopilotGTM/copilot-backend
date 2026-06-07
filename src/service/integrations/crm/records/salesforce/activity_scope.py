"""Shared Salesforce activity sync scoping helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm import CRMSourceObjectType
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealStatus
from src.infra.database.sessions import acquire_org_async_session
from src.service.integrations.crm.records.common.query_chunks import (
    LOCAL_ID_QUERY_CHUNK_SIZE,
    iter_chunks,
)
from src.service.integrations.crm.records.common.source_mapping import CRMSourceMappingHelper

INITIAL_SYNC_CLOSED_DEAL_LOOKBACK_DAYS = 180


async def load_initial_sync_deal_external_ids(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    provider: str,
) -> list[str]:
    async for session in acquire_org_async_session(organisation_id):
        helper = CRMSourceMappingHelper(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            provider=provider,
        )
        external_to_local = await helper.load_local_id_map(
            object_type=CRMSourceObjectType.DEAL,
            external_ids=None,
        )
        if not external_to_local:
            return []

        local_to_external = {
            local_id: external_id for external_id, local_id in external_to_local.items()
        }
        cutoff = datetime.now(UTC) - timedelta(days=INITIAL_SYNC_CLOSED_DEAL_LOOKBACK_DAYS)
        rows = []
        for local_ids in iter_chunks(set(local_to_external), LOCAL_ID_QUERY_CHUNK_SIZE):
            stmt = select(col(CRMDeal.deal_id)).where(
                col(CRMDeal.organisation_id) == organisation_id,
                col(CRMDeal.integration_id) == integration_id,
                col(CRMDeal.is_deleted) == False,  # noqa: E712
                col(CRMDeal.deal_id).in_(local_ids),
                or_(
                    col(CRMDeal.deal_status) == CRMDealStatus.OPEN.value,
                    col(CRMDeal.close_date) >= cutoff,
                    and_(
                        col(CRMDeal.close_date).is_(None),
                        col(CRMDeal.source_modified_at) >= cutoff,
                    ),
                ),
            )
            rows.extend((await session.execute(stmt)).all())
        eligible_local_ids = {deal_id for (deal_id,) in rows}
        return [
            external_id
            for local_id, external_id in local_to_external.items()
            if local_id in eligible_local_ids
        ]

    return []
