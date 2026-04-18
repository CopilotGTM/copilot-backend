from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from typing import Any

from src.infra.config.configs import secret_manager_config
from src.infra.utils.logger import get_logger

from .azure_key_vault import AzureKeyVaultSecretProvider
from .interface import SecretProvider

logger = get_logger("secrets_manager")


class SecretsManager:
    """Lean secrets manager: minimal retry, simple health."""

    def __init__(
        self,
        provider: SecretProvider | None = None,
        *,
        max_retries: int | None = None,
        initial_retry_delay: float | None = None,
        max_retry_delay: float | None = None,
    ) -> None:
        self.provider = provider or AzureKeyVaultSecretProvider()
        self.max_retries = (
            max_retries
            if max_retries is not None
            else int(secret_manager_config.SECRET_GET_MAX_RETRIES)
        )
        self.initial_retry_delay = (
            initial_retry_delay
            if initial_retry_delay is not None
            else float(secret_manager_config.SECRET_GET_RETRY_INITIAL_DELAY_SEC)
        )
        self.max_retry_delay = (
            max_retry_delay
            if max_retry_delay is not None
            else float(secret_manager_config.SECRET_GET_RETRY_MAX_DELAY_SEC)
        )
        # Minimal counters
        self._provider_calls = 0
        self._provider_failures = 0
        self._last_failure_time: float | None = None

    async def get(self, name: str) -> str | None:
        """Fetch secret from provider with a small retry window.

        Returns None if not found or on persistent/transient failure after retries.
        """
        for attempt in range(self.max_retries + 1):
            try:
                self._provider_calls += 1
                value = await self.provider.get(name)
                return value
            except Exception as e:  # transient network/timeouts likely
                self._provider_failures += 1
                self._last_failure_time = time.time()
                if attempt < self.max_retries:
                    delay = min(self.initial_retry_delay * (2**attempt), self.max_retry_delay)
                    logger.warning(
                        "Retrying secret fetch",
                        extra={
                            "event": "secret_fetch_retry",
                            "secret_name": name,
                            "retry_delay_s": delay,
                            "failure_reason": str(e),
                        },
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "Secret fetch failed after retries",
                        extra={
                            "event": "secret_fetch_failed",
                            "secret_name": name,
                            "failure_reason": str(e),
                        },
                    )
        # If all attempts failed or provider returned None each time
        return None

    async def get_with_fallback(self, name: str) -> str | None:
        """Compatibility alias; just calls get()."""
        return await self.get(name)

    async def warm(self, names: Iterable[str]) -> None:
        """Best-effort parallel prefetch (no cache retained)."""
        await asyncio.gather(*(self.get(n) for n in names), return_exceptions=True)

    def get_metrics(self) -> dict[str, Any]:
        return {
            "provider_calls": self._provider_calls,
            "provider_failures": self._provider_failures,
            "last_failure_time": self._last_failure_time,
            "consecutive_failures": 0,
            "circuit_breaker_open": False,
            "failed_secrets_count": 0,
        }

    def health_check(self) -> dict[str, Any]:
        return {
            "healthy": True,
            "circuit_breaker_open": False,
            "consecutive_failures": 0,
            "last_failure_count": 0,
        }


# Singleton instances
secrets_manager = SecretsManager()
master_secrets_manager = SecretsManager(
    provider=AzureKeyVaultSecretProvider(vault_url=secret_manager_config.AZURE_MASTER_KEY_VAULT_URL)
)
