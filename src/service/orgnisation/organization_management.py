import os
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from azure.identity import (
    DefaultAzureCredential,
    ManagedIdentityCredential,
    WorkloadIdentityCredential,
)
from azure.keyvault.secrets import SecretClient
from azure.storage.blob import BlobServiceClient
from migrations.migration_manager import migration_manager
from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.master_tables import (
    Organisation,
    OrganisationActionHistory,
    OrganisationEventType,
    OrganisationStatus,
)
from src.infra.config.configs import (
    cloud_infra_config,
    cloud_storage_config,
    database_config,
    secret_manager_config,
)
from src.infra.database.sessions import (
    acquire_org_async_session,
    master_async_session_scope,
    master_session_scope,
)
from src.infra.database.tenant_runtime_config import validate_tenant_runtime_config
from src.infra.database.tenant_runtime_grants import TenantRuntimeGrantService
from src.infra.utils.cursor import decode_cursor, encode_cursor
from src.infra.utils.logger import get_logger
from src.infra.utils.resource_naming import (
    get_key_vault_secret_name,
    get_org_container_name,
    get_role_name,
    get_schema_name,
)
from src.service.crm.internal.internal_crm_setup_service import InternalCRMSetupService

logger = get_logger("organization_management_service")


class _HasTable(Protocol):
    __table__: Any


class ProvisioningError(Exception):
    """Custom exception for failures during organization provisioning."""

    def __init__(self, message: str, organisation_id: UUID | None = None):
        self.organisation_id = organisation_id
        super().__init__(f"Provisioning failed for org {organisation_id}: {message}")


class DeletionError(Exception):
    """Raised when destructive cleanup cannot complete fully."""

    pass


