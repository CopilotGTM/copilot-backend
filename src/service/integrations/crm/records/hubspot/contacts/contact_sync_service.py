"""HubSpot contacts record sync orchestrator.

Fresh-session lifecycle:

  Session A  — load KnowledgeIntegration + IntegrationTokenInfo. Release.
  (no sess)  — paginated HubSpot contacts fetch.
  (no sess)  — pure-Python normalise.
  Session B  — load `{company_external_id → company_id}` map, writer.apply
               (resolves company_id at write time), backfill_crm_deals, commit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.backfill import (
    DealFkBackfillOp,
    backfill_crm_deals,
)
from src.service.integrations.crm.records.hubspot.contacts.contact_normalizer import (
    normalize_contacts,
)
from src.service.integrations.crm.records.hubspot.contacts.contact_types import (
    ContactSyncResult,
    NormalizedHubSpotContact,
)
from src.service.integrations.crm.records.hubspot.contacts.contact_writer import (
    HubSpotContactWriter,
)
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
)
from src.workers.integrations_worker.providers.crm.hubspot.contacts_fetcher import (
    HubSpotContactsFetcher,
)

logger = get_logger(__name__)


class ContactSyncConfigurationError(ValueError):
    """Integration not usable for contacts record sync (missing, wrong type, inactive)."""


class HubSpotContactRecordSyncService:
    """Orchestrate one HubSpot contacts record sync run."""

    PROVIDER = "hubspot"
    OBJECT_TYPE = "contact"

    async def run(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> ContactSyncResult:
        """Run the main execution path for this service or task."""

        started_at = datetime.now(UTC)

        token_info = await self._load_token_info(organisation_id, integration_id)

        raw_contacts = await HubSpotContactsFetcher(token_info=token_info).fetch_all()

        normalized, skipped_malformed = normalize_contacts(raw_contacts)

        write_result, backfill_result = await self._persist(
            organisation_id=organisation_id,
            integration_id=integration_id,
            normalized=normalized,
        )

        completed_at = datetime.now(UTC)
        return ContactSyncResult(
            provider=self.PROVIDER,
            object_type=self.OBJECT_TYPE,
            organisation_id=organisation_id,
            integration_id=integration_id,
            started_at=started_at,
            completed_at=completed_at,
            fetched_contacts=len(raw_contacts),
            contacts_created=write_result.contacts_created,
            contacts_updated=write_result.contacts_updated,
            contacts_reactivated=write_result.contacts_reactivated,
            contacts_skipped_stale=write_result.contacts_skipped_stale,
            unresolved_company_link=write_result.unresolved_company_link,
            unresolved_owner_link=write_result.unresolved_owner_link,
            skipped_malformed=skipped_malformed,
            deal_backfill_rows_updated=backfill_result["rows_updated"],
            deal_backfill_per_op=backfill_result["per_op"],
        )

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
                raise ContactSyncConfigurationError(
                    f"Integration {integration_id} not found for organisation {organisation_id}"
                )
            int_type = integration.integration_type
            if isinstance(int_type, str):
                int_type = IntegrationType(int_type)
            if int_type != IntegrationType.HUBSPOT:
                raise ContactSyncConfigurationError(
                    f"Integration {integration_id} is {int_type.value}, expected hubspot"
                )
            if not integration.is_active:
                raise ContactSyncConfigurationError(f"Integration {integration_id} is not active")
            return IntegrationTokenInfo.from_integration(integration, organisation_id)
        raise ContactSyncConfigurationError(
            "Could not acquire org session for contacts record sync"
        )

    async def _persist(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        normalized: list[NormalizedHubSpotContact],
    ) -> tuple[Any, dict[str, Any]]:
        async for session in acquire_org_async_session(organisation_id):
            # Load companies + users snapshots ONCE for write-time FK
            # resolution. Same (org, integration) scope as the writer itself;
            # matches the chain's users → companies → contacts ordering so
            # both maps are already populated by the time we get here.
            company_map = await self._load_company_map(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )
            owner_map = await self._load_owner_map(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )

            writer = HubSpotContactWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )
            write_result = await writer.apply(
                normalized=normalized,
                company_map=company_map,
                owner_map=owner_map,
            )

            # Reload external_id → contact_id PK for the backfill op.
            contact_pk_map = await self._load_contact_pk_map(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )

            # One deal backfill op: primary_contact_external_id →
            # primary_contact_id (+ name + email). FK+name+email shape.
            backfill_payload: dict[str, Any] = {"rows_updated": 0, "per_op": {}}
            mapping: dict[str, tuple[UUID, str | None, None, str | None]] = {}
            for contact in normalized:
                pk = contact_pk_map.get(contact.external_id)
                if pk is None:
                    continue
                mapping[contact.external_id] = (
                    pk,
                    contact.full_name,
                    None,
                    contact.email,
                )

            if mapping:
                ops = [
                    DealFkBackfillOp(
                        external_col="primary_contact_external_id",
                        fk_col="primary_contact_id",
                        name_col="primary_contact_name",
                        email_col="primary_contact_email",
                        mapping=mapping,
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

        raise ContactSyncConfigurationError(
            "Could not acquire org session for contacts record sync write"
        )

    async def persist_raw_contact(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        raw_contact: dict[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        """Normalize and persist one contact without invoking a full contact fetch."""
        normalized, _ = normalize_contacts([raw_contact])
        return await self._persist(
            organisation_id=organisation_id,
            integration_id=integration_id,
            normalized=normalized,
        )

    async def _load_company_map(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, UUID]:
        stmt = select(CRMCompany).where(
            col(CRMCompany.organisation_id) == organisation_id,
            col(CRMCompany.integration_id) == integration_id,
            col(CRMCompany.is_deleted) == False,  # noqa: E712
        )
        rows = (await session.execute(stmt)).scalars().all()
        return {r.external_id: r.company_id for r in rows if r.external_id is not None}

    async def _load_owner_map(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, UUID]:
        """`{owner_external_id → crm_users.user_id}` for write-time owner_id resolution."""
        from src.domain.models.organisation_tables.sync.crm.users import CRMUser

        stmt = select(CRMUser).where(
            col(CRMUser.organisation_id) == organisation_id,
            col(CRMUser.integration_id) == integration_id,
            col(CRMUser.is_deleted) == False,  # noqa: E712
        )
        rows = (await session.execute(stmt)).scalars().all()
        return {r.external_id: r.user_id for r in rows if r.external_id is not None}

    async def _load_contact_pk_map(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, UUID]:
        from src.domain.models.organisation_tables.sync.crm.contacts import (
            CRMContact,
        )

        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == organisation_id,
            col(CRMContact.integration_id) == integration_id,
            col(CRMContact.is_deleted) == False,  # noqa: E712
        )
        rows = (await session.execute(stmt)).scalars().all()
        return {r.external_id: r.contact_id for r in rows if r.external_id is not None}
