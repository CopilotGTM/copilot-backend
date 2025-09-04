from __future__ import annotations
import os
import asyncio
import time
from typing import Optional, Iterable, Dict
from azure.keyvault.secrets.aio import SecretClient
from azure.identity.aio import DefaultAzureCredential, WorkloadIdentityCredential, ManagedIdentityCredential
from azure.core.exceptions import (
    ServiceRequestError, 
    ClientAuthenticationError, 
    ResourceNotFoundError,
    HttpResponseError
)
from .interface import SecretProvider
from src.infra.config.configs import cloud_infra_config, secret_manager_config
from src.infra.utils.logger import get_logger

logger = get_logger("azure_key_vault_provider")


class AzureKeyVaultSecretProvider(SecretProvider):
    """Production-ready Azure Key Vault implementation of :class:`SecretProvider`.

    Responsibilities
    ---------------
    * Establish an authenticated `SecretClient` using an ordered credential strategy.
    * Fetch secrets (`get`) and simple bulk retrieval (`get_many`).
    * Translate authentication configuration into appropriate identity objects.
    * Handle transient failures with appropriate error categorization.
    * Automatic credential refresh and client recreation on auth failures.

    Credential Order
    ----------------
    1. Local dev: `DefaultAzureCredential` (enables AZ CLI / VS Code / env vars).
    2. Workload Identity (if `AZURE_TENANT_ID` + `AZURE_CLIENT_ID` present) – for
       Kubernetes / federated identity.
    3. Managed Identity fallback (VM / App Service / Container Apps system MI).

    Error Handling
    --------------
    * Distinguishes between transient (network) and permanent (auth/not found) errors
    * Automatic client recreation on authentication failures
    * Detailed logging for production debugging
    """

    def __init__(self):
        self._client: Optional[SecretClient] = None
        self._credential = None
        self._lock = asyncio.Lock()
        self._last_auth_failure: Optional[float] = None
        self._auth_failure_count = 0
        self._client_creation_time: Optional[float] = None

    async def _ensure_client(self):
        """Create the SecretClient once (lazy init) with automatic recreation on auth failures."""
        if self._client is not None:
            # Check if we need to recreate client due to auth failures
            if (self._last_auth_failure and 
                time.time() - self._last_auth_failure < 300):  # 5 minutes
                logger.info("Recreating Azure Key Vault client due to recent auth failure")
                await self._recreate_client()
            else:
                return

        async with self._lock:
            if (self._client is not None and 
                (not self._last_auth_failure or 
                 time.time() - self._last_auth_failure >= 300)):
                return
            
            await self._create_client()

    async def _create_client(self):
        """Create a new SecretClient with appropriate credential strategy."""
        key_vault_url = secret_manager_config.AZURE_KEY_VAULT_URL
        if not key_vault_url:
            raise RuntimeError("AZURE_KEY_VAULT_URL not configured")

        try:
            # Close existing client if any
            if self._client:
                try:
                    await self._client.close()
                except Exception:
                    pass

            # Create new credential based on environment
            if os.getenv("ENV") == "local":
                logger.info("Auth strategy (secrets): DefaultAzureCredential (local)")
                self._credential = DefaultAzureCredential(
                    exclude_interactive_browser_credential=True,
                    exclude_visual_studio_code_credential=False
                )
            else:
                tenant_id = cloud_infra_config.AZURE_TENANT_ID
                client_id = cloud_infra_config.AZURE_CLIENT_ID
                if tenant_id and client_id:
                    logger.info("Auth strategy (secrets): Workload Identity")
                    allowed_tenants = [
                        t.strip() 
                        for t in (cloud_infra_config.AZURE_ADDITIONALLY_ALLOWED_TENANTS or "").split(',') 
                        if t.strip()
                    ] or None
                    self._credential = WorkloadIdentityCredential(
                        tenant_id=tenant_id,
                        client_id=client_id,
                        additionally_allowed_tenants=allowed_tenants or ["*"]
                    )
                else:
                    logger.info("Auth strategy (secrets): Managed Identity fallback")
                    self._credential = ManagedIdentityCredential()

            # Create new client
            self._client = SecretClient(vault_url=key_vault_url, credential=self._credential)
            self._client_creation_time = time.time()
            self._auth_failure_count = 0
            logger.info(f"Azure Key Vault client created successfully for: {key_vault_url}")
            
        except Exception as e:
            logger.error(f"Failed to create Azure Key Vault client: {e}")
            self._client = None
            self._credential = None
            raise

    async def _recreate_client(self):
        """Force recreation of the client (used after auth failures)."""
        logger.info("Forcing recreation of Azure Key Vault client")
        self._client = None
        self._credential = None
        await self._create_client()

    def _categorize_exception(self, exception: Exception) -> tuple[str, bool]:
        """Categorize exception and determine if it's retryable.
        
        Returns:
            tuple[str, bool]: (category, is_retryable)
        """
        if isinstance(exception, ResourceNotFoundError):
            return "not_found", False
        elif isinstance(exception, ClientAuthenticationError):
            return "auth_error", False  # Don't retry auth errors immediately
        elif isinstance(exception, asyncio.TimeoutError):
            return "timeout", True
        elif isinstance(exception, ServiceRequestError):
            return "network_error", True
        elif isinstance(exception, HttpResponseError):
            if exception.status_code in [429, 503, 504]:  # Rate limit, service unavailable, timeout
                return "rate_limit", True
            elif exception.status_code in [401, 403]:
                return "auth_error", False
            else:
                return "http_error", False
        elif isinstance(exception, RuntimeError):
            error_msg = str(exception).lower()
            if "event loop is closed" in error_msg or "no running event loop" in error_msg:
                return "event_loop_error", False  # Don't retry immediately, needs client recreation
            else:
                return "runtime_error", True
        else:
            return "unknown_error", True

    async def get(self, name: str) -> Optional[str]:
        """Retrieve a single secret value with enhanced error handling and categorization.

        Returns None for permanent errors (not found, auth) and raises for transient
        errors that should be retried by the SecretsManager.
        """
        try:
            await self._ensure_client()
        except Exception as e:
            logger.error(f"Failed to ensure Azure Key Vault client: {e}")
            return None
        
        if not self._client:
            logger.error("Azure Key Vault client not available")
            return None

        try:
            logger.debug(f"Fetching secret '{name}' from Azure Key Vault")
            # Bound the network call to prevent indefinite stalls
            secret = await asyncio.wait_for(
                self._client.get_secret(name),
                timeout=max(1, int(secret_manager_config.SECRET_GET_TIMEOUT_SEC)),
            )
            logger.debug(f"Successfully retrieved secret '{name}'")
            return secret.value
            
        except RuntimeError as e:
            if "Event loop is closed" in str(e) or "no running event loop" in str(e):
                logger.error(f"Event loop error for secret '{name}': {e}")
                # Force client recreation for next attempt
                self._client = None
                return None
            else:
                # Re-raise other RuntimeErrors
                raise
        except Exception as e:
            error_category, is_retryable = self._categorize_exception(e)
            
            if error_category == "not_found":
                logger.warning(f"Secret '{name}' not found in Azure Key Vault")
                return None
            elif error_category == "auth_error":
                self._last_auth_failure = time.time()
                self._auth_failure_count += 1
                logger.error(f"Authentication error for secret '{name}': {e}")
                
                # If we have multiple auth failures, force client recreation
                if self._auth_failure_count >= 2:
                    logger.warning("Multiple auth failures detected, will recreate client on next request")
                    self._client = None
                
                return None
            elif error_category in ["network_error", "rate_limit", "timeout"]:
                logger.warning(f"Transient error fetching secret '{name}' ({error_category}): {e}")
                # Re-raise transient errors so SecretsManager can retry
                raise
            else:
                logger.error(f"Unexpected error fetching secret '{name}' ({error_category}): {e}")
                return None

    async def get_many(self, names: Iterable[str]) -> Dict[str, Optional[str]]:
        """Enhanced bulk retrieval with better error handling and parallelization.

        Uses controlled concurrency to avoid overwhelming Key Vault while still
        providing better performance than sequential calls.
        """
        async def fetch_single(name: str) -> tuple[str, Optional[str]]:
            try:
                value = await self.get(name)
                return name, value
            except Exception as e:
                logger.warning(f"Failed to fetch secret '{name}' in bulk operation: {e}")
                return name, None

        # Use semaphore to limit concurrent requests to Key Vault
        semaphore = asyncio.Semaphore(5)  # Max 5 concurrent requests
        
        async def limited_fetch(name: str) -> tuple[str, Optional[str]]:
            async with semaphore:
                return await fetch_single(name)

        names_list = list(names)
        logger.debug(f"Bulk fetching {len(names_list)} secrets from Azure Key Vault")
        
        tasks = [limited_fetch(name) for name in names_list]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        final_results: Dict[str, Optional[str]] = {}
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Bulk fetch task failed: {result}")
                continue
            name, value = result
            final_results[name] = value
        
        success_count = sum(1 for v in final_results.values() if v is not None)
        logger.info(f"Bulk fetch completed: {success_count}/{len(names_list)} secrets retrieved")
        
        return final_results

    async def close(self):
        """Release underlying network resources / credential handles."""
        if self._client:
            try:
                await self._client.close()
                logger.debug("Azure Key Vault client closed successfully")
            except Exception as e:
                logger.warning(f"Error closing Azure Key Vault client: {e}")
        
        if self._credential:
            try:
                if hasattr(self._credential, 'close'):
                    await self._credential.close()
                logger.debug("Azure credential closed successfully")
            except Exception as e:
                logger.warning(f"Error closing Azure credential: {e}")
        
        self._client = None
        self._credential = None

    def get_client_info(self) -> Dict[str, any]:
        """Get client information for debugging and monitoring."""
        return {
            "client_exists": self._client is not None,
            "credential_type": type(self._credential).__name__ if self._credential else None,
            "last_auth_failure": self._last_auth_failure,
            "auth_failure_count": self._auth_failure_count,
            "client_age_seconds": (
                time.time() - self._client_creation_time 
                if self._client_creation_time else None
            ),
            "vault_url": secret_manager_config.AZURE_KEY_VAULT_URL,
        }