class OrganizationManagementService:
    """Service for managing organizations with schema-level isolation."""

    def __init__(self):
        """Initializes the service."""
        self._vector_extension_checked = False

    # Master connection is acquired on-demand from master dependency

    async def create_organization(
        self,
        admin_id: UUID,
        organisation_name: str,
        **org_details,
    ) -> dict[str, Any]:
        """
        Creates a new organization with a dedicated schema, role, and runs migrations.

        Manages its own sessions in three phases to avoid holding a DB connection
        across long-running provisioning steps (migrations, Key Vault, storage).

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
            org_table = cast(_HasTable, Organisation).__table__
            org_name_col = cast(Any, org_table.c.organisation_name)
            result = await session.execute(
                select(Organisation).where(org_name_col == organisation_name)
            )
            existing_org = result.scalars().first()

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
                org_data["linked_urls"] = org_data.pop("linked_in_url", [])
                org_data["website_urls"] = org_data.pop("website_url", [])

                organization = Organisation(
                    organisation_id=uuid4(),
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

        # ── Phase 2: provisioning (no session held) ──
        schema_name = get_schema_name(organisation_id)
        role_name = get_role_name(organisation_id)
        role_password = secrets.token_urlsafe(16)

        logger.info(
            f"Organization record ready for provisioning — ID: {organisation_id}, name: {org_name}"
        )

        try:
            provision_metrics: dict[str, float] = {}

            def time_step(label: str, fn: Callable[[], None]):
                start = time.perf_counter()
                fn()
                provision_metrics[label] = round(time.perf_counter() - start, 4)

            time_step(
                "tenant_database",
                lambda: self._provision_tenant_database(schema_name, role_name, role_password),
            )
            time_step(
                "store_secret",
                lambda: self._store_password_in_key_vault(organisation_id, role_password),
            )
            crm_start = time.perf_counter()
            await self._setup_internal_crm(organisation_id, admin_id)
            provision_metrics["internal_crm_setup"] = round(time.perf_counter() - crm_start, 4)
            time_step(
                "storage_container",
                lambda: self._provision_org_storage_container(organisation_id),
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
        return {"organization": organization, "schema_name": schema_name}

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

    def _create_database_role_and_schema(self, schema_name: str, role_name: str, password: str):
        """
        Creates or updates the database role and schema for an organization idempotently.
        This operation is transactional; it will roll back on any failure.
        """
        self._validate_identifier(schema_name, "schema_name")
        self._validate_identifier(role_name, "role_name")
        logger.info(f"Ensuring database role '{role_name}' and schema '{schema_name}' exist.")
        try:
            # On first use, ensure vector extension availability is checked once
            if not self._vector_extension_checked:
                self._ensure_vector_extension_available()
                self._vector_extension_checked = True

            with master_session_scope() as session, session.begin():
                # Check if role exists
                role_exists = session.execute(
                    text("SELECT 1 FROM pg_roles WHERE rolname = :role_name"),
                    {"role_name": role_name},
                ).scalar_one_or_none()

                if role_exists:
                    logger.warning(f"Role '{role_name}' already exists. Updating password.")
                    session.execute(
                        text(f"ALTER ROLE {role_name} WITH PASSWORD :pwd"),
                        {"pwd": password},
                    )
                else:
                    logger.info(f"Role '{role_name}' not found. Creating new role.")
                    session.execute(
                        text(f"CREATE ROLE {role_name} LOGIN PASSWORD :pwd NOINHERIT"),
                        {"pwd": password},
                    )

                # Create schema if it doesn't exist and set authorization
                session.execute(
                    text(f"CREATE SCHEMA IF NOT EXISTS {schema_name} AUTHORIZATION {role_name}")
                )

                # Grant necessary permissions
                session.execute(text(f"GRANT USAGE, CREATE ON SCHEMA {schema_name} TO {role_name}"))
                session.execute(
                    text(f"GRANT ALL PRIVILEGES ON SCHEMA {schema_name} TO {role_name}")
                )

                # Set default privileges for future objects created by the role
                session.execute(
                    text(
                        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema_name} GRANT ALL ON TABLES TO {role_name}"
                    )
                )
                session.execute(
                    text(
                        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema_name} GRANT ALL ON SEQUENCES TO {role_name}"
                    )
                )
                session.execute(
                    text(
                        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema_name} GRANT ALL ON FUNCTIONS TO {role_name}"
                    )
                )

                # Ensure the role gets the correct schema by default for new connections in this DB
                db_name = database_config.DATABASE_NAME
                session.execute(
                    text(
                        f'ALTER ROLE {role_name} IN DATABASE "{db_name}" SET search_path = "{schema_name}", public'
                    )
                )

                # Grant role to admin user for maintenance
                admin_user = database_config.DATABASE_USER
                if admin_user:
                    session.execute(text(f'GRANT {role_name} TO "{admin_user}"'))

                # Attempt to create vector extension within the schema, failing gracefully
                try:
                    session.execute(
                        text(f"CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA {schema_name}")
                    )
                    logger.info(f"Enabled 'vector' extension in schema '{schema_name}'.")
                except SQLAlchemyError as vector_error:
                    logger.warning(
                        f"Could not create vector extension in schema '{schema_name}': {vector_error}. "
                        "Vector operations may be affected."
                    )
            logger.info(
                f"Successfully created and configured role '{role_name}' and schema '{schema_name}'."
            )
        except SQLAlchemyError as e:
            logger.error(
                f"Database role/schema creation failed for '{schema_name}': {e}",
                exc_info=True,
            )
            raise ProvisioningError(f"Failed to create database role/schema: {e}") from e

    def _run_organization_migrations(self, schema_name: str, target_version: str = "head"):
        """Runs Alembic migrations for a specific organization's schema."""
        logger.info(f"Running migrations for schema '{schema_name}' to version '{target_version}'.")
        try:
            result = migration_manager.run_organization_migration(
                schema_name=schema_name,
                target_revision=target_version,
                validate_schema=True,
            )
            if not result.success:
                raise ProvisioningError(
                    f"Alembic migration failed for schema '{schema_name}': {result.error}"
                )

            logger.info(
                f"Successfully applied migrations to schema '{schema_name}' in {result.duration:.2f}s."
            )
        except Exception as e:
            logger.error(
                f"Migration execution failed for schema '{schema_name}': {e}",
                exc_info=True,
            )
            raise ProvisioningError(f"An unexpected error occurred during migration: {e}") from e

    def _provision_tenant_database(
        self,
        schema_name: str,
        role_name: str,
        role_password: str,
    ) -> None:
        self._create_database_role_and_schema(schema_name, role_name, role_password)
        self._run_organization_migrations(schema_name)
        self._grant_tenant_runtime_role_access(schema_name, role_name)

    def _grant_tenant_runtime_role_access(
        self,
        schema_name: str,
        schema_owner_role: str,
    ) -> None:
        if not database_config.TENANT_DATABASE_USER:
            return

        config = validate_tenant_runtime_config(
            database_user=database_config.DATABASE_USER,
            tenant_database_user=database_config.TENANT_DATABASE_USER,
            tenant_password=database_config.TENANT_DATABASE_PASSWORD,
            tenant_password_secret_name=database_config.TENANT_DATABASE_PASSWORD_SECRET_NAME,
        )
        self._validate_identifier(schema_name, "schema_name")
        self._validate_identifier(schema_owner_role, "schema_owner_role")
        try:
            with master_session_scope() as session, session.begin():
                TenantRuntimeGrantService(session).grant_tenant_runtime_role_access(
                    schema_name=schema_name,
                    schema_owner_role=schema_owner_role,
                    migration_owner_role=database_config.DATABASE_USER,
                    runtime_role=config.user,
                )
        except SQLAlchemyError as e:
            logger.error(
                "Failed to grant tenant runtime role '%s' access to schema '%s': %s",
                config.user,
                schema_name,
                e,
                exc_info=True,
            )
            raise ProvisioningError(f"Failed to grant tenant runtime role access: {e}") from e

    async def _setup_internal_crm(self, organisation_id: UUID, admin_id: UUID) -> None:
        """Ensure internal CRM integration + default pipeline exist for the organisation."""
        try:
            async for org_session in acquire_org_async_session(organisation_id):
                service = InternalCRMSetupService(org_session, organisation_id, admin_id)
                await service.setup_internal_crm()
                return
        except Exception as exc:
            logger.error(
                "Internal CRM setup failed for org %s: %s",
                organisation_id,
                exc,
                exc_info=True,
            )
            raise ProvisioningError(
                "Internal CRM setup failed",
                organisation_id=organisation_id,
            ) from exc

    def _provision_org_storage_container(self, organisation_id: UUID):
        """Ensure the per-organisation container exists in the shared storage account (idempotent)."""
        account = (
            cloud_storage_config.ORGANISATION_STORAGE_NAME
            or cloud_storage_config.APPLICATION_STORAGE_NAME
        )
        if not account:
            logger.warning(
                "No shared storage account name configured (ORGANISATION_STORAGE_NAME / APPLICATION_STORAGE_NAME). Skipping container creation."
            )
            return
        container = get_org_container_name(organisation_id)
        try:
            credential = self._get_azure_credential()
            service = BlobServiceClient(
                f"https://{account}.blob.core.windows.net", credential=credential
            )
            client = service.get_container_client(container)
            client.create_container()
            logger.info(f"Created container '{container}' in account '{account}'.")
        except Exception as e:
            if "ContainerAlreadyExists" in str(e):
                logger.info(f"Container '{container}' already exists (idempotent).")
            else:
                logger.error(f"Failed to create container '{container}' in '{account}': {e}")
                raise ProvisioningError(f"Container provisioning failed: {e}") from e

    def _store_password_in_key_vault(self, organisation_id: UUID, password: str):
        """
        Stores the database role password in Azure Key Vault.
        This is a non-critical step; failures are logged as warnings.
        """
        role_name = get_role_name(organisation_id)
        secret_name = get_key_vault_secret_name(organisation_id)
        logger.info(
            f"Storing password for role '{role_name}' in Key Vault as secret '{secret_name}'."
        )

        key_vault_url = secret_manager_config.AZURE_KEY_VAULT_URL
        if not key_vault_url:
            logger.warning("AZURE_KEY_VAULT_URL not configured. Skipping Key Vault operation.")
            return

        try:
            credential = self._get_azure_credential()
            client = SecretClient(vault_url=key_vault_url, credential=credential)
            client.set_secret(secret_name, password)
            logger.info(f"Successfully stored secret '{secret_name}' in Azure Key Vault.")
        except Exception as e:
            # Do not block provisioning if Key Vault fails. Log a strong warning.
            logger.warning(
                f"CRITICAL: Failed to store password in Key Vault for org {organisation_id}. "
                f"The password will be lost. Error: {e}",
                exc_info=True,
            )

    def _delete_password_from_key_vault(self, organisation_id: UUID):
        """
        Deletes a password secret from Azure Key Vault.
        This operation is non-critical; failures are logged as warnings.
        """
        secret_name = get_key_vault_secret_name(organisation_id)
        logger.info(f"Attempting to delete secret '{secret_name}' from Azure Key Vault.")

        key_vault_url = secret_manager_config.AZURE_KEY_VAULT_URL
        if not key_vault_url:
            logger.warning("AZURE_KEY_VAULT_URL not configured. Skipping Key Vault deletion.")
            return

        try:
            credential = self._get_azure_credential()
            client = SecretClient(vault_url=key_vault_url, credential=credential)

            poller = client.begin_delete_secret(secret_name)
            deleted_secret = poller.result()

            logger.info(
                f"Successfully deleted secret '{deleted_secret.name}' from Azure Key Vault."
            )
        except Exception as e:
            # This is often not a critical error (e.g., secret already deleted), so log as a warning.
            logger.warning(
                f"Failed to delete secret '{secret_name}' from Key Vault: {e}",
                exc_info=True,
            )

    def _ensure_vector_extension_available(self):
        """Ensures the vector extension is available in the database."""
        try:
            with master_session_scope() as session, session.begin():
                # Check if vector extension is available
                result = session.execute(
                    text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
                ).fetchone()

                if result:
                    # Extension is available, try to create it if not exists
                    session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                    logger.info("Vector extension is available and enabled")
                else:
                    logger.warning("Vector extension is not available in this PostgreSQL instance")
                    logger.warning(
                        "Vector operations may not work. Consider using pgvector/pgvector Docker image"
                    )
        except SQLAlchemyError as e:
            logger.warning(f"Could not check/create vector extension: {e}")
            logger.warning("Continuing without vector extension verification")

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

    async def delete_organization(
        self, session: AsyncSession, organisation_id: UUID, admin_id: UUID
    ) -> Organisation:
        """
        Deletes an organization, its schema, role, and associated data.
        """
        logger.info(f"Deleting organization {organisation_id} by admin {admin_id}")

        organisation = await session.get(Organisation, organisation_id)
        if not organisation:
            raise ValueError(f"Organization with ID {organisation_id} not found.")

        if organisation.status != OrganisationStatus.SUSPENDED:
            raise ValueError(
                f"Organization must be in 'suspended' state to be deleted, but is currently in '{organisation.status.value}' state."
            )

        schema_name = get_schema_name(organisation_id)
        role_name = get_role_name(organisation_id)

        # Log the deletion event before starting the destructive operations
        history_entry = OrganisationActionHistory(
            organisation_id=organisation_id,
            action=OrganisationEventType.DELETED,
            actor_admin_id=admin_id,
            note=f"Organization '{organisation.organisation_name}' permanently deleted.",
            details={"schema_name": schema_name, "role_name": role_name},
        )
        session.add(history_entry)
        await session.commit()

        # Delete the organization record from the master database.
        # Users will be cascade deleted due to the relationship setting.
        await session.delete(organisation)
        await session.commit()

        logger.info(f"Deleted organization record for '{organisation.organisation_name}'")

        # Use the master connection to drop schema and role
        try:
            with master_session_scope() as master_sess, master_sess.begin():
                logger.info(f"Dropping schema '{schema_name}' and role '{role_name}'.")
                master_sess.execute(text(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE"))
                master_sess.execute(text(f"DROP ROLE IF EXISTS {role_name}"))
                logger.info(
                    f"Successfully dropped schema and role for organization {organisation_id}."
                )
        except SQLAlchemyError as e:
            logger.error(
                f"Failed to drop schema/role for organization {organisation_id}: {e}",
                exc_info=True,
            )
            raise DeletionError(
                f"Failed to clean up database resources for organization {organisation_id}."
            ) from e

        # Delete the password from Key Vault
        self._delete_password_from_key_vault(organisation_id)

        logger.info(
            f"Successfully deleted organization {organisation_id} and all associated resources."
        )
        return organisation

    # ------------------ Helper / pagination utilities ------------------ #
    def _get_azure_credential(self):
        """Resolve Azure credential with explicit, deterministic ordering.

        For local development (when ENV=local), it uses DefaultAzureCredential.
        Otherwise, it follows a specific production order.
        """
        # Use DefaultAzureCredential for local development for convenience
        if os.getenv("ENV") == "local":
            logger.info("Auth strategy: DefaultAzureCredential (local development).")
            return DefaultAzureCredential()

        # Production credential order
        tenant_id = cloud_infra_config.AZURE_TENANT_ID
        client_id = cloud_infra_config.AZURE_CLIENT_ID

        # Workload Identity (Kubernetes federated token). Requires tenant & client id.
        if tenant_id and client_id:
            logger.info("Auth strategy: Workload Identity (federated token)")
            allowed_tenants = [
                t.strip()
                for t in (cloud_infra_config.AZURE_ADDITIONALLY_ALLOWED_TENANTS or "").split(",")
                if t.strip()
            ] or None
            return WorkloadIdentityCredential(
                tenant_id=tenant_id,
                client_id=client_id,
                additionally_allowed_tenants=allowed_tenants or ["*"],
            )
        if client_id and not tenant_id:
            logger.warning(
                "Workload identity client id present but AZURE_TENANT_ID missing; using Managed Identity fallback (may cause cross-tenant issues)."
            )

        # 3. Managed Identity (e.g., VM / AzureService / no explicit client id)
        logger.info("Auth strategy: Managed Identity (no client id provided)")
        return ManagedIdentityCredential()

    def _validate_identifier(self, value: str, field: str):
        """Basic safety validation for SQL identifiers comprised of lowercase letters, digits, and underscores.
        Raises ValueError if invalid. This is defensive; names come from controlled generators.
        """
        import re

        if not re.fullmatch(r"[a-z0-9_]+", value):
            raise ValueError(f"Invalid {field} '{value}' – must match [a-z0-9_]+")

    def _quote_identifier(self, value: str) -> str:
        return '"' + value.replace('"', '""') + '"'

    async def rotate_organization_password(
        self, organisation_id: UUID, session: AsyncSession, admin_id: UUID
    ) -> dict[str, Any]:
        """Rotates the database role password and updates Key Vault.
        Returns metadata including new secret name.
        """
        role_name = get_role_name(organisation_id)
        schema_name = get_schema_name(organisation_id)
        new_password = secrets.token_urlsafe(16)
        self._validate_identifier(role_name, "role_name")
        try:
            with master_session_scope() as master_sess, master_sess.begin():
                master_sess.execute(
                    text(f"ALTER ROLE {role_name} WITH PASSWORD :pwd"),
                    {"pwd": new_password},
                )
            self._store_password_in_key_vault(organisation_id, new_password)
            session.add(
                OrganisationActionHistory(
                    organisation_id=organisation_id,
                    action=OrganisationEventType.FIELD_CHANGED,
                    actor_admin_id=admin_id,
                    details={
                        "message": "Database role password rotated",
                        "schema": schema_name,
                    },
                )
            )
            await session.commit()
            return {"role_name": role_name, "schema_name": schema_name}
        except Exception as e:
            await session.rollback()
            logger.error(f"Failed to rotate password for organization {organisation_id}: {e}")
            raise ProvisioningError(f"Failed to rotate password: {e}") from e

    # Override history fetchers to return cursor metadata without breaking original callers
    async def get_organization_history_with_cursor(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        limit: int,
        cursor: str | None = None,
    ) -> dict[str, Any]:
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
        items = await self.get_all_organization_history(session, limit, cursor)
        has_more = len(items) > limit
        page_items = items[:limit]
        next_cursor = (
            encode_cursor(page_items[-1].created_at, page_items[-1].id) if has_more else None
        )
        return {"items": page_items, "next_cursor": next_cursor, "has_more": has_more}


organization_management_service = OrganizationManagementService()
