from __future__ import annotations
"""Azure per-organization blob storage helper for chat attachments."""
from datetime import datetime, timedelta
from typing import Optional, AsyncGenerator
from uuid import UUID, uuid4
import re
import os
import asyncio

from azure.identity.aio import (
    ClientSecretCredential,
    WorkloadIdentityCredential,
    ManagedIdentityCredential,
)
from azure.storage.blob.aio import BlobServiceClient
from azure.storage.blob import (
    ContentSettings,
    generate_blob_sas,
    BlobSasPermissions,
)

from src.infra.utils.logger import get_logger
from src.infra.config.configs import cloud_infra_config, cloud_storage_config
from src.infra.utils.resource_naming import get_org_container_name

logger = get_logger("azure_org_blob_storage")


async def _get_credential_async():
    """Return Azure credential with explicit ordering to avoid unintended fallbacks.

    Order:
      1. Service Principal (client secret) when tenant, client, secret present
      2. Workload Identity (federated token) when tenant & client present
      3. Managed Identity (no explicit ids) as final fallback
    """
    tenant_id = cloud_infra_config.AZURE_TENANT_ID
    client_id = cloud_infra_config.AZURE_CLIENT_ID
    client_secret = cloud_infra_config.AZURE_CLIENT_SECRET

    if tenant_id and client_id and client_secret:
        logger.info("Auth strategy (blob): Service Principal (client secret)")
        return ClientSecretCredential(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )
    if tenant_id and client_id:
        logger.info("Auth strategy (blob): Workload Identity (federated token)")
        return WorkloadIdentityCredential(
            tenant_id=tenant_id,
            client_id=client_id,
        )
    if client_id and not tenant_id:
        logger.warning("(blob) Workload identity client id present but AZURE_TENANT_ID missing; using Managed Identity fallback (possible wrong tenant).")
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
        self._blob_service: Optional[BlobServiceClient] = None

    async def _init_credential(self):
        if self._credential is None:
            self._credential = await _get_credential_async()

    async def _get_blob_service_client(self) -> BlobServiceClient:
        """Lazily initializes and returns the async BlobServiceClient."""
        if self._blob_service is None:
            await self._init_credential()
            account_url = f"https://{self.account_name}.blob.core.windows.net"
            self._blob_service = BlobServiceClient(account_url=account_url, credential=self._credential)
        return self._blob_service

    _SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")

    def _sanitize_filename(self, name: str) -> str:
        base = os.path.basename(name)
        base = base.strip().replace(" ", "_")
        base = self._SAFE_FILENAME.sub("_", base)
        return base or "file"

    async def _ensure_container(self):
        """Create the organisation container if it doesn't already exist (idempotent)."""
        bs = await self._get_blob_service_client()
        container_client = bs.get_container_client(self.container)
        try:
            await container_client.create_container()
            logger.info("Created container %s in account %s", self.container, self.account_name)
        except Exception as e:
            # Ignore if already exists; Azure error code check kept simple.
            if "ContainerAlreadyExists" not in str(e):
                logger.debug("Container create skipped/failed: %s", e)

    async def generate_read_sas(
        self, blob_path: str, minutes: int = 120, download_filename: str | None = None
    ) -> str:
        """
        Generates a read-access SAS URL.

        If `download_filename` is provided, it sets the Content-Disposition header
        to ensure the user's browser downloads the file with the original, friendly name.
        """
        await self._ensure_container()
        blob_service = await self._get_blob_service_client()
        container = self.container
        start = datetime.utcnow() - timedelta(minutes=5)
        expiry = datetime.utcnow() + timedelta(minutes=minutes)
        delegation_key = await blob_service.get_user_delegation_key(start, expiry)

        content_disposition = None
        if download_filename:
            # Ensure the filename is properly formatted for the header.
            # Basic sanitization to prevent header injection.
            safe_download_name = download_filename.replace('"', "'")
            content_disposition = f'attachment; filename="{safe_download_name}"'

        sas = generate_blob_sas(
            account_name=self.account_name,
            container_name=container,
            blob_name=blob_path,
            user_delegation_key=delegation_key,
            permission=BlobSasPermissions(read=True),
            expiry=expiry,
            content_disposition=content_disposition,
        )
        return f"https://{self.account_name}.blob.core.windows.net/{container}/{blob_path}?{sas}"

    async def generate_write_sas(self, blob_path: str, minutes: int = 15) -> str:
        """Generate a short‑lived SAS URL for client direct upload."""
        await self._ensure_container()
        blob_service = await self._get_blob_service_client()
        container = self.container
        start = datetime.utcnow() - timedelta(minutes=5)
        expiry = datetime.utcnow() + timedelta(minutes=minutes)
        delegation_key = await blob_service.get_user_delegation_key(start, expiry)
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
        """
        Plans a structured, secure, and organized blob path based on database IDs.

        The structure `user_id/attachment_id/sanitized_filename.ext` is chosen for:
        - **Direct DB Correlation**: The path can be reconstructed directly from the DB record.
        - **Uniqueness**: The attachment_id guarantees a unique path.
        - **Auditing**: The user_id provides clear ownership context.
        """
        safe_name = self._sanitize_filename(original_filename)
        return f"chat_uploads/{user_id}/{attachment_id}/{safe_name}"


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
                # Split directory and filename
                if "/" in original_blob_path:
                        dir_part, _filename = original_blob_path.rsplit("/", 1)
                else:
                        dir_part, _filename = "", original_blob_path
                return f"{dir_part}/thumbnails/{variant}.jpg" if dir_part else f"thumbnails/{variant}.jpg"

    def plan_thumbnail_path_for_source(self, *, user_id: UUID, source_id: UUID, original_filename: str, variant: str) -> str:
                """Plan a v2 thumbnail path for knowledge sources (thumbnails folder).

                Example:
                    original file planned at: knowledgebase/<user>/<source>/<file>
                    thumbnail path:           knowledgebase/<user>/<source>/thumbnails/<variant>.jpg
                """
                # We ignore the original filename beyond ensuring directory structure consistency.
                return f"knowledgebase/{user_id}/{source_id}/thumbnails/{variant}.jpg"


    async def get_blob_properties(self, blob_path: str):
        """Return blob properties (exists check, size, content type) or None if missing."""
        await self._ensure_container()
        blob_service = await self._get_blob_service_client()
        container = self.container
        blob_client = blob_service.get_blob_client(container=container, blob=blob_path)
        try:
            props = await blob_client.get_blob_properties()
            return {
                "size": props.size,
                "content_type": getattr(props, "content_settings", None).content_type if getattr(props, "content_settings", None) else None,
                "last_modified": props.last_modified,
            }
        except Exception:
            # Azure SDK raises ResourceNotFoundError, but a broad catch is fine to confirm non-existence.
            return None

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

    async def __aenter__(self):
        """Enables use as an async context manager."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Ensures resources are cleaned up when exiting a context."""
        await self.close()
