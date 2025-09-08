from __future__ import annotations
import asyncio
import time
from typing import Optional, Iterable, Dict

from .interface import SecretProvider
from .azure_key_vault import AzureKeyVaultSecretProvider
from src.infra.utils.logger import get_logger

logger = get_logger("secrets_manager")


class SecretsManager:
    """Lean secrets manager: minimal retry, simple health."""

    def __init__(
        self,
        provider: SecretProvider | None = None,
        *,
        max_retries: int = 1,
        initial_retry_delay: float = 0.5,
        max_retry_delay: float = 2.0,
    ) -> None:
        self.provider = provider or AzureKeyVaultSecretProvider()
        self.max_retries = max_retries
        self.initial_retry_delay = initial_retry_delay
        self.max_retry_delay = max_retry_delay
        # Minimal counters
        self._provider_calls = 0
        self._provider_failures = 0
        self._last_failure_time: Optional[float] = None

    async def get(self, name: str) -> Optional[str]:
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
                    delay = min(
                        self.initial_retry_delay * (2**attempt), self.max_retry_delay
                    )
                    logger.warning(
                        "Retrying secret '%s' in %.2fs after error: %s", name, delay, e
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "Failed to fetch secret '%s' after retries: %s", name, e
                    )
        # If all attempts failed or provider returned None each time
        return None

    async def get_with_fallback(self, name: str) -> Optional[str]:
        """Compatibility alias; just calls get()."""
        return await self.get(name)

    async def warm(self, names: Iterable[str]) -> None:
        """Best-effort parallel prefetch (no cache retained)."""
        await asyncio.gather(*(self.get(n) for n in names), return_exceptions=True)

    def get_metrics(self) -> Dict[str, any]:
        return {
            "provider_calls": self._provider_calls,
            "provider_failures": self._provider_failures,
            "last_failure_time": self._last_failure_time,
            "consecutive_failures": 0,
            "circuit_breaker_open": False,
            "failed_secrets_count": 0,
        }

    def health_check(self) -> Dict[str, any]:
        return {
            "healthy": True,
            "circuit_breaker_open": False,
            "consecutive_failures": 0,
            "last_failure_count": 0,
        }


# Singleton instance with simplified configuration
secrets_manager = SecretsManager()
