"""HubSpot users (owners) record sync orchestrator.

Fresh-session lifecycle:

  Session A  — load KnowledgeIntegration + IntegrationTokenInfo AND the
               org's app-user email index (public.users scoped by org).
               Release.
  (no sess)  — paginated HubSpot owners fetch.
  (no sess)  — pure-Python normalise + match.
  Session B  — writer.apply() + backfill_crm_deals + commit.

Identity bridge: for owners whose email matches a `public.users` row in this
org, `crm_users.user_id = public.users.user_id`. For everyone else, a fresh
`uuid4()` is minted inside the writer. The sync never writes `public.users`.
See the module-level docstring on `user_types.py` for the signup-flow
convergence contract.
"""

from __future__ import annotations

from collections.abc import Iterable
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
from src.service.integrations.crm.records.users.user_normalizer import (
    normalize_email,
    normalize_owners,
    resolve_matches,
)
from src.service.integrations.crm.records.users.user_types import (
    OwnerMatchResult,
    UserSyncResult,
)
from src.service.integrations.crm.records.users.user_writer import (
    HubSpotUserWriter,
)
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
)
from src.workers.integrations_worker.providers.crm.hubspot.owners_fetcher import (
    HubSpotOwnersFetcher,
)

logger = get_logger(__name__)


class UserSyncConfigurationError(ValueError):
    """Integration not usable for users record sync (missing, wrong type, inactive)."""


class HubSpotUserRecordSyncService:
    """Orchestrate one HubSpot users record sync run."""

    PROVIDER = "hubspot"
    OBJECT_TYPE = "user"

    async def run(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> UserSyncResult:
        started_at = datetime.now(UTC)

        token_info = await self._load_token_info(organisation_id, integration_id)
        email_index = await self._load_app_user_email_index(organisation_id)

        raw_owners = await HubSpotOwnersFetcher(token_info=token_info).fetch_all()

        normalized, skipped_malformed = normalize_owners(raw_owners)
        match_results = resolve_matches(normalized, email_index)
        all_external_ids = [o.external_id for o in normalized]

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
            fetched_owners=len(raw_owners),
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
            "HubSpot users sync summary",
            extra={
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
                "fetched_owners": result.fetched_owners,
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

    async def _load_token_info(
        self,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> IntegrationTokenInfo:
        async for session in acquire_org_async_session(organisation_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.organisation_id) == organisation_id,
            )
            integration = (await session.execute(stmt)).scalar_one_or_none()
            if integration is None:
                raise UserSyncConfigurationError(
                    f"Integration {integration_id} not found for organisation {organisation_id}"
                )
            int_type = integration.integration_type
            if isinstance(int_type, str):
                int_type = IntegrationType(int_type)
            if int_type != IntegrationType.HUBSPOT:
                raise UserSyncConfigurationError(
                    f"Integration {integration_id} is {int_type.value}, expected hubspot"
                )
            if not integration.is_active:
                raise UserSyncConfigurationError(f"Integration {integration_id} is not active")
            return IntegrationTokenInfo.from_integration(integration, organisation_id)
        raise UserSyncConfigurationError("Could not acquire org session for users record sync")

    async def _load_app_user_email_index(
        self,
        organisation_id: UUID,
    ) -> dict[str, UUID]:
        """Build `{normalized_email → app user_id}` for this org only.

        Scoped to `organisation_id` so a HubSpot owner in org A can never
        accidentally match a `public.users` row in org B.
        """
        index: dict[str, UUID] = {}
        async for session in acquire_master_async_session():
            stmt = select(User).where(
                col(User.organisation_id) == organisation_id,
            )
            rows = (await session.execute(stmt)).scalars().all()
            for user in rows:
                normalized = normalize_email(user.email)
                if normalized is None:
                    continue
                # public.users.email is globally unique, so collisions on the
                # normalized form within one org are impossible in practice.
                index[normalized] = user.user_id
        return index

    async def _persist(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        matches: list[OwnerMatchResult],
        all_external_ids: Iterable[str],
    ) -> tuple[Any, dict[str, Any]]:
        async for session in acquire_org_async_session(organisation_id):
            writer = HubSpotUserWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )
            write_result = await writer.apply(
                matches=matches,
                all_external_ids=all_external_ids,
            )

            # Reload external_id → user_id PK for the backfill ops so we pick
            # up both matched and freshly-minted ids uniformly.
            owner_pk_map = await self._load_owner_pk_map(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )

            # Build backfill ops. Owner op carries name + email; created_by /
            # updated_by are FK-only (deals populate those external_ids in
            # phase 6).
            owner_mapping: dict[str, tuple[UUID, str | None, None, str | None]] = {}
            fk_only_mapping: dict[str, tuple[UUID, None, None, None]] = {}
            for match in matches:
                owner = match.owner
                pk = owner_pk_map.get(owner.external_id)
                if pk is None:
                    # Writer dropped this row (dup-email collision) — skip
                    # the backfill too; deal rows will backfill on a later
                    # successful run.
                    continue
                owner_mapping[owner.external_id] = (
                    pk,
                    owner.full_name,
                    None,
                    owner.email,
                )
                fk_only_mapping[owner.external_id] = (
                    pk,
                    None,
                    None,
                    None,
                )

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

        raise UserSyncConfigurationError(
            "Could not acquire org session for users record sync write"
        )

    async def _load_owner_pk_map(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, UUID]:
        """`{owner_external_id → crm_users.user_id}` — for deal backfill and
        for companies/contacts phases to resolve `owner_id` at write time."""
        from src.domain.models.organisation_tables.sync.crm.users import CRMUser

        stmt = select(CRMUser).where(
            col(CRMUser.organisation_id) == organisation_id,
            col(CRMUser.integration_id) == integration_id,
            col(CRMUser.is_deleted) == False,  # noqa: E712
        )
        rows = (await session.execute(stmt)).scalars().all()
        return {r.external_id: r.user_id for r in rows if r.external_id is not None}
