"""Active-snapshot writer for CRM users from HubSpot owners.

Scoped to `(organisation_id, integration_id, is_deleted=false)` on every
statement. The writer does not skip owners without a `public.users` match —
it mints a fresh `user_id = uuid4()` for those rows so every non-malformed
HubSpot owner becomes addressable via `crm_users.user_id`.

Identity convergence (design):

  - matched owners        → `crm_users.user_id = public.users.user_id`
  - unmatched / no-email  → `crm_users.user_id = uuid4()`, no public.users write

When a `public.users` row is later created for the same email in this org,
the signup flow is expected to reuse the existing `crm_users.user_id` so
both tables share one UUID without a renumbering pass.

Two passes:

  Pass A — upsert every match result
    - existing row (keyed by source mapping)  → update, mark active
    - no existing row                          → insert with resolved or
                                                 freshly minted user_id
    - duplicate user_id collision (two owners → same already-claimed id) →
      skip, count, WARNING log

  Pass B — active-snapshot deactivation
    - any prior mapped row whose provider id isn't in the HubSpot snapshot
      (matched + unmatched) gets is_active=false.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectType,
)
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.source_mapping import CRMSourceMappingHelper
from src.service.integrations.crm.records.hubspot.users.user_types import (
    NormalizedHubSpotOwner,
    OwnerMatchResult,
    UserWriteResult,
)

logger = get_logger(__name__)


class HubSpotUserWriter:
    """Writer for crm_users rows from HubSpot owners."""

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
            provider="hubspot",
        )

    async def apply(
        self,
        *,
        matches: list[OwnerMatchResult],
        all_external_ids: Iterable[str],
    ) -> UserWriteResult:
        result = UserWriteResult()
        now = datetime.now(UTC)

        existing_by_external = await self._load_existing_rows()

        # Track claimed user_ids so two owners resolving to the same
        # public.users.user_id (rare — two HubSpot owners sharing an email)
        # don't PK-collide during insert. Seed with user_ids already present
        # on existing rows.
        claimed_user_ids: set[UUID] = {row.user_id for row in existing_by_external.values()}

        for match in matches:
            owner = match.owner
            existing = existing_by_external.get(owner.external_id)

            if existing is not None:
                was_active = bool(existing.is_active)
                was_deleted = bool(existing.is_deleted)
                self._apply_updates(existing, owner, now=now)
                existing.is_deleted = False
                await self._upsert_source_mapping(owner, existing.user_id, now=now)
                if was_deleted or not was_active:
                    existing.is_active = True
                    result.users_reactivated += 1
                else:
                    result.users_updated += 1
                continue

            user_id, came_from_public = self._resolve_user_id(match, claimed_user_ids)
            if user_id is None:
                result.skipped_dup_email += 1
                logger.warning(
                    "Duplicate HubSpot owner maps to already-claimed public user_id; skipping",
                    extra={
                        "organisation_id": str(self.organisation_id),
                        "integration_id": str(self.integration_id),
                        "owner_external_id": owner.external_id,
                        "email": owner.email,
                    },
                )
                continue

            row = self._new_row(owner, user_id, now=now)
            self.session.add(row)
            await self._upsert_source_mapping(owner, row.user_id, now=now)
            claimed_user_ids.add(user_id)
            result.users_created += 1
            if came_from_public:
                result.users_matched_public += 1
            else:
                result.users_minted_uuid += 1

        # Pass B — deactivation: any prior row whose external_id isn't in
        # the current HubSpot snapshot (matched + unmatched) gets
        # is_active=false.
        snapshot_external_ids = set(all_external_ids)
        for external_id, row in existing_by_external.items():
            if external_id in snapshot_external_ids:
                continue
            if row.is_active:
                row.is_active = False
                result.users_deactivated += 1
        await self.source_mappings.mark_missing_deleted(
            object_type=CRMSourceObjectType.USER,
            seen_external_ids=snapshot_external_ids,
        )

        await self.session.flush()
        logger.info(
            "User writer apply complete",
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
        match: OwnerMatchResult,
        claimed_user_ids: set[UUID],
    ) -> tuple[UUID | None, bool]:
        """Return `(user_id, came_from_public)`.

        - Matched owner → reuse `matched_app_user_id`; if already claimed in
          this run, return (None, _) so the caller can count + skip.
        - Unmatched owner (no email or no public match) → mint a fresh UUID.
          `uuid4()` collisions against already-claimed ids are astronomically
          unlikely, but we still loop defensively.
        """
        if match.matched_app_user_id is not None:
            if match.matched_app_user_id in claimed_user_ids:
                return None, True
            return match.matched_app_user_id, True
        while True:
            candidate = uuid4()
            if candidate not in claimed_user_ids:
                return candidate, False

    # ── Loading ──────────────────────────────────────────────────────────

    async def _load_existing_rows(self) -> dict[str, CRMUser]:
        """Every mapped crm_users row for this integration, keyed by provider id."""
        external_to_local = await self.source_mappings.load_local_id_map(
            object_type=CRMSourceObjectType.USER,
            include_deleted=True,
        )
        if not external_to_local:
            return {}
        stmt = select(CRMUser).where(
            col(CRMUser.organisation_id) == self.organisation_id,
            col(CRMUser.integration_id) == self.integration_id,
            col(CRMUser.user_id).in_(set(external_to_local.values())),
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        local_to_external = {
            local_id: external_id for external_id, local_id in external_to_local.items()
        }
        return {
            local_to_external[row.user_id]: row for row in rows if row.user_id in local_to_external
        }

    async def _upsert_source_mapping(
        self,
        owner: NormalizedHubSpotOwner,
        local_user_id: UUID,
        *,
        now: datetime,
    ) -> None:
        await self.source_mappings.upsert_integration_mapping(
            external_object_type=CRMSourceObjectType.USER,
            external_id=owner.external_id,
            local_object_type=CRMSourceObjectType.USER,
            local_object_id=local_user_id,
            data_payload=owner.data_payload,
            source_created_at=owner.source_created_at,
            source_modified_at=owner.source_modified_at,
            synced_at=now,
        )

    # ── Row construction ─────────────────────────────────────────────────

    def _new_row(
        self,
        owner: NormalizedHubSpotOwner,
        app_user_id: UUID,
        *,
        now: datetime,
    ) -> CRMUser:
        return CRMUser(
            user_id=app_user_id,
            organisation_id=self.organisation_id,
            integration_id=self.integration_id,
            email=owner.email,
            first_name=owner.first_name,
            last_name=owner.last_name,
            full_name=owner.full_name,
            user_role=owner.user_role,
            primary_team_id=owner.primary_team_id,
            team_ids=list(owner.team_ids),
            is_active=True,
            source_created_at=owner.source_created_at,
            source_modified_at=owner.source_modified_at,
            synced_at=now,
            last_seen_at=now,
            properties=owner.properties,
        )

    def _apply_updates(
        self,
        row: CRMUser,
        owner: NormalizedHubSpotOwner,
        *,
        now: datetime,
    ) -> None:
        # user_id is the PK — do NOT update it. Even if a match re-evaluation
        # would yield a different app user_id on a later sync (e.g. email
        # changed), the pre-existing PK is referenced by CRMDeal.owner_id and
        # friends; rewriting it would break integrity.
        row.email = owner.email
        row.first_name = owner.first_name
        row.last_name = owner.last_name
        row.full_name = owner.full_name
        row.user_role = owner.user_role
        row.primary_team_id = owner.primary_team_id
        row.team_ids = list(owner.team_ids)
        row.source_created_at = owner.source_created_at
        row.source_modified_at = owner.source_modified_at
        row.synced_at = now
        row.last_seen_at = now
        row.properties = owner.properties
