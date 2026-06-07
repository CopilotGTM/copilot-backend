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
from src.domain.models.organisation_tables.sync.crm import CRMSourceObjectType
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.infra.database.sessions import (
    acquire_master_async_session,
    acquire_org_async_session,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.backfill import (
    DealFkBackfillOp,
    backfill_crm_deals,
)
from src.service.integrations.crm.records.common.query_chunks import (
    LOCAL_ID_QUERY_CHUNK_SIZE,
    iter_chunks,
)
from src.service.integrations.crm.records.common.source_mapping import CRMSourceMappingHelper
from src.service.integrations.crm.records.salesforce.users.user_normalizer import (
    build_owner_backfill_mapping,
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

            owner_mapping, fk_only_mapping = build_owner_backfill_mapping(
                users=[match.user for match in matches],
                user_pk_map=user_pk_map,
            )

            backfill_payload: dict[str, Any] = {"rows_updated": 0, "per_op": {}}
            if owner_mapping:
                backfill_payload = await self._backfill_owner_references(
                    session=session,
                    organisation_id=organisation_id,
                    integration_id=integration_id,
                    owner_mapping=owner_mapping,
                    fk_only_mapping=fk_only_mapping,
                )

            await session.commit()
            return write_result, backfill_payload

        raise SalesforceUserSyncConfigurationError(
            "Could not acquire org session for Salesforce users record sync write"
        )

    async def persist_single_user(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        raw_user: dict[str, Any],
    ) -> UserSyncResult:
        started_at = datetime.now(UTC)
        email_index = await self._load_app_user_email_index(organisation_id)
        normalized, skipped_malformed = normalize_users([raw_user])
        match_results = resolve_matches(normalized, email_index)

        write_result, backfill_result = await self._persist_single(
            organisation_id=organisation_id,
            integration_id=integration_id,
            matches=match_results,
        )

        completed_at = datetime.now(UTC)
        return UserSyncResult(
            provider=self.PROVIDER,
            object_type=self.OBJECT_TYPE,
            organisation_id=organisation_id,
            integration_id=integration_id,
            started_at=started_at,
            completed_at=completed_at,
            fetched_users=1,
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

    async def _persist_single(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        matches: list[UserMatchResult],
    ) -> tuple[Any, dict[str, Any]]:
        async for session in acquire_org_async_session(organisation_id):
            writer = SalesforceUserWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )
            write_result = await writer.apply(
                matches=matches,
                all_external_ids=[match.user.external_id for match in matches],
                deactivate_missing=False,
            )
            user_pk_map = await self._load_user_pk_map(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )
            owner_mapping, fk_only_mapping = build_owner_backfill_mapping(
                users=[match.user for match in matches],
                user_pk_map=user_pk_map,
            )
            backfill_payload = {"rows_updated": 0, "per_op": {}}
            if owner_mapping:
                backfill_payload = await self._backfill_owner_references(
                    session=session,
                    organisation_id=organisation_id,
                    integration_id=integration_id,
                    owner_mapping=owner_mapping,
                    fk_only_mapping=fk_only_mapping,
                )
            await session.commit()
            return write_result, backfill_payload

        raise SalesforceUserSyncConfigurationError(
            "Could not acquire org session for Salesforce single user write"
        )

    async def _backfill_owner_references(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        owner_mapping: dict[str, tuple[UUID, str | None, None, str | None]],
        fk_only_mapping: dict[str, tuple[UUID, None, None, None]],
    ) -> dict[str, Any]:
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
        deal_result = await backfill_crm_deals(
            session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            ops=ops,
        )
        owner_rows = await _backfill_owner_tables(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            owner_mapping=owner_mapping,
        )
        per_op = dict(deal_result.per_op)
        per_op.update(owner_rows)
        return {
            "rows_updated": deal_result.rows_updated + sum(owner_rows.values()),
            "per_op": per_op,
        }

    async def _load_user_pk_map(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, UUID]:
        helper = CRMSourceMappingHelper(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            provider=self.PROVIDER,
        )
        external_to_local = await helper.load_local_id_map(object_type=CRMSourceObjectType.USER)
        if not external_to_local:
            return {}
        rows = []
        for local_ids in iter_chunks(set(external_to_local.values()), LOCAL_ID_QUERY_CHUNK_SIZE):
            stmt = select(CRMUser.user_id).where(
                col(CRMUser.organisation_id) == organisation_id,
                col(CRMUser.integration_id) == integration_id,
                col(CRMUser.is_deleted) == False,  # noqa: E712
                col(CRMUser.user_id).in_(local_ids),
            )
            rows.extend((await session.execute(stmt)).all())
        live_ids = {user_id for (user_id,) in rows}
        return {
            external_id: user_id
            for external_id, user_id in external_to_local.items()
            if user_id in live_ids
        }


async def _backfill_owner_tables(
    *,
    session,
    organisation_id: UUID,
    integration_id: UUID,
    owner_mapping: dict[str, tuple[UUID, str | None, None, str | None]],
) -> dict[str, int]:
    del session, organisation_id, integration_id, owner_mapping
    return {
        "crm_companies.owner_id": 0,
        "crm_contacts.owner_id": 0,
        "crm_activities.owner_id": 0,
    }
