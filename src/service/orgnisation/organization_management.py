"""Organisation control-plane state and shared-tenant default setup.

The admin worker calls this service after accepting an organisation-creation
job. Database infrastructure is deployment-owned; this module only writes the
master record and seeds organisation-scoped defaults through the org session.
"""

import time
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.master_tables import (
    Organisation,
    OrganisationActionHistory,
    OrganisationEventType,
    OrganisationStatus,
)
from src.infra.database.sessions import (
    master_async_session_scope,
)
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.cursor import decode_cursor, encode_cursor
from src.infra.utils.logger import get_logger

logger = get_logger("organization_management_service")


class _HasTable(Protocol):
    __table__: Any


class ProvisioningError(Exception):
    """Custom exception for failures during organization provisioning."""

    def __init__(self, message: str, organisation_id: UUID | None = None):
        self.organisation_id = organisation_id
        super().__init__(f"Provisioning failed for org {organisation_id}: {message}")


class OrganizationManagementService:
    """Manage master organisation state and shared-schema tenant defaults."""

    async def create_organization(
        self,
        organisation_id: UUID,
        admin_id: UUID,
        organisation_name: str,
        **org_details,
    ) -> dict[str, Any]:
        """Create master organisation state and idempotent shared-tenant defaults.

        Master and organisation sessions remain separate so CRM/storage setup
        never holds the control-plane connection. The permanent `tenant_data`
        schema, runtime role, and migrations are deployment-owned prerequisites.

        Args:
            admin_id: UUID of the admin creating the organisation.
            organisation_name: The name of the organization.
            **org_details: Additional details for the organization.

        Returns:
            A dictionary containing the created organization details.

        Raises:
            ProvisioningError: If any step in the organization creation process fails.
            ValueError: If the organization already exists and is not retryable.
        """
        logger.info(f"Starting creation process for organization: {organisation_name}")

        # ── Phase 1: create/update org record, then release session ──
        async with master_async_session_scope() as session:
            existing_org = await session.get(Organisation, organisation_id)

            if existing_org:
                # Allow retry from FAILED or PROVISIONING (task retry / idempotency)
                if existing_org.status in (
                    OrganisationStatus.FAILED,
                    OrganisationStatus.PROVISIONING,
                ):
                    logger.info(
                        f"Retrying provisioning for organization '{organisation_name}' "
                        f"(ID: {existing_org.organisation_id}, "
                        f"status: {existing_org.status.value})."
                    )
                    organization = existing_org
                    organization.status = OrganisationStatus.PROVISIONING
                    for key, value in org_details.items():
                        setattr(organization, key, value)
                else:
                    raise ValueError(
                        f"Organization '{organisation_name}' already exists "
                        f"with status '{existing_org.status.value}'."
                    )
            else:
                org_data = org_details.copy()
                org_data["linked_urls"] = org_data.pop(
                    "linked_urls", org_data.pop("linked_in_url", [])
                )
                org_data["website_urls"] = org_data.pop(
                    "website_urls", org_data.pop("website_url", [])
                )

                organization = Organisation(
                    organisation_id=organisation_id,
                    organisation_name=organisation_name,
                    status=OrganisationStatus.PROVISIONING,
                    created_by_auth_user_id=str(admin_id),
                    **org_data,
                )
                session.add(organization)

            await session.commit()
            await session.refresh(organization)

            # Extract into locals so session can be released
            organisation_id = organization.organisation_id
            org_name = organization.organisation_name

        logger.info(
            "Organization record ready for default setup — ID: %s, name: %s",
            organisation_id,
            org_name,
        )

        try:
            provision_metrics: dict[str, float] = {}

            storage_start = time.perf_counter()
            await self._provision_org_storage_container(organisation_id)
            provision_metrics["storage_container"] = round(
                time.perf_counter() - storage_start,
                4,
            )
        except ProvisioningError:
            await self._mark_org_failed(
                organisation_id,
                admin_id,
                details={"error": "Provisioning failed (wrapped)."},
            )
            raise
        except Exception as e:
            await self._mark_org_failed(
                organisation_id,
                admin_id,
                details={
                    "error": f"Provisioning failed: {e.__class__.__name__}",
                    "message": str(e),
                },
            )
            raise ProvisioningError(str(e), organisation_id=organisation_id) from e

        # ── Phase 3: mark PENDING with a fresh session ──
        async with master_async_session_scope() as session:
            org_row = await session.get(Organisation, organisation_id)
            if not org_row:
                raise ProvisioningError(
                    "Organisation row disappeared after provisioning",
                    organisation_id=organisation_id,
                )
            organization = org_row
            organization.status = OrganisationStatus.PENDING
            session.add(
                OrganisationActionHistory(
                    organisation_id=organisation_id,
                    action=OrganisationEventType.CREATED,
                    actor_admin_id=admin_id,
                    details={
                        "message": "Organization provisioned and ready for approval.",
                        "metrics": provision_metrics,
                    },
                )
            )
            await session.commit()
            await session.refresh(organization)

        logger.info(
            f"Successfully provisioned organization: {org_name} | metrics={provision_metrics}"
        )
        return {"organization": organization}

    async def _mark_org_failed(
        self,
        organisation_id: UUID,
        admin_id: UUID,
        details: dict[str, Any],
    ) -> None:
        """Mark an organisation as FAILED using a fresh session."""
        try:
            async with master_async_session_scope() as session:
                org = await session.get(Organisation, organisation_id)
                if org:
                    org.status = OrganisationStatus.FAILED
                    session.add(
                        OrganisationActionHistory(
                            organisation_id=organisation_id,
                            action=OrganisationEventType.SYSTEM_MIGRATION,
                            actor_admin_id=admin_id,
                            details=details,
                        )
                    )
                    await session.commit()
        except Exception:
            logger.error("Failed to mark org %s as FAILED in DB", organisation_id, exc_info=True)

    async def _provision_org_storage_container(self, organisation_id: UUID) -> None:
        """Create the required deterministic organisation container."""
        async with AzureOrganisationBlobStorage(organisation_id) as storage:
            await storage.provision_container()

    async def update_organisation_status(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        admin_id: UUID,
        new_status: OrganisationStatus,
        note: str | None = None,
    ) -> Organisation:
        """
        Updates the status of an organization and logs the action.
        """
        logger.info(
            f"Updating status of organization {organisation_id} to '{new_status}' by admin {admin_id}"
        )

        organisation = await session.get(Organisation, organisation_id)
        if not organisation:
            raise ValueError(f"Organization with ID {organisation_id} not found.")

        if organisation.status == new_status:
            logger.warning(f"Organization {organisation_id} is already in state '{new_status}'.")
            return organisation

        # State transition logic
        if new_status == OrganisationStatus.APPROVED:
            if organisation.status != OrganisationStatus.PENDING:
                raise ValueError(
                    f"Organization must be in PENDING state to be approved, but is in {organisation.status} state."
                )
            organisation.approved_by = admin_id
            organisation.approved_at = datetime.now(UTC)
            event_type = OrganisationEventType.APPROVED
        elif new_status == OrganisationStatus.REJECTED:
            if organisation.status != OrganisationStatus.PENDING:
                raise ValueError(
                    f"Organization must be in PENDING state to be rejected, but is in {organisation.status} state."
                )
            event_type = OrganisationEventType.REJECTED
        elif new_status == OrganisationStatus.SUSPENDED:
            if organisation.status != OrganisationStatus.APPROVED:
                raise ValueError(
                    f"Organization must be in APPROVED state to be suspended, but is in {organisation.status} state."
                )
            event_type = OrganisationEventType.SUSPENDED
        else:
            raise ValueError(f"Unsupported status transition to '{new_status}'.")

        organisation.status = new_status

        history_entry = OrganisationActionHistory(
            organisation_id=organisation_id,
            action=event_type,
            actor_admin_id=admin_id,
            note=note,
            details={
                "message": f"Organization status changed to {new_status} by admin {admin_id}."
            },
        )
        session.add(history_entry)
        await session.commit()
        await session.refresh(organisation)
        logger.info(f"Successfully updated organization {organisation_id} status to '{new_status}'")
        return organisation

    async def update_organization(
        self,
        session: AsyncSession,
        admin_id: UUID,
        organisation_id: UUID,
        update_data: dict[str, Any],
    ) -> Organisation:
        """
        Updates an existing organization's details.
        """
        logger.info(f"Updating organization {organisation_id} by admin {admin_id}")

        organisation = await session.get(Organisation, organisation_id)
        if not organisation:
            raise ValueError(f"Organization with ID {organisation_id} not found.")

        if organisation.status not in [
            OrganisationStatus.APPROVED,
            OrganisationStatus.PENDING,
        ]:
            raise ValueError(f"Organization with status '{organisation.status}' cannot be updated.")

        updated_fields = []
        for key, value in update_data.items():
            if hasattr(organisation, key) and value is not None:
                old_value = getattr(organisation, key)
                if old_value != value:
                    setattr(organisation, key, value)
                    updated_fields.append(
                        {
                            "field": key,
                            "old_value": str(old_value),
                            "new_value": str(value),
                        }
                    )

        if "organisation_name" in update_data:
            new_name = update_data["organisation_name"]
            org_table = cast(_HasTable, Organisation).__table__
            org_name_col = cast(Any, org_table.c.organisation_name)
            org_id_col = cast(Any, org_table.c.organisation_id)
            with session.no_autoflush:
                result = await session.execute(
                    select(Organisation).where(
                        org_name_col == new_name,
                        org_id_col != organisation_id,
                    )
                )
                if result.scalars().first():
                    raise ValueError(f"Organization name '{new_name}' already exists.")

        if not updated_fields:
            logger.warning(f"No fields to update for organization {organisation_id}")
            return organisation

        history_entry = OrganisationActionHistory(
            organisation_id=organisation_id,
            action=OrganisationEventType.FIELD_CHANGED,
            actor_admin_id=admin_id,
            details={"changes": updated_fields},
        )
        session.add(history_entry)
        await session.commit()
        await session.refresh(organisation)

        logger.info(
            f"Successfully updated organization {organisation_id} with fields: {[f['field'] for f in updated_fields]}"
        )
        return organisation

    async def get_organization_history(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        limit: int,
        cursor: str | None = None,
    ) -> list[OrganisationActionHistory]:
        """
        Retrieves the action history for a specific organization with pagination.
        """
        logger.info(f"Fetching action history for organization {organisation_id}")

        oah_table = cast(_HasTable, OrganisationActionHistory).__table__
        oah_cols = oah_table.c
        org_id_col = cast(Any, oah_cols.organisation_id)
        created_at_col = cast(Any, oah_cols.created_at)
        history_id_col = cast(Any, oah_cols.id)

        filters: list[Any] = [org_id_col == organisation_id]
        if cursor:
            ts, history_id = decode_cursor(cursor)
            filters.append(
                or_(
                    created_at_col < ts,
                    and_(
                        created_at_col == ts,
                        history_id_col < history_id,
                    ),
                )
            )

        stmt = (
            select(OrganisationActionHistory)
            .where(*filters)
            .order_by(
                created_at_col.desc(),
                history_id_col.desc(),
            )
            .limit(limit + 1)
        )

        result = await session.execute(stmt)
        history = result.scalars().all()
        return list(history)

    async def get_all_organization_history(
        self,
        session: AsyncSession,
        limit: int,
        cursor: str | None = None,
    ) -> list[OrganisationActionHistory]:
        """
        Retrieves the action history for all organizations with pagination.
        """
        logger.info("Fetching action history for all organizations")

        oah_table = cast(_HasTable, OrganisationActionHistory).__table__
        oah_cols = oah_table.c
        created_at_col = cast(Any, oah_cols.created_at)
        history_id_col = cast(Any, oah_cols.id)

        filters: list[Any] = []
        if cursor:
            ts, history_id = decode_cursor(cursor)
            filters.append(
                or_(
                    created_at_col < ts,
                    and_(
                        created_at_col == ts,
                        history_id_col < history_id,
                    ),
                )
            )

        stmt = (
            select(OrganisationActionHistory)
            .where(*filters)
            .order_by(
                created_at_col.desc(),
                history_id_col.desc(),
            )
            .limit(limit + 1)
        )

        result = await session.execute(stmt)
        history = result.scalars().all()
        return list(history)

    # Override history fetchers to return cursor metadata without breaking original callers
    async def get_organization_history_with_cursor(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        limit: int,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return one organisation's history with cursor metadata."""
        items = await self.get_organization_history(session, organisation_id, limit, cursor)
        has_more = len(items) > limit
        page_items = items[:limit]
        next_cursor = (
            encode_cursor(page_items[-1].created_at, page_items[-1].id) if has_more else None
        )
        return {"items": page_items, "next_cursor": next_cursor, "has_more": has_more}

    async def get_all_organization_history_with_cursor(
        self,
        session: AsyncSession,
        limit: int,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return cross-organisation history with cursor metadata."""
        items = await self.get_all_organization_history(session, limit, cursor)
        has_more = len(items) > limit
        page_items = items[:limit]
        next_cursor = (
            encode_cursor(page_items[-1].created_at, page_items[-1].id) if has_more else None
        )
        return {"items": page_items, "next_cursor": next_cursor, "has_more": has_more}


organization_management_service = OrganizationManagementService()
