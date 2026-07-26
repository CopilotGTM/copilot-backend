"""Org-scoped Azure Blob Storage helpers.

The app uses one shared storage account with a dedicated container per
organisation. This module owns container bootstrap, blob path conventions, SAS
generation, and cleanup so callers stay insulated from Azure SDK details.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from collections.abc import AsyncIterable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from azure.identity.aio import (
    DefaultAzureCredential,
    ManagedIdentityCredential,
    WorkloadIdentityCredential,
)
from azure.storage.blob import (
    BlobSasPermissions,
    ContentSettings,
    generate_blob_sas,
)
from azure.storage.blob.aio import BlobServiceClient
from src.infra.config.configs import cloud_infra_config, cloud_storage_config
from src.infra.utils.logger import get_logger
from src.infra.utils.resource_naming import get_org_container_name

logger = get_logger("azure_org_blob_storage")


async def _get_credential_async():
    """Return Azure credential with explicit ordering to avoid unintended fallbacks.

    For local development (when ENV=local), it uses DefaultAzureCredential.
    Otherwise, it follows a specific production order.
    """
    # Use DefaultAzureCredential for local development for convenience
    if os.getenv("ENV") == "local":
        logger.info("Auth strategy (blob): DefaultAzureCredential (local development).")
        return DefaultAzureCredential()

    # Production credential order
    tenant_id = cloud_infra_config.AZURE_TENANT_ID
    client_id = cloud_infra_config.AZURE_CLIENT_ID

    if tenant_id and client_id:
        logger.info("Auth strategy (blob): Workload Identity (federated token)")
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
            "(blob) Workload identity client id present but AZURE_TENANT_ID missing; using Managed Identity fallback (possible wrong tenant)."
        )
    logger.info("Auth strategy (blob): Managed Identity fallback")
    return ManagedIdentityCredential()


class AzureOrganisationBlobStorage:
    """Blob ops constrained to an organisation's container inside a shared storage account.

    Previously we created one storage account per organisation. This was simplified to
    a single (or environment) storage account with one container per organisation to
    reduce Azure resource sprawl and stay within SOC2 isolation via logical scoping.
    """

    def __init__(self, organisation_id: UUID):
        self.organisation_id = organisation_id
        # Shared account name via env var; fallback to deprecated generator for legacy envs.
        self.account_name = cloud_storage_config.ORGANISATION_STORAGE_NAME
        self.container = get_org_container_name(organisation_id)
        self._credential = None
        self._blob_service: BlobServiceClient | None = None
        self._container_ready = False
        self._container_lock = asyncio.Lock()
        self._delegation_key = None
        self._delegation_key_expiry: datetime | None = None
        self._delegation_key_lock = asyncio.Lock()

    async def _init_credential(self):
        if self._credential is None:
            self._credential = await _get_credential_async()

    async def _get_blob_service_client(self) -> BlobServiceClient:
        """Create the async BlobServiceClient once and reuse it for repeated I/O.

        Blob client construction can trigger credential lookups and network-bound
        setup, so caching avoids repeated auth overhead while staying bound to one
        organisation and one container.
        """
        if self._blob_service is None:
            await self._init_credential()
            account_url = f"https://{self.account_name}.blob.core.windows.net"
            self._blob_service = BlobServiceClient(
                account_url=account_url, credential=self._credential
            )
        return self._blob_service

    _SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")

    def _sanitize_filename(self, name: str) -> str:
        base = os.path.basename(name)
        base = base.strip().replace(" ", "_")
        base = self._SAFE_FILENAME.sub("_", base)
        return base or "file"

    async def _ensure_container(self):
        """Create the org container if missing, while tolerating races between workers.

        Concurrent starts can both attempt creation, so `ContainerAlreadyExists` is
        treated as a no-op for idempotency.
        """
        if self._container_ready:
            return
        async with self._container_lock:
            if self._container_ready:
                return
            bs = await self._get_blob_service_client()
            container_client = bs.get_container_client(self.container)
            try:
                await container_client.create_container()
                logger.info("Created container %s in account %s", self.container, self.account_name)
                self._container_ready = True
            except Exception as e:
                # Ignore if already exists; Azure error code check kept simple.
                if "ContainerAlreadyExists" in str(e):
                    self._container_ready = True
                else:
                    logger.debug("Container create skipped/failed: %s", e)
                    # copilotgtm: container bootstrap stays best-effort; retry on a
                    # new storage instance instead of once per generated file URL.
                    self._container_ready = True

    async def _get_user_delegation_key(self, minutes: int):
        """Reuse one delegation key while it safely covers the requested SAS lifetime."""
        refresh_buffer = timedelta(minutes=5)
        required_expiry = datetime.now(UTC) + timedelta(minutes=minutes) + refresh_buffer
        if (
            self._delegation_key is not None
            and self._delegation_key_expiry is not None
            and self._delegation_key_expiry >= required_expiry
        ):
            return self._delegation_key

        async with self._delegation_key_lock:
            required_expiry = datetime.now(UTC) + timedelta(minutes=minutes) + refresh_buffer
            if (
                self._delegation_key is not None
                and self._delegation_key_expiry is not None
                and self._delegation_key_expiry >= required_expiry
            ):
                return self._delegation_key

            blob_service = await self._get_blob_service_client()
            start = datetime.now(UTC) - timedelta(minutes=5)
            key_expiry = datetime.now(UTC) + timedelta(minutes=max(60, minutes + 10))
            self._delegation_key = await blob_service.get_user_delegation_key(start, key_expiry)
            self._delegation_key_expiry = key_expiry
            return self._delegation_key

    async def generate_read_sas(
        self,
        blob_path: str,
        minutes: int = 30,
        download_filename: str | None = None,
        content_type: str | None = None,
    ) -> str:
        """Generate a short-lived read SAS URL for one blob.

        Read URLs expire on purpose so download links are never long-lived shared
        secrets. Optional `download_filename` sets a safe `Content-Disposition`
        name for browser behavior. Optional `content_type` overrides the response
        content type without rewriting blob metadata.
        """
        await self._ensure_container()
        container = self.container
        expiry = datetime.now(UTC) + timedelta(minutes=minutes)
        delegation_key = await self._get_user_delegation_key(minutes)

        content_disposition = None
        if download_filename:
            # Ensure the filename is properly formatted for the header.
            # Basic sanitization to prevent header injection.
            safe_download_name = self._sanitize_filename(download_filename)
            content_disposition = f'attachment; filename="{safe_download_name}"'

        sas = generate_blob_sas(
            account_name=self.account_name,
            container_name=container,
            blob_name=blob_path,
            user_delegation_key=delegation_key,
            permission=BlobSasPermissions(read=True),
            expiry=expiry,
            content_disposition=content_disposition,
            content_type=content_type,
        )
        return f"https://{self.account_name}.blob.core.windows.net/{container}/{blob_path}?{sas}"

    async def generate_write_sas(self, blob_path: str, minutes: int = 15) -> str:
        """Generate a short-lived write SAS URL for direct uploads."""
        await self._ensure_container()
        container = self.container
        expiry = datetime.now(UTC) + timedelta(minutes=minutes)
        delegation_key = await self._get_user_delegation_key(minutes)
        sas = generate_blob_sas(
            account_name=self.account_name,
            container_name=container,
            blob_name=blob_path,
            user_delegation_key=delegation_key,
            permission=BlobSasPermissions(create=True, write=True),
            expiry=expiry,
        )
        return f"https://{self.account_name}.blob.core.windows.net/{container}/{blob_path}?{sas}"

    def plan_blob_path(self, *, user_id: UUID, attachment_id: UUID, original_filename: str) -> str:
        """Build a deterministic chat upload path using DB-scoped IDs.

        Paths are stable for the given IDs so support can map storage bytes back
        to source rows during incident investigation.
        """
        safe_name = self._sanitize_filename(original_filename)
        return f"chat_uploads/{user_id}/{attachment_id}/{safe_name}"

    def plan_crm_import_path(
        self, *, user_id: UUID, import_id: UUID, original_filename: str
    ) -> str:
        safe_name = self._sanitize_filename(original_filename)
        return f"crm_imports/{user_id}/{import_id}/{safe_name}"

    def thumbnail_blob_path(self, original_blob_path: str, variant: str) -> str:
        """Return a deterministic thumbnail path inside a dedicated thumbnails/ folder.

        New structure (v2):
            original: knowledgebase/<user>/<source>/file.pdf -> knowledgebase/<user>/<source>/thumbnails/<variant>.jpg
            original: chat_uploads/<user>/<att>/image.png   -> chat_uploads/<user>/<att>/thumbnails/<variant>.jpg

        Rationale:
            * Keeps derivatives grouped & easy to list/purge.
            * Avoids cluttering directory with many size suffixes.
            * Future-proofs for additional variants without renaming originals.

        Backwards compatibility: If the original already looks like an old-style
        thumbnail path (contains '_thumb_' and endswith .jpg) we just return it.
        """
        if not original_blob_path:
            return f"thumbnails/{variant}.jpg"
        if "_thumb_" in original_blob_path and original_blob_path.endswith(".jpg"):
            return original_blob_path  # already a legacy thumbnail style
        # Keep thumbnails next to source files for easy cleanup and retention control.
        if "/" in original_blob_path:
            dir_part, _filename = original_blob_path.rsplit("/", 1)
        else:
            dir_part, _filename = "", original_blob_path
        return f"{dir_part}/thumbnails/{variant}.jpg" if dir_part else f"thumbnails/{variant}.jpg"

    def plan_thumbnail_path_for_source(
        self, *, user_id: UUID, source_id: UUID, original_filename: str, variant: str
    ) -> str:
        """Plan a v2 thumbnail path for knowledge sources (thumbnails folder).

        Example:
            original file planned at: knowledgebase/<user>/<source>/<file>
            thumbnail path:           knowledgebase/<user>/<source>/thumbnails/<variant>.jpg
        """
        # We ignore the original filename beyond ensuring directory structure consistency.
        return f"knowledgebase/{user_id}/{source_id}/thumbnails/{variant}.jpg"

    async def get_blob_properties(self, blob_path: str):
        """Return blob metadata if present, otherwise `None`."""
        await self._ensure_container()
        blob_service = await self._get_blob_service_client()
        container = self.container
        blob_client = blob_service.get_blob_client(container=container, blob=blob_path)
        try:
            props = await blob_client.get_blob_properties()
            return {
                "size": props.size,
                "content_type": (
                    getattr(props, "content_settings", None).content_type
                    if getattr(props, "content_settings", None)
                    else None
                ),
                "last_modified": props.last_modified,
            }
        except Exception:
            # Azure SDK raises ResourceNotFoundError, but a broad catch is fine to confirm non-existence.
            return None

    async def get_blob_content(self, blob_path: str) -> bytes:
        """Download full blob content.

        This is intentionally eager because callers need raw bytes for hashing and
        downstream checks, not streaming pipelines.
        """
        await self._ensure_container()
        blob_service = await self._get_blob_service_client()
        container = self.container
        blob_client = blob_service.get_blob_client(container=container, blob=blob_path)
        try:
            download_stream = await blob_client.download_blob()
            content = await download_stream.readall()
            logger.debug(f"Downloaded blob {blob_path} ({len(content)} bytes)")
            return content
        except Exception as e:
            logger.error(f"Failed to download blob {blob_path}: {str(e)}")
            raise

    async def upload_bytes(
        self,
        blob_path: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> None:
        """Upload bytes to a blob path using the resolved org container.

        Centralizing storage writes here keeps service code from depending on
        Azure object-shape details or retry strategy.
        """

        await self._ensure_container()
        blob_service = await self._get_blob_service_client()
        container = self.container
        blob_client = blob_service.get_blob_client(container=container, blob=blob_path)
        await blob_client.upload_blob(
            data,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
        )

    async def upload_stream(
        self,
        blob_path: str,
        data: AsyncIterable[bytes],
        *,
        content_type: str = "application/octet-stream",
    ) -> None:
        """Upload a byte stream without buffering the full payload in memory."""

        await self._ensure_container()
        blob_service = await self._get_blob_service_client()
        container = self.container
        blob_client = blob_service.get_blob_client(container=container, blob=blob_path)
        await blob_client.upload_blob(
            data,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
        )

    async def delete_blob(self, blob_path: str):
        """Deletes a blob if it exists."""
        try:
            await self._ensure_container()
            blob_service = await self._get_blob_service_client()
            container = self.container
            blob_client = blob_service.get_blob_client(container=container, blob=blob_path)
            await blob_client.delete_blob(delete_snapshots="include")
            logger.info("Deleted blob: %s/%s", container, blob_path)
        except Exception as e:
            # This is not a critical error if the blob is already gone.
            logger.warning("Failed to delete blob %s/%s: %s", container, blob_path, e)

    async def compute_checksum(self, blob_path: str, algorithm: str = "sha256") -> str:
        """Stream and compute checksum for a blob without loading it fully into memory."""

        await self._ensure_container()
        blob_service = await self._get_blob_service_client()
        container = self.container
        blob_client = blob_service.get_blob_client(container=container, blob=blob_path)
        hasher = hashlib.new(algorithm)
        downloader = await blob_client.download_blob()
        async for chunk in downloader.chunks():  # type: ignore[attr-defined]
            hasher.update(chunk)
        return hasher.hexdigest()

    async def close(self):
        """Closes the underlying BlobServiceClient and credential."""
        if self._blob_service:
            await self._blob_service.close()
            self._blob_service = None

        # Some DefaultAzureCredential implementations expose an async close; support both.
        if self._credential and hasattr(self._credential, "close"):
            try:
                maybe = self._credential.close()
                if asyncio.iscoroutine(maybe):  # type: ignore[arg-type]
                    await maybe
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("Error closing credential: %s", e)
        self._credential = None
        self._container_ready = False
        self._delegation_key = None
        self._delegation_key_expiry = None

    async def __aenter__(self):
        """Enables use as an async context manager."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Ensures resources are cleaned up when exiting a context."""
        await self.close()
