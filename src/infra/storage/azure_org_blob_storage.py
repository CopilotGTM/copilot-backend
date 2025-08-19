from __future__ import annotations
"""Azure per-organization blob storage helper for chat attachments."""
from datetime import datetime, timedelta
from typing import Optional, AsyncGenerator
from uuid import UUID, uuid4
import re
import os
import asyncio

from azure.identity.aio import DefaultAzureCredential, ClientSecretCredential
from azure.storage.blob.aio import BlobServiceClient
from azure.storage.blob import (
    ContentSettings,
    generate_blob_sas,
    BlobSasPermissions,
)

from src.infra.utils.logger import get_logger
from src.infra.config.configs import cloud_infra_config
from src.infra.utils.resource_naming import get_storage_account_name

logger = get_logger("azure_org_blob_storage")


async def _get_credential_async():
    """
    Returns the default Azure credential.

    This relies on `DefaultAzureCredential`, which is the standard for modern Azure
    applications. It automatically handles Managed Identity in production environments
    (e.g., App Service, VMs) and local developer authentication (Azure CLI, VS Code)
    without needing to manage secrets in the code.
    """
    logger.info("Authenticating with Azure using DefaultAzureCredential (Managed Identity or local developer).")
    return DefaultAzureCredential()


class AzureOrganisationBlobStorage:
    """Blob ops constrained to an organisation's storage account."""

    def __init__(self, organisation_id: UUID):
        self.organisation_id = organisation_id
        self.account_name = get_storage_account_name(organisation_id)
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

    async def generate_read_sas(
        self, container: str, blob_path: str, minutes: int = 120, download_filename: str | None = None
    ) -> str:
        """
        Generates a read-access SAS URL.

        If `download_filename` is provided, it sets the Content-Disposition header
        to ensure the user's browser downloads the file with the original, friendly name.
        """
        blob_service = await self._get_blob_service_client()
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

    async def generate_write_sas(self, container: str, blob_path: str, minutes: int = 15) -> str:
        """Generate a short‑lived SAS URL for client direct upload."""
        blob_service = await self._get_blob_service_client()
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


    async def get_blob_properties(self, container: str, blob_path: str):
        """Return blob properties (exists check, size, content type) or None if missing."""
        blob_service = await self._get_blob_service_client()
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

    async def delete_blob(self, container: str, blob_path: str):
        """Deletes a blob if it exists."""
        try:
            blob_service = await self._get_blob_service_client()
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
        
        if self._credential and hasattr(self._credential, "close"):
            self._credential.close()

    async def __aenter__(self):
        """Enables use as an async context manager."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Ensures resources are cleaned up when exiting a context."""
        await self.close()
