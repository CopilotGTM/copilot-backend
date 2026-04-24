"""Salesforce users record sync orchestrator."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.master_tables.user_models import User
from src.domain.models.organisation_tables.sync import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.database.sessions import (
    acquire_master_async_session,
    acquire_org_async_session,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.backfill import (
    DealFkBackfillOp,
    backfill_crm_deals,
)
from src.service.integrations.crm.records.salesforce.users.user_normalizer import (
    normalize_email,
    normalize_users,
    resolve_matches,
)
from src.service.integrations.crm.records.salesforce.users.user_types import (
    UserMatchResult,
    UserSyncResult,
)
from src.service.integrations.crm.records.salesforce.users.user_writer import (
    SalesforceUserWriter,
)
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
)
from src.workers.integrations_worker.providers.crm.salesforce.users_fetcher import (
    SalesforceUsersFetcher,
)

logger = get_logger(__name__)


@dataclass(slots=True)
class SalesforceUserSyncContext:
    token_info: IntegrationTokenInfo
    instance_url: str


class SalesforceUserSyncConfigurationError(ValueError):
    """Integration not usable for Salesforce users record sync."""


class SalesforceUserRecordSyncService:
    PROVIDER = "salesforce"
    OBJECT_TYPE = "user"

    async def run(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> UserSyncResult:
        started_at = datetime.now(UTC)

        context = await self._load_context(organisation_id, integration_id)
        email_index = await self._load_app_user_email_index(organisation_id)

        raw_users = await SalesforceUsersFetcher(
            token_info=context.token_info,
            instance_url=context.instance_url,
        ).fetch_all()

        normalized, skipped_malformed = normalize_users(raw_users)
        match_results = resolve_matches(normalized, email_index)
        all_external_ids = [user.external_id for user in normalized]

        write_result, backfill_result = await self._persist(
            organisation_id=organisation_id,
            integration_id=integration_id,
            matches=match_results,
            all_external_ids=all_external_ids,
        )

        completed_at = datetime.now(UTC)
        result = UserSyncResult(
            provider=self.PROVIDER,
            object_type=self.OBJECT_TYPE,
            organisation_id=organisation_id,
            integration_id=integration_id,
            started_at=started_at,
            completed_at=completed_at,
            fetched_users=len(raw_users),
            users_created=write_result.users_created,
            users_updated=write_result.users_updated,
            users_reactivated=write_result.users_reactivated,
            users_deactivated=write_result.users_deactivated,
            users_matched_public=write_result.users_matched_public,
            users_minted_uuid=write_result.users_minted_uuid,
            skipped_malformed=skipped_malformed,
            skipped_dup_email=write_result.skipped_dup_email,
            app_user_index_size=len(email_index),
            deal_backfill_rows_updated=backfill_result["rows_updated"],
            deal_backfill_per_op=backfill_result["per_op"],
        )
        logger.info(
            "Salesforce users sync summary",
            extra={
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
                "fetched_users": result.fetched_users,
                "users_created": result.users_created,
                "users_updated": result.users_updated,
                "users_reactivated": result.users_reactivated,
                "users_deactivated": result.users_deactivated,
                "users_matched_public": result.users_matched_public,
                "users_minted_uuid": result.users_minted_uuid,
                "skipped_malformed": result.skipped_malformed,
                "skipped_dup_email": result.skipped_dup_email,
                "app_user_index_size": result.app_user_index_size,
            },
        )
        return result

    async def _load_context(
        self,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> SalesforceUserSyncContext:
        async for session in acquire_org_async_session(organisation_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.organisation_id) == organisation_id,
            )
            integration = (await session.execute(stmt)).scalar_one_or_none()
            if integration is None:
                raise SalesforceUserSyncConfigurationError(
                    f"Integration {integration_id} not found for organisation {organisation_id}"
                )
            int_type = integration.integration_type
            if isinstance(int_type, str):
                int_type = IntegrationType(int_type)
            if int_type != IntegrationType.SALESFORCE:
                raise SalesforceUserSyncConfigurationError(
                    f"Integration {integration_id} is {int_type.value}, expected salesforce"
                )
            if not integration.is_active:
                raise SalesforceUserSyncConfigurationError(
                    f"Integration {integration_id} is not active"
                )
            credentials = dict(integration.credentials or {})
            instance_url = str(credentials.get("instance_url") or "").rstrip("/")
            if not instance_url:
                raise SalesforceUserSyncConfigurationError(
                    f"Integration {integration_id} is missing Salesforce instance_url"
                )
            return SalesforceUserSyncContext(
                token_info=IntegrationTokenInfo.from_integration(integration, organisation_id),
                instance_url=instance_url,
            )
        raise SalesforceUserSyncConfigurationError(
            "Could not acquire org session for Salesforce users record sync"
        )

    async def _load_app_user_email_index(
        self,
        organisation_id: UUID,
    ) -> dict[str, UUID]:
        index: dict[str, UUID] = {}
        async for session in acquire_master_async_session():
            stmt = select(User).where(col(User.organisation_id) == organisation_id)
            rows = (await session.execute(stmt)).scalars().all()
            for user in rows:
                normalized = normalize_email(user.email)
                if normalized is None:
                    continue
                index[normalized] = user.user_id
        return index

    async def _persist(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        matches: list[UserMatchResult],
        all_external_ids: Iterable[str],
    ) -> tuple[Any, dict[str, Any]]:
        async for session in acquire_org_async_session(organisation_id):
            writer = SalesforceUserWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )
            write_result = await writer.apply(
                matches=matches,
                all_external_ids=all_external_ids,
            )

            user_pk_map = await self._load_user_pk_map(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )

            owner_mapping: dict[str, tuple[UUID, str | None, None, str | None]] = {}
            fk_only_mapping: dict[str, tuple[UUID, None, None, None]] = {}
            for match in matches:
                user = match.user
                pk = user_pk_map.get(user.external_id)
                if pk is None:
                    continue
                owner_mapping[user.external_id] = (pk, user.full_name, None, user.email)
                fk_only_mapping[user.external_id] = (pk, None, None, None)

            backfill_payload: dict[str, Any] = {"rows_updated": 0, "per_op": {}}
            if owner_mapping:
                ops = [
                    DealFkBackfillOp(
                        external_col="owner_external_id",
                        fk_col="owner_id",
                        name_col="owner_name",
                        email_col="owner_email",
                        mapping=owner_mapping,
                    ),
                    DealFkBackfillOp(
                        external_col="created_by_user_external_id",
                        fk_col="created_by_user_id",
                        mapping=fk_only_mapping,
                    ),
                    DealFkBackfillOp(
                        external_col="updated_by_user_external_id",
                        fk_col="updated_by_user_id",
                        mapping=fk_only_mapping,
                    ),
                ]
                backfill_result = await backfill_crm_deals(
                    session,
                    organisation_id=organisation_id,
                    integration_id=integration_id,
                    ops=ops,
                )
                backfill_payload["rows_updated"] = backfill_result.rows_updated
                backfill_payload["per_op"] = dict(backfill_result.per_op)

            await session.commit()
            return write_result, backfill_payload

        raise SalesforceUserSyncConfigurationError(
            "Could not acquire org session for Salesforce users record sync write"
        )

    async def _load_user_pk_map(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, UUID]:
        from src.domain.models.organisation_tables.sync.crm.users import CRMUser

        stmt = select(CRMUser).where(
            col(CRMUser.organisation_id) == organisation_id,
            col(CRMUser.integration_id) == integration_id,
            col(CRMUser.is_deleted) == False,  # noqa: E712
        )
        rows = (await session.execute(stmt)).scalars().all()
        return {row.external_id: row.user_id for row in rows if row.external_id is not None}
