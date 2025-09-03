from __future__ import annotations
import asyncio
from typing import Optional, Iterable
from .interface import SecretProvider
from .cache import TTLSecretCache
from .azure_key_vault import AzureKeyVaultSecretProvider
from src.infra.utils.logger import get_logger

logger = get_logger("secrets_manager")


class SecretsManager:
    """High-level facade combining provider + cache + orchestration utilities.

    Responsibilities
    ----------------
    * Provide a *single* place the rest of the codebase calls to obtain a secret.
    * Hide provider selection, caching policy, and potential retries.
    * Offer warming & invalidation primitives (rotation workflows).

    Concurrency Model
    -----------------
    Reads are optimistic: first consult the local cache (cheap, lock in cache
    object). Cache miss triggers a provider call. To prevent a thundering herd,
    we use an async lock only for the rare race where multiple coroutines miss
    concurrently for the same key (future optimisation: keyed locks).

    Migration Note
    --------------
    The synchronous database module currently uses a blocking shim. Long‑term
    plan is to refactor DB secret fetch to async or run sync path in a threadpool.
    """

    def __init__(self, provider: SecretProvider | None = None, ttl_seconds: int = 300, max_size: int = 1000):
        self.provider = provider or AzureKeyVaultSecretProvider()
        self.cache = TTLSecretCache(ttl_seconds=ttl_seconds, max_size=max_size)
        self._lock = asyncio.Lock()

    async def get(self, name: str) -> Optional[str]:
        """Return secret value or None if not found / transient failure.

        Basic flow: cache -> provider -> populate cache -> return.
        TODO: add retry/backoff and richer error mapping when needed.
        """
        cached = self.cache.get(name)
        if cached is not None:
            return cached
        # Single-flight (coarse) to limit duplicate outbound calls when many
        # awaiters arrive simultaneously on startup.
        async with self._lock:
            cached2 = self.cache.get(name)
            if cached2 is not None:
                return cached2
            value = await self.provider.get(name)
            if value is not None:
                self.cache.set(name, value)
            return value

    async def invalidate(self, name: str):
        """Remove a cached secret (rotation or forced refresh)."""
        self.cache.invalidate(name)

    async def warm(self, names: Iterable[str]):
        """Preload a collection of secrets concurrently (best effort)."""
        await asyncio.gather(*(self.get(n) for n in names))


# Singleton instance exposed for import convenience
secrets_manager = SecretsManager()
