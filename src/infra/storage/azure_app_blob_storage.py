from __future__ import annotations

# Azure application-level blob storage helper.
import asyncio
from datetime import UTC, datetime, timedelta

from azure.storage.blob import BlobSasPermissions, ContentSettings, generate_blob_sas
from azure.storage.blob.aio import BlobServiceClient
from src.infra.config.configs import cloud_storage_config
from src.infra.storage.azure_org_blob_storage import _get_credential_async
from src.infra.utils.logger import get_logger
from src.infra.utils.resource_naming import get_application_container_name

logger = get_logger("azure_app_blob_storage")


class AzureApplicationBlobStorage:
    # Blob ops constrained to the application (master) container.

    def __init__(self, container_name: str | None = None):
        account_name = cloud_storage_config.APPLICATION_STORAGE_NAME
        if not account_name:
            raise ValueError("APPLICATION_STORAGE_NAME is not configured")
        self.account_name = account_name
        self.container = container_name or get_application_container_name()
        self._credential = None
        self._blob_service: BlobServiceClient | None = None

    async def _init_credential(self):
        if self._credential is None:
            self._credential = await _get_credential_async()

    async def _get_blob_service_client(self) -> BlobServiceClient:
        if self._blob_service is None:
            await self._init_credential()
            account_url = f"https://{self.account_name}.blob.core.windows.net"
            self._blob_service = BlobServiceClient(
                account_url=account_url, credential=self._credential
            )
        return self._blob_service

    async def _ensure_container(self):
        bs = await self._get_blob_service_client()
        container_client = bs.get_container_client(self.container)
        try:
            await container_client.create_container()
            logger.info("Created container %s in account %s", self.container, self.account_name)
        except Exception as e:
            if "ContainerAlreadyExists" not in str(e):
                logger.debug("Container create skipped/failed: %s", e)

    async def generate_read_sas(
        self, blob_path: str, minutes: int = 30, download_filename: str | None = None
    ) -> str:
        await self._ensure_container()
        blob_service = await self._get_blob_service_client()
        start = datetime.now(UTC) - timedelta(minutes=5)
        expiry = datetime.now(UTC) + timedelta(minutes=minutes)
        delegation_key = await blob_service.get_user_delegation_key(start, expiry)

        content_disposition = None
        if download_filename:
            safe_download_name = download_filename.replace('"', "'")
            content_disposition = f'attachment; filename="{safe_download_name}"'

        sas = generate_blob_sas(
            account_name=self.account_name,
            container_name=self.container,
            blob_name=blob_path,
            user_delegation_key=delegation_key,
            permission=BlobSasPermissions(read=True),
            expiry=expiry,
            content_disposition=content_disposition,
        )
        return (
            f"https://{self.account_name}.blob.core.windows.net/{self.container}/{blob_path}?{sas}"
        )

    async def generate_write_sas(self, blob_path: str, minutes: int = 15) -> str:
        await self._ensure_container()
        blob_service = await self._get_blob_service_client()
        start = datetime.now(UTC) - timedelta(minutes=5)
        expiry = datetime.now(UTC) + timedelta(minutes=minutes)
        delegation_key = await blob_service.get_user_delegation_key(start, expiry)
        sas = generate_blob_sas(
            account_name=self.account_name,
            container_name=self.container,
            blob_name=blob_path,
            user_delegation_key=delegation_key,
            permission=BlobSasPermissions(create=True, write=True),
            expiry=expiry,
        )
        return (
            f"https://{self.account_name}.blob.core.windows.net/{self.container}/{blob_path}?{sas}"
        )

    async def get_blob_properties(self, blob_path: str):
        await self._ensure_container()
        blob_service = await self._get_blob_service_client()
        blob_client = blob_service.get_blob_client(container=self.container, blob=blob_path)
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
            return None

    async def upload_bytes(
        self, blob_path: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> None:
        await self._ensure_container()
        blob_service = await self._get_blob_service_client()
        blob_client = blob_service.get_blob_client(container=self.container, blob=blob_path)
        await blob_client.upload_blob(
            data,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
        )

    async def close(self):
        if self._blob_service:
            await self._blob_service.close()
            self._blob_service = None
        if self._credential and hasattr(self._credential, "close"):
            try:
                maybe = self._credential.close()
                if asyncio.iscoroutine(maybe):
                    await maybe
            except Exception:
                logger.debug("Error closing credential", exc_info=True)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
