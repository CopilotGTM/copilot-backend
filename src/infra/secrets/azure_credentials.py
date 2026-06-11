from __future__ import annotations

import os

from azure.identity import (
    DefaultAzureCredential,
    ManagedIdentityCredential,
    WorkloadIdentityCredential,
)
from src.infra.config.configs import cloud_infra_config
from src.infra.utils.logger import get_logger

logger = get_logger("azure_credentials")


def _allowed_tenants() -> list[str]:
    return [
        tenant.strip()
        for tenant in (cloud_infra_config.AZURE_ADDITIONALLY_ALLOWED_TENANTS or "").split(",")
        if tenant.strip()
    ] or ["*"]


def build_sync_azure_credential(*, prefer_default_credential: bool = False):
    if prefer_default_credential or os.getenv("ENV") == "local":
        logger.info("Auth strategy: DefaultAzureCredential (local development).")
        return DefaultAzureCredential(
            exclude_interactive_browser_credential=True,
            exclude_visual_studio_code_credential=False,
        )

    tenant_id = cloud_infra_config.AZURE_TENANT_ID
    client_id = cloud_infra_config.AZURE_CLIENT_ID
    if tenant_id and client_id:
        logger.info("Auth strategy: Workload Identity (federated token)")
        return WorkloadIdentityCredential(
            tenant_id=tenant_id,
            client_id=client_id,
            additionally_allowed_tenants=_allowed_tenants(),
        )

    if client_id and not tenant_id:
        logger.warning(
            "AZURE_CLIENT_ID is set without AZURE_TENANT_ID; using Managed Identity fallback."
        )

    logger.info("Auth strategy: Managed Identity")
    return ManagedIdentityCredential()
