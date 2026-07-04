"""Active-snapshot writer for CRM users from Salesforce User rows."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectType,
)
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.query_chunks import (
    LOCAL_ID_QUERY_CHUNK_SIZE,
    iter_chunks,
)
from src.service.integrations.crm.records.common.source_mapping import (
    CRMSourceMappingHelper,
    IntegrationMappingUpsert,
)
from src.service.integrations.crm.records.salesforce.users.user_types import (
    NormalizedSalesforceUser,
    UserMatchResult,
    UserWriteResult,
)

logger = get_logger(__name__)


class SalesforceUserWriter:
    """Writer for `crm_users` rows from Salesforce users."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.integration_id = integration_id
        self.source_mappings = CRMSourceMappingHelper(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            provider="salesforce",
        )

    async def apply(
        self,
        *,
        matches: list[UserMatchResult],
        all_external_ids: Iterable[str],
        deactivate_missing: bool = True,
    ) -> UserWriteResult:
        """Upsert Salesforce users into `crm_users` using one local row per email.

        Multiple Salesforce `User.Id` values can represent the same human, so
        source mappings converge on the canonical CRM row instead of minting
        duplicate local identities.
        """
        result = UserWriteResult()
        now = datetime.now(UTC)

        existing_by_external = await self._load_existing_rows()
        mapped_user_ids = {row.user_id for row in existing_by_external.values()}
        matched_user_ids = {
            match.matched_app_user_id for match in matches if match.matched_app_user_id is not None
        }
        existing_by_user_id = await self._load_existing_rows_by_user_ids(
            matched_user_ids - mapped_user_ids
        )
        existing_by_email = await self._load_existing_rows_by_email(
            {_normalized_email(match.user.email) for match in matches if match.user.email}
        )
        claimed_user_ids: set[UUID] = set(mapped_user_ids)
        canonical_by_email: dict[str, CRMUser] = {}
        # Salesforce can expose multiple User.Id values for one human/email.
        # Canonicalize by normalized email so every provider id maps to one
        # stable local crm_users.user_id.
        for row in [*existing_by_external.values(), *existing_by_user_id.values()]:
            if row.email:
                canonical_by_email.setdefault(_normalized_email(row.email), row)
        canonical_by_email.update(existing_by_email)
        mapping_rows: list[IntegrationMappingUpsert] = []

        for match in matches:
            user = match.user
            existing = existing_by_external.get(user.external_id)

            if existing is not None:
                incoming_active = user.is_active
                was_active = bool(existing.is_active)
                was_deleted = bool(existing.is_deleted)
                self._apply_updates(existing, user, now=now)
                existing.is_deleted = False
                mapping_rows.append(self._source_mapping_row(user, existing.user_id, now=now))
                if user.email:
                    canonical_by_email[_normalized_email(user.email)] = existing
                if was_deleted or (incoming_active and not was_active):
                    result.users_reactivated += 1
                else:
                    result.users_updated += 1
                continue

            email_key = _normalized_email(user.email) if user.email else None
            existing_email_row = canonical_by_email.get(email_key) if email_key else None
            if existing_email_row is not None:
                incoming_active = user.is_active
                was_active = bool(existing_email_row.is_active)
                was_deleted = bool(existing_email_row.is_deleted)
                self._apply_updates(existing_email_row, user, now=now)
                existing_email_row.is_deleted = False
                mapping_rows.append(
                    self._source_mapping_row(user, existing_email_row.user_id, now=now)
                )
                claimed_user_ids.add(existing_email_row.user_id)
                if match.matched_app_user_id == existing_email_row.user_id:
                    result.users_matched_public += 1
                if was_deleted or (incoming_active and not was_active):
                    result.users_reactivated += 1
                else:
                    result.users_updated += 1
                continue

            if match.matched_app_user_id is not None:
                existing_local = existing_by_user_id.get(match.matched_app_user_id)
                if existing_local is not None:
                    incoming_active = user.is_active
                    was_active = bool(existing_local.is_active)
                    was_deleted = bool(existing_local.is_deleted)
                    self._apply_updates(existing_local, user, now=now)
                    existing_local.is_deleted = False
                    mapping_rows.append(
                        self._source_mapping_row(user, existing_local.user_id, now=now)
                    )
                    claimed_user_ids.add(existing_local.user_id)
                    if user.email:
                        canonical_by_email[_normalized_email(user.email)] = existing_local
                    result.users_matched_public += 1
                    if was_deleted or (incoming_active and not was_active):
                        result.users_reactivated += 1
                    else:
                        result.users_updated += 1
                    continue

            user_id, came_from_public = self._resolve_user_id(match, claimed_user_ids)
            if user_id is None:
                result.skipped_dup_email += 1
                logger.warning(
                    "Duplicate Salesforce user maps to already-claimed public user_id; skipping",
                    extra={
                        "organisation_id": str(self.organisation_id),
                        "integration_id": str(self.integration_id),
                        "user_external_id": user.external_id,
                        "email": user.email,
                    },
                )
                continue

            row = self._new_row(user, user_id, now=now)
            self.session.add(row)
            mapping_rows.append(self._source_mapping_row(user, row.user_id, now=now))
            claimed_user_ids.add(user_id)
            if user.email:
                canonical_by_email[_normalized_email(user.email)] = row
            result.users_created += 1
            if came_from_public:
                result.users_matched_public += 1
            else:
                result.users_minted_uuid += 1

        if deactivate_missing:
            snapshot_external_ids = set(all_external_ids)
            for external_id, row in existing_by_external.items():
                if external_id in snapshot_external_ids:
                    continue
                if row.is_active:
                    row.is_active = False
                    result.users_deactivated += 1
        if deactivate_missing:
            await self.source_mappings.mark_missing_deleted(
                object_type=CRMSourceObjectType.USER,
                seen_external_ids=snapshot_external_ids,
            )

        if mapping_rows:
            await self.source_mappings.bulk_upsert_integration_mappings(mapping_rows)

        await self.session.flush()
        logger.info(
            "Salesforce user writer apply complete",
            extra={
                "organisation_id": str(self.organisation_id),
                "integration_id": str(self.integration_id),
                "users_created": result.users_created,
                "users_updated": result.users_updated,
                "users_reactivated": result.users_reactivated,
                "users_deactivated": result.users_deactivated,
                "users_matched_public": result.users_matched_public,
                "users_minted_uuid": result.users_minted_uuid,
                "skipped_dup_email": result.skipped_dup_email,
            },
        )
        return result

    def _resolve_user_id(
        self,
        match: UserMatchResult,
        claimed_user_ids: set[UUID],
    ) -> tuple[UUID | None, bool]:
        if match.matched_app_user_id is not None:
            if match.matched_app_user_id in claimed_user_ids:
                return None, True
            return match.matched_app_user_id, True
        while True:
            candidate = uuid4()
            if candidate not in claimed_user_ids:
                return candidate, False

    async def _load_existing_rows(self) -> dict[str, CRMUser]:
        external_to_local = await self.source_mappings.load_local_id_map(
            object_type=CRMSourceObjectType.USER,
            include_deleted=True,
        )
        if not external_to_local:
            return {}
        rows: list[CRMUser] = []
        for local_ids in iter_chunks(set(external_to_local.values()), LOCAL_ID_QUERY_CHUNK_SIZE):
            stmt = select(CRMUser).where(
                col(CRMUser.organisation_id) == self.organisation_id,
                col(CRMUser.user_id).in_(local_ids),
            )
            rows.extend((await self.session.execute(stmt)).scalars().all())
        local_to_external = {
            local_id: external_id for external_id, local_id in external_to_local.items()
        }
        return {
            local_to_external[row.user_id]: row for row in rows if row.user_id in local_to_external
        }

    async def _load_existing_rows_by_user_ids(self, user_ids: set[UUID]) -> dict[UUID, CRMUser]:
        if not user_ids:
            return {}
        rows: list[CRMUser] = []
        for local_ids in iter_chunks(user_ids, LOCAL_ID_QUERY_CHUNK_SIZE):
            stmt = select(CRMUser).where(
                col(CRMUser.organisation_id) == self.organisation_id,
                col(CRMUser.user_id).in_(local_ids),
            )
            rows.extend((await self.session.execute(stmt)).scalars().all())
        return {row.user_id: row for row in rows}

    async def _load_existing_rows_by_email(self, emails: set[str]) -> dict[str, CRMUser]:
        normalized_emails = {email for email in emails if email}
        if not normalized_emails:
            return {}
        stmt = select(CRMUser).where(
            col(CRMUser.organisation_id) == self.organisation_id,
            func.lower(func.btrim(CRMUser.email)).in_(normalized_emails),
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        result: dict[str, CRMUser] = {}
        for row in rows:
            if row.email:
                result.setdefault(_normalized_email(row.email), row)
        return result

    def _source_mapping_row(
        self,
        user: NormalizedSalesforceUser,
        local_user_id: UUID,
        *,
        now: datetime,
    ) -> IntegrationMappingUpsert:
        return IntegrationMappingUpsert(
            external_object_type=CRMSourceObjectType.USER,
            external_id=user.external_id,
            local_object_type=CRMSourceObjectType.USER,
            local_object_id=local_user_id,
            data_payload=user.data_payload,
            source_created_at=user.source_created_at,
            source_modified_at=user.source_modified_at,
            synced_at=now,
        )

    def _new_row(
        self,
        user: NormalizedSalesforceUser,
        app_user_id: UUID,
        *,
        now: datetime,
    ) -> CRMUser:
        return CRMUser(
            user_id=app_user_id,
            organisation_id=self.organisation_id,
            integration_id=self.integration_id,
            email=user.email,
            first_name=user.first_name,
            last_name=user.last_name,
            full_name=user.full_name,
            user_role=user.user_role,
            is_active=user.is_active,
            source_created_at=user.source_created_at,
            source_modified_at=user.source_modified_at,
            synced_at=now,
            last_seen_at=now,
            properties=user.properties,
        )

    def _apply_updates(
        self,
        row: CRMUser,
        user: NormalizedSalesforceUser,
        *,
        now: datetime,
    ) -> None:
        row.email = user.email
        row.first_name = user.first_name
        row.last_name = user.last_name
        row.full_name = user.full_name
        row.user_role = user.user_role
        row.is_active = user.is_active
        row.source_created_at = user.source_created_at
        row.source_modified_at = user.source_modified_at
        row.synced_at = now
        row.last_seen_at = now
        row.properties = user.properties


def _normalized_email(email: str) -> str:
    return email.strip().lower()
