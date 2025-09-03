from __future__ import annotations
import os
import asyncio
from typing import Optional, Iterable, Dict
from azure.keyvault.secrets.aio import SecretClient
from azure.identity.aio import DefaultAzureCredential, WorkloadIdentityCredential, ManagedIdentityCredential
from .interface import SecretProvider
from src.infra.config.configs import cloud_infra_config, secret_manager_config
from src.infra.utils.logger import get_logger

logger = get_logger("azure_key_vault_provider")


class AzureKeyVaultSecretProvider(SecretProvider):
    """Async Azure Key Vault implementation of :class:`SecretProvider`.

    Responsibilities
    ---------------
    * Establish an authenticated `SecretClient` using an ordered credential strategy.
    * Fetch secrets (`get`) and simple bulk retrieval (`get_many`).
    * Translate authentication configuration into appropriate identity objects.

    Credential Order
    ----------------
    1. Local dev: `DefaultAzureCredential` (enables AZ CLI / VS Code / env vars).
    2. Workload Identity (if `AZURE_TENANT_ID` + `AZURE_CLIENT_ID` present) – for
       Kubernetes / federated identity.
    3. Managed Identity fallback (VM / App Service / Container Apps system MI).

    NOTE: Error handling is intentionally lightweight here; higher layers (manager)
    can add retries/backoff & metrics.
    """

    def __init__(self):
        self._client: Optional[SecretClient] = None
        self._credential = None
        self._lock = asyncio.Lock()

    async def _ensure_client(self):
        """Create the SecretClient once (lazy init)."""
        if self._client is not None:
            return
        async with self._lock:
            if self._client is not None:
                return
            key_vault_url = secret_manager_config.AZURE_KEY_VAULT_URL
            if not key_vault_url:
                raise RuntimeError("AZURE_KEY_VAULT_URL not configured")
            if os.getenv("ENV") == "local":
                logger.info("Auth strategy (secrets): DefaultAzureCredential (local)")
                self._credential = DefaultAzureCredential()
            else:
                tenant_id = cloud_infra_config.AZURE_TENANT_ID
                client_id = cloud_infra_config.AZURE_CLIENT_ID
                if tenant_id and client_id:
                    logger.info("Auth strategy (secrets): Workload Identity")
                    allowed_tenants = [t.strip() for t in (cloud_infra_config.AZURE_ADDITIONALLY_ALLOWED_TENANTS or "").split(',') if t.strip()] or None
                    self._credential = WorkloadIdentityCredential(
                        tenant_id=tenant_id,
                        client_id=client_id,
                        additionally_allowed_tenants=allowed_tenants or ["*"]
                    )
                else:
                    logger.info("Auth strategy (secrets): Managed Identity fallback")
                    self._credential = ManagedIdentityCredential()
            self._client = SecretClient(vault_url=key_vault_url, credential=self._credential)

    async def get(self, name: str) -> Optional[str]:
        """Retrieve a single secret value or None on failure.

        Returning None instead of raising for transient conditions keeps the
        provider thin. Upstream logic can differentiate behaviours if/when we
        add richer exception mapping.
        """
        await self._ensure_client()
        try:
            secret = await self._client.get_secret(name)  # type: ignore
            return secret.value
        except Exception as e:  # pragma: no cover - network dependent
            logger.warning("Secret fetch failed for %s: %s", name, e)
            return None

    async def get_many(self, names: Iterable[str]) -> Dict[str, Optional[str]]:
        """Naive sequential multi-get.

        Could be parallelised (gather) but Key Vault may rate limit; sequential
        keeps behaviour predictable. Callers needing performance can shard.
        """
        results: Dict[str, Optional[str]] = {}
        for n in names:
            results[n] = await self.get(n)
        return results

    async def close(self):
        """Release underlying network resources / credential handles."""
        if self._client:
            try:
                await self._client.close()  # type: ignore
            except Exception:  # pragma: no cover
                pass
        if self._credential and hasattr(self._credential, "close"):
            try:
                await self._credential.close()  # type: ignore
            except Exception:  # pragma: no cover
                pass
